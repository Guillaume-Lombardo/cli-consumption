from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli_consumption.adapters.claude import ClaudeAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
    read_table,
)


def transcript(home: Path, *, extra: bool = False) -> Path:
    events: list[dict[str, Any]] = [
        {
            "type": "user",
            "sessionId": "session-1",
            "uuid": "prompt-1",
            "cwd": "/srv/work/acme/service",
            "timestamp": "2026-08-25T10:00:00Z",
            "message": {"role": "user", "content": "privacy canary"},
        },
        {
            "type": "assistant",
            "sessionId": "session-1",
            "requestId": "request-1",
            "timestamp": "2026-08-25T10:00:01Z",
            "message": {
                "id": "message-1",
                "model": "claude-sonnet-4-5",
                "stop_reason": None,
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": 1,
                },
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "privacy canary"},
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": "session-1",
            "requestId": "request-1",
            "timestamp": "2026-08-25T10:00:02Z",
            "message": {
                "id": "message-1",
                "model": "claude-sonnet-4-5",
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 10,
                    "output_tokens": 20,
                },
                "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash"}],
            },
        },
        {
            "type": "user",
            "sessionId": "session-1",
            "uuid": "result-1",
            "toolUseResult": {"stdout": "privacy canary"},
            "timestamp": "2026-08-25T10:00:03Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "privacy canary"}],
            },
        },
        {
            "type": "assistant",
            "sessionId": "session-1",
            "requestId": "request-2",
            "timestamp": "2026-08-25T10:00:04Z",
            "message": {
                "id": "message-2",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "output_tokens": 10,
                },
                "content": [{"type": "text", "text": "privacy canary"}],
            },
        },
        {
            "type": "user",
            "sessionId": "session-1",
            "uuid": "prompt-2",
            "timestamp": "2026-08-25T10:00:10Z",
            "message": {"role": "user", "content": "privacy canary"},
        },
        {
            "type": "assistant",
            "sessionId": "session-1",
            "requestId": "request-3",
            "isApiErrorMessage": True,
            "timestamp": "2026-08-25T10:00:11Z",
            "message": {
                "id": "message-3",
                "model": "claude-haiku-4-5",
                "stop_reason": "stop_sequence",
                "usage": {"input_tokens": 2, "output_tokens": 0},
                "content": [{"type": "text", "text": "privacy canary"}],
            },
        },
    ]
    if extra:
        events.append(
            {
                "type": "system",
                "subtype": "compact_boundary",
                "sessionId": "session-1",
                "timestamp": "2026-08-25T10:00:12Z",
                "content": "privacy canary",
            }
        )
    path = home / "projects" / "-srv-work-acme-service" / "session-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def test_collects_usage_tools_turns_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    transcript(home, extra=True)
    snapshot = ClaudeAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "claude:session-1"
    assert conversation["project"] == "acme"
    assert conversation["models"] == ["claude-haiku-4-5", "claude-sonnet-4-5"]
    assert conversation["model_calls"] == 3
    assert conversation["tool_calls"] == 1
    assert conversation["input_tokens"] == 257
    assert conversation["uncached_input_tokens"] == 107
    assert conversation["cached_input_tokens"] == 140
    assert conversation["cache_write_input_tokens"] == 10
    assert conversation["output_tokens"] == 30
    assert conversation["total_tokens"] == 287
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert snapshot.tool_calls[0]["tool_name"] == "Bash"
    assert snapshot.compaction_events[0]["turn_id"] == "claude:session-1:prompt-2"
    assert "privacy canary" not in str(snapshot.to_dict())


def test_deduplicates_streaming_fragments_and_copied_sessions(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    transcript(first)
    transcript(second, extra=True)
    snapshot = ClaudeAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert len(snapshot.model_calls) == 3
    assert snapshot.model_calls[0]["output_tokens"] == 20
    assert len(snapshot.tool_calls) == 1
    assert (
        snapshot.to_dict()
        == ClaudeAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )


def test_malformed_records_and_missing_directory_are_handled(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    path = transcript(home)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")
        handle.write(
            '{"type":"assistant","message":{"model":"privacy canary",'
            '"usage":{"input_tokens":Infinity,"output_tokens":-1},'
            '"content":[{"type":"tool_use","name":"privacy canary"}]}}\n'
        )
    snapshot = ClaudeAdapter().collect([("machine", home)])
    assert snapshot.malformed_records == 2
    assert snapshot.model_calls[-1]["model"] == "unknown"
    assert snapshot.model_calls[-1]["total_tokens"] == 0
    assert len(snapshot.tool_calls) == 1
    assert "privacy canary" not in str(snapshot.to_dict())

    with pytest.raises(ValueError, match="Missing Claude Code projects directory"):
        ClaudeAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    transcript(home, extra=True)
    snapshot = ClaudeAdapter().collect([("machine", home)])
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        ingest_snapshot(engine, snapshot)
        rows = {name: read_table(engine, name) for name in TABLES}
        assert "privacy canary" not in json.dumps(rows)
        output = tmp_path / "reports"
        paths = export_csv(engine, output)
        dashboard = output / "dashboard.html"
        generate_dashboard(engine, dashboard)
        assert all("privacy canary" not in path.read_text() for path in paths)
        assert "privacy canary" not in dashboard.read_text()
    finally:
        engine.dispose()
