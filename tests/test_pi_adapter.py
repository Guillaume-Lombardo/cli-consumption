from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.pi import PiAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "privacy canary secret"


def session(home: Path, *, extra: bool = False) -> Path:
    entries: list[dict[str, Any]] = [
        {
            "type": "session",
            "version": 3,
            "id": "session-1",
            "timestamp": "2026-08-25T10:00:00Z",
            "cwd": "/srv/work/acme/service",
            "parentSession": CANARY,
        },
        {
            "type": "thinking_level_change",
            "id": "thinking-1",
            "parentId": None,
            "timestamp": "2026-08-25T10:00:00.500Z",
            "thinkingLevel": "high",
        },
        {
            "type": "message",
            "id": "prompt-1",
            "parentId": "thinking-1",
            "timestamp": "2026-08-25T10:00:01Z",
            "message": {"role": "user", "content": CANARY},
        },
        {
            "type": "message",
            "id": "assistant-1",
            "parentId": "prompt-1",
            "timestamp": "2026-08-25T10:00:02Z",
            "message": {
                "role": "assistant",
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "text", "text": CANARY},
                    {
                        "type": "toolCall",
                        "id": "tool-1",
                        "name": "bash",
                        "arguments": {"command": CANARY},
                    },
                ],
                "usage": {
                    "input": 100,
                    "cacheRead": 40,
                    "cacheWrite": 10,
                    "output": 20,
                    "reasoning": 5,
                    "totalTokens": 170,
                    "cost": {"total": 1, "secret": CANARY},
                },
                "stopReason": "toolUse",
                "errorMessage": CANARY,
            },
        },
        {
            "type": "message",
            "id": "result-1",
            "parentId": "assistant-1",
            "timestamp": "2026-08-25T10:00:03Z",
            "message": {
                "role": "toolResult",
                "toolCallId": "tool-1",
                "toolName": "bash",
                "content": [{"type": "text", "text": CANARY}],
                "details": {"output": CANARY},
                "isError": False,
            },
        },
        {
            "type": "message",
            "id": "assistant-2",
            "parentId": "result-1",
            "timestamp": "2026-08-25T10:00:04Z",
            "message": {
                "role": "assistant",
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": CANARY}],
                "usage": {
                    "input": 5,
                    "cacheRead": 100,
                    "cacheWrite": 0,
                    "output": 10,
                    "totalTokens": 115,
                },
                "stopReason": "stop",
            },
        },
        {
            "type": "compaction",
            "id": "compaction-1",
            "parentId": "assistant-2",
            "timestamp": "2026-08-25T10:00:05Z",
            "summary": CANARY,
            "tokensBefore": 50000,
            "details": {"readFiles": [CANARY]},
            "usage": {
                "input": 10,
                "cacheRead": 0,
                "cacheWrite": 0,
                "output": 2,
                "totalTokens": 12,
            },
        },
        {
            "type": "model_change",
            "id": "model-1",
            "parentId": "compaction-1",
            "timestamp": "2026-08-25T10:00:06Z",
            "provider": "openai",
            "modelId": "gpt-5",
        },
        {
            "type": "message",
            "id": "prompt-2",
            "parentId": "model-1",
            "timestamp": "2026-08-25T10:00:10Z",
            "message": {"role": "user", "content": CANARY},
        },
        {
            "type": "message",
            "id": "assistant-3",
            "parentId": "prompt-2",
            "timestamp": "2026-08-25T10:00:11Z",
            "message": {
                "role": "assistant",
                "provider": "openai",
                "model": "gpt-5",
                "content": [{"type": "thinking", "thinking": CANARY}],
                "usage": {
                    "input": 2,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "output": 1,
                    "reasoning": 1,
                    "totalTokens": 3,
                },
                "stopReason": "error",
                "errorMessage": CANARY,
            },
        },
        {
            "type": "message",
            "id": "prompt-branch",
            "parentId": "prompt-1",
            "timestamp": "2026-08-25T10:00:12Z",
            "message": {"role": "user", "content": CANARY},
        },
        {
            "type": "message",
            "id": "assistant-branch",
            "parentId": "prompt-branch",
            "timestamp": "2026-08-25T10:00:13Z",
            "message": {
                "role": "assistant",
                "provider": "google",
                "model": "gemini-2.5-pro",
                "content": [{"type": "text", "text": CANARY}],
                "usage": {
                    "input": 1,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "output": 1,
                    "totalTokens": 2,
                },
                "stopReason": "stop",
            },
        },
    ]
    if extra:
        entries.append(
            {
                "type": "custom",
                "id": "custom-1",
                "parentId": "assistant-branch",
                "timestamp": "2026-08-25T10:00:14Z",
                "customType": "fixture",
                "data": {"secret": CANARY},
            }
        )
    path = home / "sessions" / "--srv-work-acme-service--" / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    return path


def test_collects_usage_tools_branches_settings_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "pi"
    session(home)

    snapshot = PiAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "pi:session-1"
    assert conversation["project"] == "acme"
    assert conversation["models"] == [
        "anthropic/claude-sonnet-4-6",
        "google/gemini-2.5-pro",
        "openai/gpt-5",
    ]
    assert conversation["model_calls"] == 5
    assert conversation["tool_calls"] == 1
    assert conversation["input_tokens"] == 268
    assert conversation["uncached_input_tokens"] == 118
    assert conversation["cached_input_tokens"] == 140
    assert conversation["cache_write_input_tokens"] == 10
    assert conversation["output_tokens"] == 34
    assert conversation["reasoning_output_tokens"] == 6
    assert conversation["visible_output_tokens"] == 28
    assert conversation["total_tokens"] == 302
    assert [turn["status"] for turn in snapshot.turns] == [
        "completed",
        "aborted",
        "completed",
    ]
    assert snapshot.tool_calls[0]["tool_name"] == "bash"
    assert snapshot.tool_calls[0]["turn_id"] == "pi:session-1:prompt-1"
    assert snapshot.compaction_events[0]["turn_id"] == "pi:session-1:prompt-1"
    assert {setting["effort"] for setting in snapshot.turn_settings} == {"high"}
    assert CANARY not in str(snapshot.to_dict())


def test_deduplicates_copies_and_handles_malformed_sessions(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    path = session(first)
    session(second, extra=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")
    invalid = first / "sessions" / "invalid.jsonl"
    invalid.write_text(json.dumps({"type": "message", "content": CANARY}) + "\n")

    snapshot = PiAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 3
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["event_count"] == 13
    assert (
        snapshot.to_dict()
        == PiAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Pi sessions directory"):
        PiAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "pi"
    session(home, extra=True)
    snapshot = PiAdapter().collect([("machine", home)])
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
