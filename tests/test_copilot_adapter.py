from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli_consumption.adapters.copilot import CopilotAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
    read_table,
)

CANARY = "copilot privacy canary secret"


def _event(
    event_id: str,
    event_type: str,
    timestamp: str,
    data: dict[str, Any],
    **values: Any,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "timestamp": timestamp,
        "parentId": None,
        "type": event_type,
        "data": data,
        **values,
    }


def session(home: Path, *, extra: bool = False) -> Path:
    events = [
        _event(
            "start-1",
            "session.start",
            "2026-08-27T10:00:00Z",
            {
                "sessionId": "copilot-session-1",
                "version": 1,
                "producer": "copilot-agent",
                "copilotVersion": "1.0.80",
                "startTime": "2026-08-27T10:00:00Z",
                "selectedModel": "gpt-5.4",
                "reasoningEffort": "high",
                "context": {
                    "cwd": f"/srv/work/acme/{CANARY}",
                    "repository": CANARY,
                    "branch": CANARY,
                },
            },
        ),
        _event(
            "prompt-1",
            "user.message",
            "2026-08-27T10:00:01Z",
            {
                "messageId": "message-1",
                "content": CANARY,
                "transformedContent": CANARY,
                "attachments": [{"path": CANARY}],
            },
        ),
        _event(
            "turn-start-1",
            "assistant.turn_start",
            "2026-08-27T10:00:02Z",
            {"turnId": "0", "model": "gpt-5.4"},
        ),
        _event(
            "assistant-1",
            "assistant.message",
            "2026-08-27T10:00:03Z",
            {
                "messageId": "message-2",
                "model": "gpt-5.4",
                "content": CANARY,
                "reasoningText": CANARY,
                "toolRequests": [{"arguments": {"secret": CANARY}}],
            },
        ),
        _event(
            "tool-1",
            "tool.execution_start",
            "2026-08-27T10:00:04Z",
            {
                "toolCallId": "call-1",
                "toolName": "read_file",
                "arguments": {"path": CANARY, "secret": CANARY},
            },
        ),
        _event(
            "tool-result-1",
            "tool.execution_complete",
            "2026-08-27T10:00:05Z",
            {
                "toolCallId": "call-1",
                "success": False,
                "result": {"content": CANARY},
                "error": {"message": CANARY},
            },
        ),
        _event(
            "turn-end-1",
            "assistant.turn_end",
            "2026-08-27T10:00:06Z",
            {"turnId": "0"},
        ),
        _event(
            "prompt-2",
            "user.message",
            "2026-08-27T10:00:07Z",
            {"messageId": "message-3", "content": CANARY},
        ),
        _event(
            "assistant-2",
            "assistant.message",
            "2026-08-27T10:00:08Z",
            {
                "messageId": "message-4",
                "model": "claude-sonnet-4.6",
                "content": CANARY,
            },
        ),
        _event(
            "subagent-tool",
            "tool.execution_start",
            "2026-08-27T10:00:08.500Z",
            {
                "toolCallId": "secret-call",
                "toolName": CANARY,
                "arguments": {"secret": CANARY},
            },
            agentId="subagent-secret",
        ),
        _event(
            "compact-1",
            "session.compaction_complete",
            "2026-08-27T10:00:09Z",
            {
                "success": True,
                "summaryContent": CANARY,
                "customInstructions": CANARY,
                "checkpointPath": CANARY,
            },
        ),
        _event(
            "turn-end-2",
            "assistant.turn_end",
            "2026-08-27T10:00:10Z",
            {"turnId": "1"},
        ),
        _event(
            "shutdown-old",
            "session.shutdown",
            "2026-08-27T10:00:11Z",
            {
                "shutdownType": "routine",
                "errorReason": CANARY,
                "modelMetrics": {
                    "gpt-5.4": {
                        "requests": {"count": 1, "cost": 99},
                        "usage": {
                            "inputTokens": 999,
                            "outputTokens": 999,
                            "cacheReadTokens": 0,
                            "cacheWriteTokens": 0,
                        },
                    }
                },
                "codeChanges": {"files": [CANARY]},
            },
        ),
        _event(
            "shutdown-latest",
            "session.shutdown",
            "2026-08-27T10:00:12Z",
            {
                "shutdownType": "routine",
                "totalPremiumRequests": 2,
                "totalNanoAiu": 123456,
                "modelMetrics": {
                    "gpt-5.4": {
                        "requests": {"count": 2, "cost": 1},
                        "usage": {
                            "inputTokens": 100,
                            "outputTokens": 20,
                            "cacheReadTokens": 40,
                            "cacheWriteTokens": 10,
                            "reasoningTokens": 5,
                        },
                        "tokenDetails": {"secret": {"value": CANARY}},
                    },
                    "claude-sonnet-4.6": {
                        "requests": {"count": 1, "cost": 1},
                        "usage": {
                            "inputTokens": 50,
                            "outputTokens": 10,
                            "cacheReadTokens": 30,
                            "cacheWriteTokens": 0,
                        },
                    },
                },
                "codeChanges": {"files": [CANARY]},
            },
        ),
    ]
    if extra:
        events.append(
            _event(
                "title-1",
                "session.title_changed",
                "2026-08-27T10:00:13Z",
                {"title": CANARY},
            )
        )
    path = home / "session-state" / "copilot-session-1" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def test_collects_turns_aggregate_usage_tools_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "copilot"
    session(home)

    snapshot = CopilotAdapter().collect(
        [("laptop", home)], [("acme", "/srv/work/acme")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "copilot:copilot-session-1"
    assert conversation["project"] == "acme"
    assert conversation["project_source"] == "mapping"
    assert conversation["models"] == ["claude-sonnet-4.6", "gpt-5.4"]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 2
    assert conversation["tool_calls"] == 1
    assert conversation["compactions"] == 1
    assert conversation["input_tokens"] == 150
    assert conversation["uncached_input_tokens"] == 70
    assert conversation["cached_input_tokens"] == 70
    assert conversation["cache_write_input_tokens"] == 10
    assert conversation["output_tokens"] == 30
    assert conversation["reasoning_output_tokens"] == 5
    assert conversation["visible_output_tokens"] == 25
    assert conversation["total_tokens"] == 180
    assert [turn["status"] for turn in snapshot.turns] == [
        "completed",
        "completed",
    ]
    assert all(turn["model_calls"] == 0 for turn in snapshot.turns)
    assert all(call["turn_id"] is None for call in snapshot.model_calls)
    assert snapshot.tool_calls[0]["tool_name"] == "read_file"
    assert snapshot.compaction_events[0]["turn_id"] == (
        "copilot:copilot-session-1:prompt-2"
    )
    assert [setting["model"] for setting in snapshot.turn_settings] == [
        "gpt-5.4",
        "claude-sonnet-4.6",
    ]
    assert all(setting["effort"] == "high" for setting in snapshot.turn_settings)
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_deduplicates_and_tolerates_malformed_or_partial_sessions(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    session(first)
    path = session(second, extra=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")
        handle.write(json.dumps({"type": "user.message", "data": CANARY}) + "\n")

    snapshot = CopilotAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 3
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert CANARY not in json.dumps(snapshot.to_dict())
    assert (
        snapshot.to_dict()
        == CopilotAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    minimal = tmp_path / "minimal"
    minimal_path = minimal / "session-state" / "minimal-session" / "events.jsonl"
    minimal_path.parent.mkdir(parents=True)
    minimal_path.write_text(
        json.dumps(
            _event(
                "minimal-start",
                "session.start",
                "2026-08-27T11:00:00Z",
                {
                    "sessionId": "minimal-session",
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.80",
                    "startTime": "2026-08-27T11:00:00Z",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    partial = CopilotAdapter().collect([("machine", minimal)])
    assert partial.conversations[0]["model_calls"] == 0
    assert partial.conversations[0]["input_tokens"] == 0

    with pytest.raises(
        ValueError, match="Missing GitHub Copilot CLI session-state directory"
    ):
        CopilotAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_and_ingestion_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "copilot"
    session(home)
    snapshot = CopilotAdapter().collect([("machine", home)])
    assert CANARY not in json.dumps(snapshot.to_dict())

    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        first = ingest_snapshot(engine, snapshot)
        second = ingest_snapshot(engine, snapshot)
        assert (first.written, first.skipped) == (1, 0)
        assert (second.written, second.skipped) == (0, 1)
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
