from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_consumption.adapters.mistral_vibe import MistralVibeAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
    read_table,
)

CANARY = "mistral vibe privacy canary secret"


def session(home: Path, *, extra: bool = False) -> Path:
    session_dir = home / "logs" / "session" / "session_20260827_vibe0001"
    session_dir.mkdir(parents=True)
    metadata = {
        "session_id": "vibe-session-1",
        "start_time": "2026-08-27T10:00:00Z",
        "end_time": "2026-08-27T10:05:00Z",
        "git_commit": CANARY,
        "git_branch": CANARY,
        "username": CANARY,
        "environment": {
            "working_directory": f"/srv/work/acme/{CANARY}",
            "secret": CANARY,
        },
        "title": CANARY,
        "stats": {
            "steps": 3,
            "session_prompt_tokens": 100,
            "session_completion_tokens": 20,
            "session_cached_tokens": 40,
            "input_price_per_million": 99,
        },
        "config": {
            "active_model": "devstral-medium-latest",
            "api_key": CANARY,
            "system_prompt": CANARY,
        },
        "system_prompt": {"role": "system", "content": CANARY},
        "tools_available": [{"function": {"name": CANARY}}],
    }
    (session_dir / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    messages = [
        {
            "role": "user",
            "message_id": "user-1",
            "content": CANARY,
            "resources": [{"path": CANARY}],
        },
        {
            "role": "assistant",
            "message_id": "assistant-1",
            "content": CANARY,
            "reasoning_content": CANARY,
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"secret": CANARY}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "bash",
            "content": CANARY,
            "tool_result": {"output": CANARY},
        },
        {
            "role": "assistant",
            "injected": True,
            "context_boundary": "compaction",
            "content": CANARY,
        },
        {"role": "user", "message_id": "user-2", "content": CANARY},
        {
            "role": "assistant",
            "content": CANARY,
            "tool_calls": [{"function": {"name": CANARY, "arguments": CANARY}}],
        },
    ]
    if extra:
        messages.append(
            {"role": "assistant", "message_id": "assistant-3", "content": CANARY}
        )
    (session_dir / "messages.jsonl").write_text(
        "".join(json.dumps(message) + "\n" for message in messages),
        encoding="utf-8",
    )
    return session_dir


def test_collects_session_aggregate_usage_turns_tools_and_compactions(
    tmp_path: Path,
) -> None:
    home = tmp_path / "vibe"
    session(home)

    snapshot = MistralVibeAdapter().collect(
        [("laptop", home)], [("acme", "/srv/work/acme")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "mistral-vibe:vibe-session-1"
    assert conversation["project"] == "acme"
    assert conversation["project_source"] == "mapping"
    assert conversation["models"] == ["devstral-medium-latest"]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 1
    assert conversation["tool_calls"] == 1
    assert conversation["compactions"] == 1
    assert conversation["duration_seconds"] == 300
    assert conversation["input_tokens"] == 100
    assert conversation["uncached_input_tokens"] == 60
    assert conversation["cached_input_tokens"] == 40
    assert conversation["output_tokens"] == 20
    assert conversation["visible_output_tokens"] == 20
    assert conversation["total_tokens"] == 120
    assert [turn["status"] for turn in snapshot.turns] == [
        "completed",
        "completed",
    ]
    assert all(turn["model_calls"] == 0 for turn in snapshot.turns)
    assert snapshot.model_calls[0]["turn_id"] is None
    assert snapshot.tool_calls[0]["tool_name"] == "bash"
    assert snapshot.compaction_events[0]["turn_id"] == snapshot.turns[0]["id"]
    assert all(setting["model"] is None for setting in snapshot.turn_settings)
    assert snapshot.malformed_records == 1
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_deduplicates_and_tolerates_malformed_or_partial_sessions(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    session(first)
    path = session(second, extra=True) / "messages.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")

    snapshot = MistralVibeAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 3
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert CANARY not in json.dumps(snapshot.to_dict())
    assert (
        snapshot.to_dict()
        == MistralVibeAdapter()
        .collect([("desktop", first), ("laptop", second)])
        .to_dict()
    )

    partial = tmp_path / "partial"
    partial_dir = partial / "logs" / "session" / "session_partial"
    partial_dir.mkdir(parents=True)
    (partial_dir / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "partial-session",
                "start_time": "2026-08-27T11:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    partial_snapshot = MistralVibeAdapter().collect([("machine", partial)])
    assert partial_snapshot.malformed_records == 1
    assert partial_snapshot.conversations[0]["model_calls"] == 0

    with pytest.raises(ValueError, match="Missing Mistral Vibe session log directory"):
        MistralVibeAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_and_ingestion_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "vibe"
    session(home)
    snapshot = MistralVibeAdapter().collect([("machine", home)])
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
