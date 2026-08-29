from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from storage_helpers import read_table

from cli_consumption.adapters.grok import GrokAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "grok privacy canary secret"


def _envelope(
    timestamp: int, method: str, update: dict[str, Any], **meta: Any
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "method": method,
        "params": {
            "sessionId": "grok-session-1",
            "update": update,
            "_meta": {
                "eventId": f"event-{timestamp}",
                "totalTokens": 999_999,
                "secret": CANARY,
                **meta,
            },
        },
    }


def session(home: Path, *, extra: bool = False) -> Path:
    directory = home / "sessions" / "%2Fsrv%2Fwork%2Facme" / "grok-session-1"
    directory.mkdir(parents=True)
    summary = {
        "info": {"id": "grok-session-1", "cwd": f"/srv/work/acme/{CANARY}"},
        "session_summary": CANARY,
        "created_at": "2026-08-27T10:00:00Z",
        "updated_at": "2026-08-27T10:00:09Z",
        "last_active_at": "2026-08-27T10:00:08Z",
        "num_messages": 6,
        "current_model_id": "grok-4.6-build",
        "reasoning_effort": "high",
        "git_remotes": [CANARY],
        "generated_title": CANARY,
        "last_turn_summary": CANARY,
    }
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    updates = [
        _envelope(
            1_777_456_800,
            "session/update",
            {
                "sessionUpdate": "user_message_chunk",
                "content": {
                    "type": "text",
                    "text": CANARY,
                    "_meta": {"promptIndex": 0},
                },
            },
            promptId="prompt-1",
            turnStartMs=1_777_456_800_000,
        ),
        _envelope(
            1_777_456_804,
            "_x.ai/session/update",
            {
                "sessionUpdate": "turn_completed",
                "prompt_id": "prompt-1",
                "stop_reason": "end_turn",
                "agent_result": CANARY,
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 25,
                    "cachedReadTokens": 40,
                    "cacheCreationTokens": 10,
                    "reasoningTokens": 5,
                    "totalTokens": 125,
                    "costUsdTicks": 999,
                    "modelUsage": {
                        "grok-4.6-build": {
                            "inputTokens": 100,
                            "outputTokens": 25,
                            "cachedReadTokens": 40,
                            "cacheCreationTokens": 10,
                            "reasoningTokens": 5,
                            "totalTokens": 125,
                            "modelCalls": 2,
                            "costUsdTicks": 999,
                            "secret": CANARY,
                        }
                    },
                },
            },
            promptId="prompt-1",
        ),
        _envelope(
            1_777_456_805,
            "session/update",
            {
                "sessionUpdate": "user_message_chunk",
                "content": {
                    "type": "text",
                    "text": CANARY,
                    "_meta": {"promptIndex": 1},
                },
            },
            promptId="prompt-2",
            turnStartMs=1_777_456_805_000,
        ),
        _envelope(
            1_777_456_807,
            "_x.ai/session/update",
            {
                "sessionUpdate": "auto_compact_completed",
                "tokens_before": 120_000,
                "tokens_after": 20_000,
                "summary_preview": CANARY,
            },
            promptId="prompt-2",
        ),
        _envelope(
            1_777_456_808,
            "_x.ai/session/update",
            {
                "sessionUpdate": "turn_completed",
                "prompt_id": "prompt-2",
                "stop_reason": "cancelled",
                "agent_result": CANARY,
                "usage": {
                    "inputTokens": 3,
                    "outputTokens": 1,
                    "modelUsage": {
                        "grok-4.5-build": {
                            "inputTokens": 3,
                            "outputTokens": 1,
                            "modelCalls": 1,
                        }
                    },
                },
            },
            promptId="prompt-2",
        ),
        _envelope(
            1_777_456_809,
            "_x.ai/session/update",
            {
                "sessionUpdate": "retry_state",
                "type": "failed",
                "message": CANARY,
            },
        ),
    ]
    if extra:
        updates.append(
            _envelope(
                1_777_456_810,
                "_x.ai/session/update",
                {"sessionUpdate": "unknown_future", "content": CANARY},
            )
        )
    (directory / "updates.jsonl").write_text(
        "".join(json.dumps(update) + "\n" for update in updates)
        + f"{{malformed {CANARY}\n",
        encoding="utf-8",
    )

    events = [
        {
            "ts": "2026-04-29T10:00:00.000Z",
            "type": "turn_started",
            "session_id": "grok-session-1",
            "turn_number": 1,
            "model_id": "grok-4.6-build",
        },
        {"ts": "2026-04-29T10:00:00.250Z", "type": "first_token"},
        {
            "ts": "2026-04-29T10:00:01Z",
            "type": "tool_started",
            "tool_name": "read_file",
            "arguments": CANARY,
        },
        {
            "ts": "2026-04-29T10:00:04Z",
            "type": "turn_ended",
            "outcome": "completed",
            "cancellation_context": {"secret": CANARY},
        },
        {
            "ts": "2026-04-29T10:00:05Z",
            "type": "turn_started",
            "session_id": "grok-session-1",
            "turn_number": 2,
            "model_id": "grok-4.5-build",
        },
        {
            "ts": "2026-04-29T10:00:08Z",
            "type": "turn_ended",
            "outcome": "cancelled",
        },
    ]
    (directory / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return directory


def test_collects_grok_build_usage_turns_tools_and_compactions(tmp_path: Path) -> None:
    home = tmp_path / "grok"
    session(home)

    snapshot = GrokAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "grok:grok-session-1"
    assert conversation["project"] == "acme"
    assert conversation["models"] == ["grok-4.5-build", "grok-4.6-build"]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 2
    assert conversation["tool_calls"] == 1
    assert conversation["compactions"] == 1
    assert conversation["input_tokens"] == 103
    assert conversation["uncached_input_tokens"] == 53
    assert conversation["cached_input_tokens"] == 40
    assert conversation["cache_write_input_tokens"] == 10
    assert conversation["output_tokens"] == 26
    assert conversation["reasoning_output_tokens"] == 5
    assert conversation["total_tokens"] == 129
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert snapshot.turns[0]["time_to_first_token_ms"] == 250
    assert snapshot.turns[0]["model_calls"] == 2
    assert snapshot.turn_settings[0]["effort"] == "high"
    assert snapshot.tool_calls[0]["tool_name"] == "read_file"
    assert snapshot.compaction_events[0]["turn_id"].endswith("prompt-2")
    assert snapshot.malformed_records == 1


def test_prefers_more_complete_duplicate_and_keeps_stable_ids(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    session(first)
    session(second, extra=True)

    snapshot = GrokAdapter().collect([("a", first), ("b", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.conversations[0]["source_machine"] == "b"
    assert snapshot.conversations[0]["external_id"] == "grok-session-1"
    assert [turn["external_id"] for turn in snapshot.turns] == [
        "prompt-1",
        "prompt-2",
    ]


def test_privacy_canary_is_absent_and_ingestion_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "grok"
    session(home)
    snapshot = GrokAdapter().collect([("desktop", home)])
    assert CANARY not in str(snapshot.to_dict())

    engine = create_database_engine(str(tmp_path / "usage.sqlite"))
    try:
        first = ingest_snapshot(engine, snapshot)
        second = ingest_snapshot(engine, snapshot)
        assert first.written == 1
        assert second.skipped == 1
        for table in TABLES:
            assert CANARY not in str(read_table(engine, table))

        output = tmp_path / "exports"
        paths = export_csv(engine, output)
        dashboard = output / "dashboard.html"
        generate_dashboard(engine, dashboard)
        for path in [*paths, dashboard]:
            assert CANARY not in path.read_text(encoding="utf-8")
    finally:
        engine.dispose()


def test_rejects_missing_or_malformed_session_metadata(tmp_path: Path) -> None:
    home = tmp_path / "grok"
    sessions = home / "sessions"
    bad = sessions / "encoded" / "bad"
    bad.mkdir(parents=True)
    (bad / "summary.json").write_text(CANARY, encoding="utf-8")
    invalid_id = sessions / "encoded" / "invalid-id"
    invalid_id.mkdir()
    (invalid_id / "summary.json").write_text(
        json.dumps({"info": {"id": CANARY}}), encoding="utf-8"
    )

    snapshot = GrokAdapter().collect([("desktop", home)])

    assert snapshot.conversations == []
    assert snapshot.malformed_records == 2
    assert CANARY not in str(snapshot.to_dict())
