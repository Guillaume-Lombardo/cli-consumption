from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.opencode import OpenCodeAdapter
from cli_consumption.adapters.registry import diagnose_provider, resolve_adapter_spec
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "privacy canary secret"


def database(home: Path, *, extra: bool = False, malformed: bool = False) -> Path:
    path = home / "opencode.db"
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
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            seq INTEGER NOT NULL,
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
    messages: list[tuple[str, str, int, int, dict[str, Any]]] = [
        (
            "msg_user_1",
            "user",
            1,
            1_777_114_800_000,
            {"text": CANARY, "files": [{"path": CANARY}]},
        ),
        (
            "msg_assistant_1",
            "assistant",
            2,
            1_777_114_801_000,
            {
                "agent": "build",
                "model": {"providerID": "anthropic", "id": "claude-sonnet-4-6"},
                "content": [
                    {"type": "text", "id": "part_text", "text": CANARY},
                    {
                        "type": "tool",
                        "id": "part_tool",
                        "name": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": CANARY},
                            "content": [{"type": "text", "text": CANARY}],
                            "structured": {"output": CANARY},
                        },
                        "time": {
                            "created": 1_777_114_801_500,
                            "completed": 1_777_114_802_000,
                        },
                    },
                ],
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
                "metadata": {"secret": CANARY},
            },
        ),
        (
            "msg_compaction",
            "compaction",
            3,
            1_777_114_803_000,
            {"reason": "auto", "summary": CANARY, "recent": CANARY},
        ),
        (
            "msg_user_2",
            "user",
            4,
            1_777_114_810_000,
            {"text": CANARY, "files": []},
        ),
        (
            "msg_assistant_2",
            "assistant",
            5,
            1_777_114_811_000,
            {
                "model": {"providerID": "openai", "id": "gpt-5"},
                "content": [{"type": "text", "id": "part", "text": CANARY}],
                "tokens": {
                    "input": 2,
                    "output": 0,
                    "reasoning": 1,
                    "cache": {"read": 0, "write": 0},
                },
                "error": {"type": "unknown", "message": CANARY},
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
                "msg_system",
                "system",
                6,
                1_777_114_812_000,
                {"text": CANARY},
            )
        )
    connection.executemany(
        "INSERT INTO session_message "
        "(id, session_id, type, seq, time_created, time_updated, data) "
        "VALUES (?, 'ses_test', ?, ?, ?, ?, ?)",
        [
            (identifier, kind, sequence, timestamp, timestamp, json.dumps(data))
            for identifier, kind, sequence, timestamp, data in messages
        ],
    )
    if malformed:
        connection.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "msg_bad_json",
                "ses_test",
                "assistant",
                99,
                1_777_114_899_000,
                1_777_114_899_000,
                "not-json",
            ),
        )
    connection.commit()
    connection.close()
    return path


def current_database(home: Path, *, malformed: bool = False) -> Path:
    path = home / "opencode.db"
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            directory TEXT NOT NULL,
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
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            seq INTEGER NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?)",
        (
            "ses_current",
            "/srv/work/acme/service",
            1_777_114_800_000,
            1_777_114_812_000,
        ),
    )
    messages = [
        (
            "msg_user_1",
            1_777_114_800_000,
            {
                "role": "user",
                "time": {"created": 1_777_114_800_000},
                "agent": "build",
                "model": {
                    "providerID": "anthropic",
                    "modelID": "claude-sonnet-4-6",
                },
                "system": CANARY,
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
                    "total": 180,
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
                "time": {"created": 1_777_114_810_000},
                "agent": "build",
                "model": {"providerID": "openai", "modelID": "gpt-5"},
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
                    "total": 3,
                    "input": 2,
                    "output": 0,
                    "reasoning": 1,
                    "cache": {"read": 0, "write": 0},
                },
                "time": {
                    "created": 1_777_114_811_000,
                    "completed": 1_777_114_811_500,
                },
            },
        ),
    ]
    connection.executemany(
        "INSERT INTO message VALUES (?, 'ses_current', ?, ?, ?)",
        [
            (identifier, timestamp, timestamp, json.dumps(data))
            for identifier, timestamp, data in messages
        ],
    )
    parts = [
        (
            "prt_tool",
            "msg_assistant_1",
            1_777_114_801_500,
            {
                "type": "tool",
                "callID": "call_1",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": CANARY},
                    "output": CANARY,
                    "time": {
                        "start": 1_777_114_801_500,
                        "end": 1_777_114_802_000,
                    },
                },
            },
        ),
        (
            "prt_compaction",
            "msg_user_2",
            1_777_114_810_000,
            {"type": "compaction", "auto": True, "summary": CANARY},
        ),
    ]
    connection.executemany(
        "INSERT INTO part VALUES (?, ?, 'ses_current', ?, ?, ?)",
        [
            (identifier, message_id, timestamp, timestamp, json.dumps(data))
            for identifier, message_id, timestamp, data in parts
        ],
    )
    connection.execute(
        "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "evt_agent",
            "ses_current",
            "agent-switched",
            1,
            1_777_114_800_000,
            1_777_114_800_000,
            json.dumps({"agent": CANARY}),
        ),
    )
    if malformed:
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (
                "prt_bad",
                "msg_assistant_1",
                "ses_current",
                1_777_114_899_000,
                1_777_114_899_000,
                "not-json",
            ),
        )
    connection.commit()
    connection.close()
    return path


