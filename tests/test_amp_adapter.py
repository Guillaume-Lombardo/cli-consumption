from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.amp import AmpAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "privacy canary secret"


def thread(home: Path, *, extra: bool = False) -> Path:
    messages: list[Any] = [
        {
            "role": "user",
            "messageId": 1,
            "timestamp": "2026-08-25T10:00:01Z",
            "content": [{"type": "text", "text": CANARY}],
        },
        {
            "role": "assistant",
            "messageId": 2,
            "content": [
                {"type": "text", "text": CANARY},
                {
                    "type": "tool_use",
                    "id": "tool-secret",
                    "name": "Read",
                    "input": {"path": CANARY},
                },
            ],
            "usage": {
                "model": "claude-sonnet-4-20250514",
                "timestamp": "2026-08-25T10:00:02Z",
                "inputTokens": 10,
                "outputTokens": 20,
                "cacheCreationInputTokens": 30,
                "cacheReadInputTokens": 40,
                "totalInputTokens": 80,
                "maxInputTokens": 200000,
                "credits": CANARY,
            },
            "stopReason": "end_turn",
            "error": CANARY,
        },
        {
            "role": "user",
            "messageId": 3,
            "content": [
                {
                    "type": "tool_result",
                    "toolUseID": "tool-secret",
                    "run": {"status": "done", "result": CANARY},
                }
            ],
        },
        {
            "role": "user",
            "messageId": 4,
            "timestamp": "2026-08-25T10:00:03Z",
            "content": [{"type": "text", "text": CANARY}],
        },
        {
            "role": "assistant",
            "messageId": 5,
            "content": [{"type": "thinking", "thinking": CANARY}],
            "usage": {
                "model": "gpt-5.6-sol",
                "timestamp": "2026-08-25T10:00:04Z",
                "inputTokens": 0,
                "outputTokens": 5,
                "cacheCreationInputTokens": 100,
                "cacheReadInputTokens": 50,
                "totalInputTokens": 150,
                "maxInputTokens": 272000,
            },
            "stop_reason": "error",
        },
    ]
    if extra:
        messages.append({"role": "custom", "payload": CANARY})
    value = {
        "v": 1,
        "id": "T-thread-1",
        "created": 1787652000000,
        "title": CANARY,
        "env": {
            "initial": {
                "cwd": "/srv/work/acme/service",
                "trees": [{"displayName": CANARY}],
                "secret": CANARY,
            }
        },
        "messages": messages,
        "meta": {"traces": [{"endTime": "2026-08-25T10:00:05Z", "payload": CANARY}]},
    }
    path = home / "threads" / "T-thread-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_collects_current_amp_usage_tools_turns_and_context(tmp_path: Path) -> None:
    home = tmp_path / "amp"
    thread(home)

    snapshot = AmpAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "amp:T-thread-1"
    assert conversation["project"] == "acme"
    assert conversation["models"] == [
        "claude-sonnet-4-20250514",
        "gpt-5.6-sol",
    ]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 2
    assert conversation["tool_calls"] == 1
    assert conversation["input_tokens"] == 230
    assert conversation["uncached_input_tokens"] == 110
    assert conversation["cached_input_tokens"] == 90
    assert conversation["cache_write_input_tokens"] == 30
    assert conversation["output_tokens"] == 25
    assert conversation["total_tokens"] == 255
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert snapshot.tool_calls[0]["tool_name"] == "Read"
    assert snapshot.tool_calls[0]["turn_id"] == "amp:T-thread-1:1"
    assert [sample["context_window_tokens"] for sample in snapshot.context_samples] == [
        200000,
        272000,
    ]
    assert CANARY not in str(snapshot.to_dict())


def test_uses_legacy_ledger_without_double_counting_message_usage(
    tmp_path: Path,
) -> None:
    home = tmp_path / "amp"
    path = home / "threads" / "T-legacy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "id": "T-legacy",
                "created": 1787652000000,
                "messages": [
                    {"role": "user", "messageId": 1, "content": "synthetic"},
                    {
                        "role": "assistant",
                        "messageId": 2,
                        "usage": {
                            "model": "claude-sonnet-4",
                            "timestamp": "2026-08-25T10:00:02Z",
                            "inputTokens": 99,
                            "outputTokens": 99,
                            "cacheCreationInputTokens": 3,
                            "cacheReadInputTokens": 4,
                        },
                    },
                ],
                "usageLedger": {
                    "events": [
                        {
                            "id": "event-secret",
                            "timestamp": "2026-08-25T10:00:02Z",
                            "model": "claude-sonnet-4",
                            "tokens": {"input": 2, "output": 5, "total": 20},
                            "toMessageId": 2,
                            "credits": CANARY,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    snapshot = AmpAdapter().collect([("desktop", home)])

    assert len(snapshot.model_calls) == 1
    call = snapshot.model_calls[0]
    assert call["input_tokens"] == 9
    assert call["uncached_input_tokens"] == 2
    assert call["cache_write_input_tokens"] == 3
    assert call["cached_input_tokens"] == 4
    assert call["output_tokens"] == 5
    assert call["total_tokens"] == 20
    assert call["unattributed_tokens"] == 6
    assert call["turn_id"] == "amp:T-legacy:1"
    assert CANARY not in str(snapshot.to_dict())


def test_deduplicates_copies_and_counts_malformed_records(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    thread(first)
    thread(second, extra=True)
    bad = first / "threads" / "T-bad.json"
    bad.write_text("not-json", encoding="utf-8")
    malformed = first / "threads" / "T-malformed.json"
    malformed.write_text(
        json.dumps(
            {
                "id": "T-malformed",
                "messages": [CANARY],
                "usageLedger": {"events": CANARY},
            }
        ),
        encoding="utf-8",
    )

    snapshot = AmpAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 3
    selected = next(
        value
        for value in snapshot.conversations
        if value["external_id"] == "T-thread-1"
    )
    assert selected["source_machine"] == "laptop"
    assert selected["event_count"] == 6
    assert (
        snapshot.to_dict()
        == AmpAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Amp threads directory"):
        AmpAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_from_storage_and_exports(tmp_path: Path) -> None:
    home = tmp_path / "amp"
    thread(home, extra=True)
    snapshot = AmpAdapter().collect([("machine", home)])
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
