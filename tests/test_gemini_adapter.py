from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.gemini import GeminiAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "gemini privacy canary secret"


def session(home: Path, *, extra: bool = False) -> Path:
    records: list[dict[str, Any]] = [
        {
            "sessionId": "session-1",
            "projectHash": "private-project-hash",
            "startTime": "2026-08-25T10:00:00Z",
            "lastUpdated": "2026-08-25T10:00:30Z",
        },
        {
            "id": "prompt-1",
            "timestamp": "2026-08-25T10:00:01Z",
            "type": "user",
            "content": CANARY,
        },
        {
            "id": "response-1",
            "timestamp": "2026-08-25T10:00:02Z",
            "type": "gemini",
            "content": CANARY,
            "model": "gemini-2.5-pro",
        },
        {
            "id": "response-1",
            "timestamp": "2026-08-25T10:00:02Z",
            "type": "gemini",
            "content": CANARY,
            "thoughts": [{"subject": CANARY, "description": CANARY}],
            "model": "gemini-2.5-pro",
            "tokens": {
                "input": 100,
                "output": 20,
                "cached": 40,
                "thoughts": 5,
                "tool": 10,
                "total": 130,
            },
            "toolCalls": [
                {
                    "id": "tool-1",
                    "name": "run_shell_command",
                    "args": {"command": CANARY},
                    "result": CANARY,
                    "description": CANARY,
                    "status": "success",
                    "timestamp": "2026-08-25T10:00:03Z",
                }
            ],
        },
        {
            "$set": {
                "lastUpdated": "2026-08-25T10:00:20Z",
                "summary": CANARY,
                "directories": [f"/private/{CANARY}"],
                "memoryScratchpad": {"workflowSummary": CANARY},
            }
        },
        {
            "id": "prompt-2",
            "timestamp": "2026-08-25T10:00:10Z",
            "type": "user",
            "content": CANARY,
        },
        {
            "id": "response-2",
            "timestamp": "2026-08-25T10:00:11Z",
            "type": "gemini",
            "content": CANARY,
            "model": "gemini-2.5-flash",
            "tokens": {
                "input": 20,
                "output": 4,
                "cached": 30,
                "thoughts": 2,
                "total": 27,
            },
        },
        {
            "id": "discarded-prompt",
            "timestamp": "2026-08-25T10:00:12Z",
            "type": "user",
            "content": CANARY,
        },
        {
            "id": "discarded-error",
            "timestamp": "2026-08-25T10:00:13Z",
            "type": "error",
            "content": CANARY,
        },
        {"$rewindTo": "discarded-prompt"},
        {
            "id": "prompt-3",
            "timestamp": "2026-08-25T10:00:14Z",
            "type": "user",
            "content": CANARY,
        },
        {
            "id": "response-3",
            "timestamp": "2026-08-25T10:00:15Z",
            "type": "gemini",
            "content": CANARY,
            "model": "gemini-2.5-flash",
            "tokens": {"input": 3, "output": 1, "total": 4},
        },
    ]
    if extra:
        records.append({"$set": {"lastUpdated": "2026-08-25T10:00:30Z"}})
    path = home / "tmp" / "project-hash" / "chats" / "session-main.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def test_collects_current_usage_tools_turns_and_rewinds(tmp_path: Path) -> None:
    home = tmp_path / "gemini"
    session(home)
    snapshot = GeminiAdapter().collect(
        [("laptop", home)], [("must-not-map", "/private")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "gemini:session-1"
    assert conversation["project"] == "outside-project"
    assert conversation["models"] == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert conversation["model_calls"] == 3
    assert conversation["tool_calls"] == 1
    assert conversation["input_tokens"] == 123
    assert conversation["uncached_input_tokens"] == 63
    assert conversation["cached_input_tokens"] == 60
    assert conversation["output_tokens"] == 32
    assert conversation["reasoning_output_tokens"] == 7
    assert conversation["visible_output_tokens"] == 25
    assert conversation["total_tokens"] == 161
    assert conversation["unattributed_tokens"] == 6
    assert [turn["status"] for turn in snapshot.turns] == [
        "completed",
        "completed",
        "completed",
    ]
    assert snapshot.tool_calls[0]["tool_name"] == "run_shell_command"
    assert snapshot.tool_calls[0]["turn_id"] == "gemini:session-1:prompt-1"
    assert CANARY not in str(snapshot.to_dict())
    assert "private-project-hash" not in str(snapshot.to_dict())


def test_supports_legacy_json_and_current_checkpoints(tmp_path: Path) -> None:
    home = tmp_path / "gemini"
    path = home / "tmp" / "project" / "chats" / "session-legacy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "sessionId": "legacy-session",
                "startTime": "2026-08-25T10:00:00Z",
                "lastUpdated": "2026-08-25T10:00:01Z",
                "messages": [
                    {
                        "id": "prompt",
                        "timestamp": "2026-08-25T10:00:00Z",
                        "type": "user",
                        "content": CANARY,
                    },
                    {
                        "id": "response",
                        "timestamp": "2026-08-25T10:00:01Z",
                        "type": "gemini",
                        "content": CANARY,
                        "model": "gemini-1.5-pro",
                        "tokens": {"input": 2, "output": 1, "total": 3},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshot = GeminiAdapter().collect([("machine", home)])
    assert snapshot.conversations[0]["source"] == "local-json"
    assert snapshot.conversations[0]["total_tokens"] == 3
    assert CANARY not in str(snapshot.to_dict())

    path.unlink()
    current = home / "tmp" / "project" / "chats" / "session-checkpoint.jsonl"
    messages = [
        {
            "id": "prompt",
            "timestamp": "2026-08-25T10:00:00Z",
            "type": "user",
            "content": CANARY,
        }
    ]
    current.write_text(
        json.dumps({"sessionId": "checkpoint-session", "projectHash": "hash"})
        + "\n"
        + json.dumps({"$set": {"messages": messages}})
        + "\n",
        encoding="utf-8",
    )
    snapshot = GeminiAdapter().collect([("machine", home)])
    assert len(snapshot.turns) == 1
    assert snapshot.turns[0]["external_id"] == "prompt"


def test_deduplicates_copies_and_rejects_malformed_fields(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    session(first)
    path = session(second, extra=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")
        handle.write(
            '{"id":"bad","type":"gemini","model":"'
            + CANARY
            + '","tokens":{"input":Infinity,"output":-1},'
            + '"toolCalls":[{"name":"'
            + CANARY
            + '","args":{"secret":"'
            + CANARY
            + '"}}]}\n'
        )
    snapshot = GeminiAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 2
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.model_calls[-1]["model"] == "unknown"
    assert snapshot.model_calls[-1]["total_tokens"] == 0
    assert len(snapshot.tool_calls) == 1
    assert CANARY not in str(snapshot.to_dict())
    assert (
        snapshot.to_dict()
        == GeminiAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Gemini CLI temporary directory"):
        GeminiAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "gemini"
    session(home)
    snapshot = GeminiAdapter().collect([("machine", home)])
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
