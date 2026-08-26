from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cli_consumption.adapters.goose import GooseAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
    read_table,
)

CANARY = "GOOSE_CANARY_SECRET_DO_NOT_PERSIST"


def database(home: Path, *, extra: bool = False, malformed: bool = False) -> Path:
    home.mkdir(parents=True)
    path = home / "sessions.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, name TEXT, working_dir TEXT NOT NULL,
            created_at TIMESTAMP, updated_at TIMESTAMP,
            provider_name TEXT, model_config_json TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT,
            session_id TEXT NOT NULL, role TEXT NOT NULL,
            content_json TEXT NOT NULL, created_timestamp INTEGER NOT NULL,
            metadata_json TEXT
        );
        CREATE TABLE usage_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            created_timestamp INTEGER NOT NULL, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            cost REAL, cost_source TEXT, is_compaction INTEGER DEFAULT 0
        );
        """
    )
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "20260826_1",
            CANARY,
            "/srv/work/acme",
            "2026-04-26T10:00:00Z",
            "2026-04-26T10:00:20Z",
            "anthropic",
            json.dumps(
                {
                    "model_name": "claude-sonnet-4-6",
                    "request_params": {"secret": CANARY},
                }
            ),
        ),
    )
    messages = [
        (
            "prompt-1",
            "user",
            1_777_197_600,
            [{"type": "text", "text": CANARY}],
            {"userVisible": True, "operations": {"secret": CANARY}},
        ),
        (
            "assistant-1",
            "assistant",
            1_777_197_602,
            [
                {"type": "text", "text": CANARY},
                {
                    "type": "toolRequest",
                    "id": "tool-1",
                    "toolCall": {
                        "status": "success",
                        "value": {
                            "name": "developer__shell",
                            "arguments": {"command": CANARY},
                        },
                    },
                },
            ],
            {"userVisible": True},
        ),
        (
            "tool-result",
            "user",
            1_777_197_603,
            [
                {
                    "type": "toolResponse",
                    "id": "tool-1",
                    "toolResult": {"status": "success", "value": CANARY},
                }
            ],
            {"userVisible": True},
        ),
        (
            "prompt-2",
            "user",
            1_777_197_610,
            [{"type": "text", "text": CANARY}],
            {"userVisible": True},
        ),
        (
            "assistant-2",
            "assistant",
            1_777_197_612,
            [{"type": "error", "kind": "other", "message": CANARY}],
            {"userVisible": True},
        ),
        (
            "hidden-context",
            "user",
            1_777_197_613,
            [{"type": "text", "text": CANARY}],
            {"userVisible": False},
        ),
    ]
    if extra:
        messages.append(
            (
                "assistant-extra",
                "assistant",
                1_777_197_614,
                [{"type": "text", "text": CANARY}],
                {"userVisible": True},
            )
        )
    connection.executemany(
        "INSERT INTO messages "
        "(message_id, session_id, role, content_json, created_timestamp, "
        "metadata_json) "
        "VALUES (?, '20260826_1', ?, ?, ?, ?)",
        [
            (identifier, role, json.dumps(content), timestamp, json.dumps(metadata))
            for identifier, role, timestamp, content, metadata in messages
        ],
    )
    connection.executemany(
        "INSERT INTO usage_ledger "
        "(session_id, created_timestamp, model, input_tokens, output_tokens, "
        "total_tokens, cache_read_tokens, cache_write_tokens, cost, cost_source, "
        "is_compaction) VALUES ('20260826_1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                1_777_197_602,
                "claude-sonnet-4-6",
                100,
                20,
                125,
                30,
                10,
                1.23,
                CANARY,
                0,
            ),
            (
                1_777_197_612,
                None,
                50,
                5,
                55,
                5,
                0,
                0.5,
                CANARY,
                1,
            ),
        ],
    )
    if malformed:
        connection.execute(
            "INSERT INTO messages "
            "(message_id, session_id, role, content_json, created_timestamp, "
            "metadata_json) "
            "VALUES ('bad-json', '20260826_1', 'assistant', 'not-json', ?, '{}')",
            (1_777_197_699,),
        )
    connection.commit()
    connection.close()
    return path


def test_collects_usage_tools_turns_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "goose"
    database(home)

    snapshot = GooseAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "goose:20260826_1"
    assert conversation["source"] == "local-sqlite-v16"
    assert conversation["project"] == "acme"
    assert conversation["models"] == ["anthropic/claude-sonnet-4-6"]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 2
    assert conversation["tool_calls"] == 1
    assert conversation["input_tokens"] == 150
    assert conversation["uncached_input_tokens"] == 105
    assert conversation["cached_input_tokens"] == 35
    assert conversation["cache_write_input_tokens"] == 10
    assert conversation["output_tokens"] == 25
    assert conversation["total_tokens"] == 180
    assert conversation["unattributed_tokens"] == 5
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert snapshot.tool_calls[0]["tool_name"] == "developer__shell"
    assert snapshot.compaction_events[0]["turn_id"] == ("goose:20260826_1:prompt-2")
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_deduplicates_copies_and_handles_malformed_records(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    database(first, malformed=True)
    database(second, extra=True)

    snapshot = GooseAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 1
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["event_count"] == 9
    assert (
        snapshot.to_dict()
        == GooseAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Goose database"):
        GooseAdapter().collect([("machine", tmp_path / "missing")])


def test_rejects_unsupported_schema_without_exposing_records(tmp_path: Path) -> None:
    home = tmp_path / "old"
    home.mkdir()
    connection = sqlite3.connect(home / "sessions.db")
    connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="Unsupported Goose database schema") as error:
        GooseAdapter().collect([("machine", home)])
    assert CANARY not in str(error.value)


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "goose"
    database(home, extra=True)
    snapshot = GooseAdapter().collect([("machine", home)])
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        ingest_snapshot(engine, snapshot)
        rows = {name: read_table(engine, name) for name in TABLES}
        assert CANARY not in json.dumps(rows)
        output = tmp_path / "reports"
        paths = export_csv(engine, output)
        dashboard = output / "dashboard.html"
        generate_dashboard(engine, dashboard)
        assert all(CANARY not in path.read_text() for path in paths)
        assert CANARY not in dashboard.read_text()
    finally:
        engine.dispose()
