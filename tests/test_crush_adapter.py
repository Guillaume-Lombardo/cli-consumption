from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from storage_helpers import read_table

from cli_consumption.adapters._shared import ProviderDataLimitError
from cli_consumption.adapters.crush import CrushAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "privacy canary secret"


def database(data_dir: Path, *, extra: bool = False, malformed: bool = False) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "crush.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            title TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cost REAL NOT NULL,
            summary_message_id TEXT,
            todos TEXT,
            updated_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            parts TEXT NOT NULL,
            model TEXT,
            provider TEXT,
            is_summary_message INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            finished_at INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO sessions VALUES (?, NULL, ?, 5, 120, 30, 9.99, ?, ?, ?, ?)",
        (
            "session-main",
            CANARY,
            "message-summary",
            json.dumps({"content": CANARY}),
            1_777_114_812,
            1_777_114_800,
        ),
    )
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, 0, 99, 99, 1, NULL, NULL, ?, ?)",
        (
            "session-child",
            "session-main",
            CANARY,
            1_777_114_812,
            1_777_114_811,
        ),
    )
    messages = [
        (
            "message-user-1",
            "user",
            [{"type": "text", "data": {"text": CANARY}}],
            None,
            None,
            0,
            1_777_114_800,
            1_777_114_800,
            1_777_114_800,
        ),
        (
            "message-assistant-1",
            "assistant",
            [
                {"type": "text", "data": {"text": CANARY}},
                {
                    "type": "tool_call",
                    "data": {"id": "tool-1", "name": "bash", "input": CANARY},
                },
                {
                    "type": "tool_result",
                    "data": {"content": CANARY, "metadata": CANARY},
                },
                {
                    "type": "finish",
                    "data": {"reason": "end_turn", "message": CANARY},
                },
            ],
            "claude-sonnet-4-6",
            "anthropic",
            0,
            1_777_114_801,
            1_777_114_802,
            1_777_114_802,
        ),
        (
            "message-summary",
            "assistant",
            [
                {"type": "text", "data": {"text": CANARY}},
                {"type": "finish", "data": {"reason": "end_turn"}},
            ],
            "claude-sonnet-4-6",
            "anthropic",
            1,
            1_777_114_803,
            1_777_114_804,
            1_777_114_804,
        ),
        (
            "message-user-2",
            "user",
            [{"type": "text", "data": {"text": CANARY}}],
            None,
            None,
            0,
            1_777_114_810,
            1_777_114_810,
            1_777_114_810,
        ),
        (
            "message-assistant-2",
            "assistant",
            [
                {"type": "reasoning", "data": {"thinking": CANARY}},
                {
                    "type": "finish",
                    "data": {"reason": "canceled", "details": CANARY},
                },
            ],
            "gpt-5",
            "openai",
            0,
            1_777_114_811,
            1_777_114_812,
            1_777_114_812,
        ),
    ]
    if extra:
        messages.append(
            (
                "message-system",
                "system",
                [{"type": "text", "data": {"text": CANARY}}],
                None,
                None,
                0,
                1_777_114_812,
                1_777_114_812,
                1_777_114_812,
            )
        )
    connection.executemany(
        "INSERT INTO messages VALUES (?, 'session-main', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                identifier,
                role,
                json.dumps(parts),
                model,
                provider,
                summary,
                created,
                updated,
                finished,
            )
            for (
                identifier,
                role,
                parts,
                model,
                provider,
                summary,
                created,
                updated,
                finished,
            ) in messages
        ],
    )
    if malformed:
        connection.execute(
            "INSERT INTO messages VALUES (?, 'session-main', 'assistant', ?, NULL, "
            "NULL, 0, ?, ?, NULL)",
            ("message-malformed", "not-json", 1_777_114_899, 1_777_114_899),
        )
    connection.commit()
    connection.close()
    return path


def registry(home: Path, project: str, data_dir: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects.json").write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "path": project,
                        "data_dir": str(data_dir),
                        "last_accessed": "2026-08-26T10:00:00Z",
                    }
                ]
            }
        )
    )


def test_collects_usage_models_tools_turns_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "global"
    data_dir = tmp_path / "project-data"
    database(data_dir)
    registry(home, "/srv/work/acme/service", data_dir)

    snapshot = CrushAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "crush:session-main"
    assert conversation["project"] == "acme"
    assert conversation["models"] == [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-5",
    ]
    assert conversation["model_calls"] == 2
    assert conversation["tool_calls"] == 1
    assert conversation["input_tokens"] == 120
    assert conversation["output_tokens"] == 30
    assert conversation["total_tokens"] == 150
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert snapshot.turns[0]["total_tokens"] == 0
    assert snapshot.turns[1]["total_tokens"] == 150
    assert snapshot.tool_calls[0]["tool_name"] == "bash"
    assert snapshot.compaction_events[0]["turn_id"] == (
        "crush:session-main:message-user-1"
    )
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_reads_direct_database_and_deduplicates_copies(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    database(first, malformed=True)
    database(second, extra=True)

    snapshot = CrushAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 1
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["event_count"] == 6
    assert (
        snapshot.to_dict()
        == CrushAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )


def test_crush_shares_sqlite_budget_across_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first_db, second_db = database(first), database(second)
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_BYTES",
        first_db.stat().st_size + second_db.stat().st_size - 1,
    )
    with pytest.raises(ProviderDataLimitError) as error:
        CrushAdapter().collect([("desktop", first), ("laptop", second)])
    assert str(error.value) == "provider_sqlite_file_too_large"


def test_crush_budgets_registry_entries_before_path_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, data_dir = tmp_path / "global", tmp_path / "project"
    database(data_dir)
    registry(home, "/srv/project", data_dir)
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 3)
    with pytest.raises(ProviderDataLimitError) as error:
        CrushAdapter().collect([("machine", home)])
    assert str(error.value) == "provider_candidate_limit_exceeded"


def test_rejects_missing_or_old_data_without_exposing_records(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No readable Crush databases"):
        CrushAdapter().collect([("machine", tmp_path / "missing")])

    home = tmp_path / "old"
    home.mkdir()
    connection = sqlite3.connect(home / "crush.db")
    connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="Unsupported Crush database schema") as error:
        CrushAdapter().collect([("machine", home)])
    assert CANARY not in str(error.value)


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "crush"
    database(home, extra=True)
    snapshot = CrushAdapter().collect([("machine", home)])
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