def test_collects_v2_usage_tools_turns_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "opencode"
    database(home)

    snapshot = OpenCodeAdapter().collect(
        [("laptop", home)], [("acme", "/srv/work/acme")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "opencode:ses_test"
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
    assert conversation["total_tokens"] == 178
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert snapshot.tool_calls[0]["tool_name"] == "bash"
    assert snapshot.compaction_events[0]["turn_id"] == "opencode:ses_test:msg_user_1"
    assert CANARY not in str(snapshot.to_dict())


def test_collects_opencode_1_18_23_message_and_part_schema(tmp_path: Path) -> None:
    home = tmp_path / "opencode"
    current_database(home)

    snapshot = OpenCodeAdapter().collect(
        [("laptop", home)], [("acme", "/srv/work/acme")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "opencode:ses_current"
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
    assert conversation["unattributed_tokens"] == 5
    assert conversation["total_tokens"] == 183
    assert snapshot.tool_calls[0]["tool_name"] == "bash"
    assert snapshot.compaction_events[0]["turn_id"] == "opencode:ses_current:msg_user_2"
    assert CANARY not in str(snapshot.to_dict())


def test_current_schema_skips_malformed_parts_without_exposing_them(
    tmp_path: Path,
) -> None:
    home = tmp_path / "opencode"
    current_database(home, malformed=True)

    snapshot = OpenCodeAdapter().collect([("machine", home)])

    assert snapshot.malformed_records == 1
    assert snapshot.conversations[0]["tool_calls"] == 1
    assert CANARY not in str(snapshot.to_dict())
    spec = resolve_adapter_spec("opencode")
    assert spec is not None
    assert diagnose_provider(spec, home).status == "degraded"


def test_deduplicates_copies_and_handles_partial_or_malformed_rows(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    database(first, malformed=True)
    database(second, extra=True)

    snapshot = OpenCodeAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 1
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["event_count"] == 6
    assert (
        snapshot.to_dict()
        == OpenCodeAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing OpenCode database"):
        OpenCodeAdapter().collect([("machine", tmp_path / "missing")])


def test_rejects_old_schema_without_exposing_records(tmp_path: Path) -> None:
    home = tmp_path / "old"
    home.mkdir()
    connection = sqlite3.connect(home / "opencode.db")
    connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(
        ValueError, match="Unsupported OpenCode database schema"
    ) as error:
        OpenCodeAdapter().collect([("machine", home)])
    assert CANARY not in str(error.value)
    spec = resolve_adapter_spec("opencode")
    assert spec is not None
    assert diagnose_provider(spec, home).status == "unsupported-schema"


def test_rejects_incomplete_current_schema_instead_of_using_projection(
    tmp_path: Path,
) -> None:
    home = tmp_path / "incomplete"
    database(home)
    connection = sqlite3.connect(home / "opencode.db")
    connection.execute("CREATE TABLE message (id TEXT PRIMARY KEY, data TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(
        ValueError, match="Unsupported OpenCode database schema"
    ) as error:
        OpenCodeAdapter().collect([("machine", home)])
    assert CANARY not in str(error.value)
    spec = resolve_adapter_spec("opencode")
    assert spec is not None
    assert diagnose_provider(spec, home).status == "unsupported-schema"


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "opencode"
    current_database(home)
    snapshot = OpenCodeAdapter().collect([("machine", home)])
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
