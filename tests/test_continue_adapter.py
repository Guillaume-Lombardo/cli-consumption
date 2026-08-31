from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.continue_cli import ContinueAdapter, _tokens
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "privacy canary secret"


def session(home: Path, *, extra: bool = False) -> Path:
    history: list[Any] = [
        {
            "message": {
                "role": "user",
                "content": CANARY,
                "metadata": {"secret": CANARY},
            },
            "contextItems": [{"content": CANARY, "name": CANARY}],
            "editorState": CANARY,
            "appliedRules": [{"rule": CANARY}],
        },
        {
            "message": {
                "role": "assistant",
                "content": CANARY,
                "usage": {
                    "promptTokens": 100,
                    "completionTokens": 40,
                    "promptTokensDetails": {
                        "cachedTokens": 30,
                        "cacheWriteTokens": 20,
                    },
                    "completionTokensDetails": {"reasoningTokens": 10},
                },
                "toolCalls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": CANARY},
                    }
                ],
            },
            "promptLogs": [
                {
                    "modelTitle": "claude-sonnet-4",
                    "modelProvider": "anthropic",
                    "prompt": CANARY,
                    "completion": CANARY,
                }
            ],
            "toolCallStates": [
                {
                    "toolCallId": "call-1",
                    "toolCall": {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": CANARY},
                    },
                    "parsedArgs": {"path": CANARY},
                    "output": [{"content": CANARY}],
                }
            ],
            "reasoning": {"text": CANARY},
        },
        {
            "message": {
                "role": "tool",
                "content": CANARY,
                "toolCallId": "call-1",
            }
        },
        {
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": CANARY}],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": CANARY,
                "toolCalls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": CANARY},
                    }
                ],
            }
        },
    ]
    if extra:
        history.append({"message": {"role": "custom", "content": CANARY}})
    value = {
        "sessionId": "session-1",
        "title": CANARY,
        "workspaceDirectory": "/srv/work/acme/service",
        "chatModelTitle": "gpt-5.6-sol",
        "mode": "agent",
        "history": history,
        "usage": {
            "totalCost": 123.45,
            "promptTokens": 160,
            "completionTokens": 65,
            "promptTokensDetails": {
                "cachedTokens": 50,
                "cacheWriteTokens": 30,
            },
            "completionTokensDetails": {"reasoningTokens": 15},
            "secret": CANARY,
        },
        "secret": CANARY,
    }
    path = home / "sessions" / "session-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    os.utime(path, (1_787_652_005, 1_787_652_005))
    return path


def test_collects_continue_turns_models_tokens_tools_and_project(
    tmp_path: Path,
) -> None:
    home = tmp_path / "continue"
    session(home)

    snapshot = ContinueAdapter().collect(
        [("laptop", home)], [("acme", "/srv/work/acme")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "continue:session-1"
    assert conversation["source"] == "local-json-v1"
    assert conversation["project"] == "acme"
    assert conversation["ended_at"] == "2026-08-25T10:00:05Z"
    assert conversation["models"] == ["anthropic/claude-sonnet-4", "gpt-5.6-sol"]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 3
    assert conversation["tool_calls"] == 2
    assert conversation["input_tokens"] == 240
    assert conversation["uncached_input_tokens"] == 160
    assert conversation["cached_input_tokens"] == 50
    assert conversation["cache_write_input_tokens"] == 30
    assert conversation["output_tokens"] == 65
    assert conversation["reasoning_output_tokens"] == 15
    assert conversation["visible_output_tokens"] == 50
    assert conversation["total_tokens"] == 305
    assert [turn["status"] for turn in snapshot.turns] == [
        "completed",
        "completed",
    ]
    assert [call["model"] for call in snapshot.model_calls] == [
        "anthropic/claude-sonnet-4",
        "gpt-5.6-sol",
        "gpt-5.6-sol",
    ]
    assert snapshot.model_calls[-1]["turn_id"] is None
    assert [call["tool_name"] for call in snapshot.tool_calls] == ["Read", "Bash"]
    assert CANARY not in str(snapshot.to_dict())


def test_normalizes_provider_specific_cache_semantics() -> None:
    usage = {
        "prompt": 100,
        "completion": 20,
        "cached": 30,
        "cache_write": 10,
        "reasoning": 5,
    }

    anthropic = _tokens(usage, "anthropic/claude-sonnet-4")
    openai = _tokens({**usage, "cache_write": 0}, "openai/gpt-5")

    assert anthropic["input_tokens"] == 140
    assert anthropic["uncached_input_tokens"] == 100
    assert anthropic["total_tokens"] == 160
    assert openai["input_tokens"] == 100
    assert openai["uncached_input_tokens"] == 70
    assert openai["total_tokens"] == 120


def test_collects_persisted_compaction_markers(tmp_path: Path) -> None:
    home = tmp_path / "continue"
    path = session(home)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["history"][1]["conversationSummary"] = CANARY
    path.write_text(json.dumps(value), encoding="utf-8")

    snapshot = ContinueAdapter().collect([("machine", home)])

    assert snapshot.conversations[0]["compactions"] == 1
    assert snapshot.compaction_events[0]["turn_id"] == snapshot.turns[0]["id"]
    assert CANARY not in str(snapshot.to_dict())


def test_deduplicates_copies_and_counts_malformed_records(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    session(first)
    session(second, extra=True)
    (first / "sessions" / "bad.json").write_text("not-json", encoding="utf-8")
    (first / "sessions" / "malformed.json").write_text(
        json.dumps(
            {
                "sessionId": "malformed",
                "history": [CANARY, {"message": CANARY}],
            }
        ),
        encoding="utf-8",
    )
    (first / "sessions" / "sessions.json").write_text(CANARY, encoding="utf-8")

    snapshot = ContinueAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 3
    selected = next(
        value for value in snapshot.conversations if value["external_id"] == "session-1"
    )
    assert selected["source_machine"] == "laptop"
    assert selected["event_count"] == 6
    assert (
        snapshot.to_dict()
        == ContinueAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Continue sessions directory"):
        ContinueAdapter().collect([("machine", tmp_path / "missing")])


def test_rejects_unsafe_labels_and_ignores_partial_records(tmp_path: Path) -> None:
    home = tmp_path / "continue"
    path = home / "sessions" / "safe-file-name.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "sessionId": CANARY,
                "chatModelTitle": CANARY,
                "workspaceDirectory": CANARY,
                "history": [
                    {"message": {"role": "user", "content": []}},
                    {
                        "message": {
                            "role": "assistant",
                            "content": CANARY,
                            "usage": {
                                "promptTokens": -1,
                                "completionTokens": float("inf"),
                            },
                            "toolCalls": [
                                {"id": "unsafe", "function": {"name": CANARY}}
                            ],
                        }
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    snapshot = ContinueAdapter().collect([("machine", home)])

    assert snapshot.conversations[0]["external_id"] == "safe-file-name"
    assert snapshot.conversations[0]["models"] == ["unknown"]
    assert snapshot.conversations[0]["input_tokens"] == 0
    assert snapshot.turns == []
    assert snapshot.tool_calls == []
    assert CANARY not in str(snapshot.to_dict())


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "continue"
    session(home, extra=True)
    snapshot = ContinueAdapter().collect([("machine", home)])
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
