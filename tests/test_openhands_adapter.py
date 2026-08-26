from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli_consumption.adapters.openhands import OpenHandsAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
    read_table,
)

CANARY = "privacy canary secret"


def conversation(home: Path, *, extra: bool = False) -> Path:
    conversation_id = "11111111-2222-3333-4444-555555555555"
    root: dict[str, Any] = {
        "id": conversation_id,
        "execution_status": "finished",
        "workspace": {
            "kind": "LocalWorkspace",
            "working_dir": "/srv/work/acme/service",
            "secret": CANARY,
        },
        "agent": {
            "llm": {
                "model": "anthropic/claude-sonnet-4-6",
                "reasoning_effort": "high",
                "api_key": CANARY,
                "litellm_extra_body": {"secret": CANARY},
            },
            "system_prompt": CANARY,
        },
        "stats": {
            "usage_to_metrics": {
                "default": {
                    "model_name": "anthropic/claude-sonnet-4-6",
                    "accumulated_cost": 99.99,
                    "token_usages": [
                        {
                            "model": "anthropic/claude-sonnet-4-6",
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "cache_read_tokens": 30,
                            "cache_write_tokens": 10,
                            "reasoning_tokens": 5,
                            "context_window": 200000,
                            "response_id": "response-a",
                            "secret": CANARY,
                        },
                        {
                            "model": "anthropic/claude-sonnet-4-6",
                            "prompt_tokens": 5,
                            "completion_tokens": 3,
                            "cache_read_tokens": 10,
                            "cache_write_tokens": 0,
                            "reasoning_tokens": 1,
                            "response_id": "response-b",
                        },
                        {
                            "model": "anthropic/claude-sonnet-4-6",
                            "prompt_tokens": 10,
                            "completion_tokens": 2,
                            "response_id": "response-condense",
                        },
                    ],
                    "costs": [{"cost": 99.99, "secret": CANARY}],
                },
                "secondary": {
                    "model_name": "openai/gpt-5",
                    "token_usages": [
                        {
                            "model": "openai/gpt-5",
                            "prompt_tokens": 20,
                            "completion_tokens": 5,
                            "cache_read_tokens": 5,
                            "reasoning_tokens": 0,
                            "context_window": 128000,
                            "response_id": "response-c",
                        }
                    ],
                },
            }
        },
        "tags": {"secret": CANARY},
        "hook_config": {"command": CANARY},
    }
    base = home / "conversations" / conversation_id / "base_state.json"
    base.parent.mkdir(parents=True)
    base.write_text(json.dumps(root), encoding="utf-8")

    events: list[dict[str, Any]] = [
        {
            "kind": "MessageEvent",
            "id": "prompt-1",
            "timestamp": "2026-08-25T10:00:00Z",
            "source": "user",
            "llm_message": {
                "role": "user",
                "content": [{"type": "text", "text": CANARY}],
            },
            "extended_content": [{"text": CANARY}],
        },
        {
            "kind": "ActionEvent",
            "id": "action-1",
            "timestamp": "2026-08-25T10:00:01Z",
            "source": "agent",
            "llm_response_id": "response-a",
            "tool_name": "terminal",
            "thought": [{"text": CANARY}],
            "action": {"command": CANARY},
            "tool_call": {"arguments": CANARY},
            "summary": CANARY,
        },
        {
            "kind": "ObservationEvent",
            "id": "observation-1",
            "timestamp": "2026-08-25T10:00:02Z",
            "source": "environment",
            "tool_name": "terminal",
            "observation": {"output": CANARY},
        },
        {
            "kind": "MessageEvent",
            "id": "assistant-1",
            "timestamp": "2026-08-25T10:00:03Z",
            "source": "agent",
            "llm_response_id": "response-b",
            "llm_message": {
                "role": "assistant",
                "content": [{"type": "text", "text": CANARY}],
                "reasoning_content": CANARY,
            },
        },
        {
            "kind": "Condensation",
            "id": "condensation-1",
            "timestamp": "2026-08-25T10:00:04Z",
            "source": "environment",
            "llm_response_id": "response-condense",
            "summary": CANARY,
            "forgotten_event_ids": [CANARY],
        },
        {
            "kind": "MessageEvent",
            "id": "prompt-2",
            "timestamp": "2026-08-25T10:00:10Z",
            "source": "user",
            "llm_message": {"role": "user", "content": [{"text": CANARY}]},
        },
        {
            "kind": "ActionEvent",
            "id": "action-2",
            "timestamp": "2026-08-25T10:00:11Z",
            "source": "agent",
            "llm_response_id": "response-c",
            "tool_name": "file_editor",
            "thought": [{"text": CANARY}],
            "action": {"path": CANARY, "patch": CANARY},
        },
    ]
    if extra:
        events.append(
            {
                "kind": "CustomEvent",
                "id": "custom-1",
                "timestamp": "2026-08-25T10:00:12Z",
                "source": "environment",
                "payload": CANARY,
            }
        )
    events_dir = base.parent / "events"
    events_dir.mkdir()
    for index, event in enumerate(events):
        (events_dir / f"event-{index:05d}-{index:08d}.json").write_text(
            json.dumps(event), encoding="utf-8"
        )
    return base.parent


