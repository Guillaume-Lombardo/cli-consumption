from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cli_consumption.adapters.kilo import KiloAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
    read_table,
)

CANARY = "privacy canary secret"


def database(home: Path, *, extra: bool = False, malformed: bool = False) -> Path:
    path = home / "kilo.db"
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            directory TEXT NOT NULL,
            title TEXT NOT NULL,
            metadata TEXT,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)",
        (
            "ses_test",
            "/srv/work/acme/service",
            CANARY,
            json.dumps({"secret": CANARY}),
            1_777_114_800_000,
            1_777_114_812_000 if extra else 1_777_114_811_000,
        ),
    )
    messages: list[tuple[str, int, dict[str, Any]]] = [
        (
            "msg_user_1",
            1_777_114_800_000,
            {
                "role": "user",
                "agent": "build",
                "system": CANARY,
                "summary": {"title": CANARY, "body": CANARY},
                "time": {"created": 1_777_114_800_000},
            },
        ),
        (
            "msg_assistant_1",
            1_777_114_801_000,
            {
                "role": "assistant",
                "parentID": "msg_user_1",
                "providerID": "anthropic",
                "modelID": "claude-sonnet-4-6",
                "finish": "tool-calls",
                "tokens": {
                    "input": 100,
                    "output": 20,
                    "reasoning": 5,
                    "cache": {"read": 40, "write": 10},
                },
                "time": {
                    "created": 1_777_114_801_000,
                    "completed": 1_777_114_802_000,
                },
                "path": {"cwd": CANARY, "root": CANARY},
            },
        ),
        (
            "msg_user_2",
            1_777_114_810_000,
            {
                "role": "user",
                "system": CANARY,
                "time": {"created": 1_777_114_810_000},
            },
        ),
        (
            "msg_assistant_2",
            1_777_114_811_000,
            {
                "role": "assistant",
                "parentID": "msg_user_2",
                "providerID": "openai",
                "modelID": "gpt-5",
                "tokens": {
                    "total": 4,
                    "input": 2,
                    "output": 0,
                    "reasoning": 1,
                    "cache": {"read": 0, "write": 0},
                },
                "error": {"name": "UnknownError", "message": CANARY},
                "time": {
                    "created": 1_777_114_811_000,
                    "completed": 1_777_114_811_500,
                },
            },
        ),
    ]
    if extra:
        messages.append(
            (
                "msg_assistant_extra",
                1_777_114_812_000,
                {
                    "role": "assistant",
                    "parentID": "msg_user_2",
                    "providerID": "openai",
                    "modelID": "gpt-5",
                    "tokens": {
                        "input": 1,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                    "time": {"created": 1_777_114_812_000},
                },
            )
        )
    connection.executemany(
        "INSERT INTO message VALUES (?, 'ses_test', ?, ?, ?)",
        [
            (identifier, timestamp, timestamp, json.dumps(data))
            for identifier, timestamp, data in messages
        ],
    )
    parts = [
        (
            "prt_text",
            "msg_assistant_1",
            1_777_114_801_100,
            {"type": "text", "text": CANARY, "metadata": {"secret": CANARY}},
        ),
        (
            "prt_tool",
            "msg_assistant_1",
            1_777_114_801_500,
            {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": CANARY},
                    "output": CANARY,
                    "metadata": {"secret": CANARY},
                    "time": {
                        "start": 1_777_114_801_500,
                        "end": 1_777_114_802_000,
                    },
                },
            },
        ),
        (
            "prt_compaction",
            "msg_user_1",
            1_777_114_803_000,
            {"type": "compaction", "auto": True, "summary": CANARY},
        ),
    ]
    connection.executemany(
        "INSERT INTO part VALUES (?, ?, 'ses_test', ?, ?, ?)",
        [
            (identifier, message_id, timestamp, timestamp, json.dumps(data))
            for identifier, message_id, timestamp, data in parts
        ],
    )
    if malformed:
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_bad_json",
                "msg_assistant_1",
                "ses_test",
                1_777_114_899_000,
                1_777_114_899_000,
                "not-json",
            ),
        )
    connection.commit()
    connection.close()
    return path


def test_collects_usage_tools_turns_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "kilo"
    database(home)

    snapshot = KiloAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "kilo:ses_test"
    assert conversation["project"] == "acme"
    assert conversation["models"] == ["anthropic/claude-sonnet-4-6", "openai/gpt-5"]
    assert conversation["model_calls"] == 2
    assert conversation["tool_calls"] == 1
    assert conversation["input_tokens"] == 152
    assert conversation["uncached_input_tokens"] == 102
    assert conversation["cached_input_tokens"] == 40
    assert conversation["cache_write_input_tokens"] == 10
    assert conversation["output_tokens"] == 26
    assert conversation["reasoning_output_tokens"] == 6
    assert conversation["visible_output_tokens"] == 20
    assert conversation["total_tokens"] == 179
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert snapshot.tool_calls[0]["tool_name"] == "bash"
    assert snapshot.compaction_events[0]["turn_id"] == "kilo:ses_test:msg_user_1"
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_deduplicates_copies_and_handles_malformed_records(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    database(first, malformed=True)
    database(second, extra=True)

    snapshot = KiloAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 1
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["event_count"] == 8
    assert (
        snapshot.to_dict()
        == KiloAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Kilo Code database"):
        KiloAdapter().collect([("machine", tmp_path / "missing")])


def test_rejects_old_schema_without_exposing_records(tmp_path: Path) -> None:
    home = tmp_path / "old"
    home.mkdir()
    connection = sqlite3.connect(home / "kilo.db")
    connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(
        ValueError, match="Unsupported Kilo Code database schema"
    ) as error:
        KiloAdapter().collect([("machine", home)])
    assert CANARY not in str(error.value)


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "kilo"
    database(home, extra=True)
    snapshot = KiloAdapter().collect([("machine", home)])
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