def test_collects_openhands_turns_models_tokens_tools_and_compactions(
    tmp_path: Path,
) -> None:
    home = tmp_path / "openhands"
    conversation(home)

    snapshot = OpenHandsAdapter().collect(
        [("laptop", home)], [("acme", "/srv/work/acme")]
    )

    value = snapshot.conversations[0]
    assert value["id"] == "openhands:11111111-2222-3333-4444-555555555555"
    assert value["source"] == "local-sdk-json-v1"
    assert value["project"] == "acme"
    assert value["started_at"] == "2026-08-25T10:00:00+00:00"
    assert value["ended_at"] == "2026-08-25T10:00:11+00:00"
    assert value["models"] == ["anthropic/claude-sonnet-4-6", "openai/gpt-5"]
    assert value["iterations"] == 2
    assert value["model_calls"] == 4
    assert value["tool_calls"] == 2
    assert value["compactions"] == 1
    assert value["input_tokens"] == 145
    assert value["uncached_input_tokens"] == 90
    assert value["cached_input_tokens"] == 45
    assert value["cache_write_input_tokens"] == 10
    assert value["output_tokens"] == 30
    assert value["reasoning_output_tokens"] == 6
    assert value["visible_output_tokens"] == 24
    assert value["total_tokens"] == 175
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "completed"]
    assert [call["model"] for call in snapshot.model_calls] == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-5",
    ]
    assert [call["tool_name"] for call in snapshot.tool_calls] == [
        "terminal",
        "file_editor",
    ]
    assert snapshot.compaction_events[0]["turn_id"].endswith(":prompt-1")
    assert [sample["context_window_tokens"] for sample in snapshot.context_samples] == [
        200000,
        128000,
    ]
    assert {setting["effort"] for setting in snapshot.turn_settings} == {"high"}
    assert CANARY not in str(snapshot.to_dict())


def test_deduplicates_copies_and_counts_malformed_records(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    conversation(first)
    conversation(second, extra=True)
    events = first / "conversations" / "11111111-2222-3333-4444-555555555555" / "events"
    (events / "event-99999-bad.json").write_text("not-json", encoding="utf-8")
    broken = first / "conversations" / "broken"
    broken.mkdir()
    (broken / "base_state.json").write_text("[]", encoding="utf-8")

    snapshot = OpenHandsAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 2
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["event_count"] == 8
    assert (
        snapshot.to_dict()
        == OpenHandsAdapter()
        .collect([("desktop", first), ("laptop", second)])
        .to_dict()
    )

    with pytest.raises(ValueError, match="Missing OpenHands conversations directory"):
        OpenHandsAdapter().collect([("machine", tmp_path / "missing")])


def test_rejects_unsafe_metadata_and_handles_aggregate_usage(tmp_path: Path) -> None:
    home = tmp_path / "openhands"
    directory = conversation(home)
    root = json.loads((directory / "base_state.json").read_text())
    root["id"] = CANARY
    root["agent"]["llm"]["model"] = CANARY
    root["agent"]["llm"]["reasoning_effort"] = CANARY
    root["workspace"]["working_dir"] = CANARY
    root["stats"]["usage_to_metrics"] = {
        "default": {
            "model_name": CANARY,
            "token_usages": [],
            "accumulated_token_usage": {
                "model": CANARY,
                "prompt_tokens": -1,
                "completion_tokens": float("inf"),
                "cache_read_tokens": 4,
            },
        }
    }
    (directory / "base_state.json").write_text(json.dumps(root), encoding="utf-8")

    snapshot = OpenHandsAdapter().collect([("machine", home)])

    assert snapshot.conversations[0]["external_id"] == directory.name
    assert snapshot.conversations[0]["project"] == "outside-project"
    assert snapshot.conversations[0]["models"] == ["unknown"]
    assert snapshot.conversations[0]["input_tokens"] == 4
    assert all(setting["effort"] is None for setting in snapshot.turn_settings)
    assert CANARY not in str(snapshot.to_dict())


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "openhands"
    conversation(home, extra=True)
    snapshot = OpenHandsAdapter().collect([("machine", home)])
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
