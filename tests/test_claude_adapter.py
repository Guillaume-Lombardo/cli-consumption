from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters._shared import ProviderDataLimitError
from cli_consumption.adapters.claude import ClaudeAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
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


def test_each_transcript_is_charged_to_the_read_budget_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    on_disk = transcript(first).stat().st_size
    on_disk += transcript(second, extra=True).stat().st_size
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_READ_BYTES", on_disk
    )

    snapshot = ClaudeAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["compactions"] == 1

    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_READ_BYTES", on_disk - 1
    )
    with pytest.raises(ProviderDataLimitError, match="provider_read_limit_exceeded"):
        ClaudeAdapter().collect([("desktop", first), ("laptop", second)])


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
    nested = home / "projects" / "-srv-work-acme-service" / "session-1" / "subagents"
    _write_jsonl(nested / "agent-canary.jsonl", _agent_events("canary", "m-c"))
    (nested / "agent-canary.meta.json").write_text(
        json.dumps({"agentType": "privacy canary", "description": "privacy canary"}),
        encoding="utf-8",
    )
    snapshot = ClaudeAdapter().collect([("machine", home)])
    assert snapshot.subagents[0]["agent_role"] == "other"
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


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def _agent_events(
    agent_id: str | None,
    message_id: str,
    *,
    session_id: str = "session-1",
    tokens: int = 7,
) -> list[dict[str, Any]]:
    identity = {"sessionId": session_id, "isSidechain": True}
    if agent_id is not None:
        identity["agentId"] = agent_id
    return [
        {
            **identity,
            "type": "user",
            "uuid": f"{message_id}-prompt",
            "cwd": "/srv/work/acme/service",
            "timestamp": "2026-08-25T10:00:05Z",
            "message": {"role": "user", "content": "privacy canary"},
        },
        {
            **identity,
            "type": "assistant",
            "requestId": f"{message_id}-request",
            "timestamp": "2026-08-25T10:00:06Z",
            "message": {
                "id": message_id,
                "model": "claude-haiku-4-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": tokens, "output_tokens": 3},
                "content": [
                    {"type": "tool_use", "id": f"{message_id}-tool", "name": "Grep"}
                ],
            },
        },
    ]


def test_collects_nested_and_legacy_subagent_transcripts(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    transcript(home)
    project = home / "projects" / "-srv-work-acme-service"
    nested = project / "session-1" / "subagents"
    _write_jsonl(nested / "agent-explorer.jsonl", _agent_events("explorer", "m-a"))
    (nested / "agent-explorer.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "privacy canary"}),
        encoding="utf-8",
    )
    _write_jsonl(
        nested / "workflows" / "wf_1" / "agent-flow.jsonl",
        _agent_events(None, "m-b"),
    )
    (nested / "workflows" / "wf_1" / "agent-flow.meta.json").write_text(
        "not-json", encoding="utf-8"
    )
    (nested / "agent-explorer.jsonl").with_name("agent-big.meta.json").write_text(
        json.dumps({"agentType": "Plan", "padding": "x" * 70_000}), encoding="utf-8"
    )
    _write_jsonl(nested / "agent-big.jsonl", _agent_events("big", "m-d"))
    _write_jsonl(project / "agent-legacy.jsonl", _agent_events("legacy", "m-c"))

    snapshot = ClaudeAdapter().collect([("desktop", home)])

    conversations = {row["external_id"]: row for row in snapshot.conversations}
    assert set(conversations) == {
        "session-1",
        "session-1:agent:explorer",
        "session-1:agent:flow",
        "session-1:agent:legacy",
        "session-1:agent:big",
    }
    assert snapshot.duplicate_conversations == 0
    assert conversations["session-1"]["model_calls"] == 3
    child = conversations["session-1:agent:explorer"]
    assert child["iterations"] == 1
    assert child["model_calls"] == 1
    assert child["tool_calls"] == 1
    assert child["total_tokens"] == 10
    edges = {row["child_thread_id"]: row for row in snapshot.subagents}
    assert set(edges) == set(conversations) - {"session-1"}
    assert {row["parent_thread_id"] for row in edges.values()} == {"session-1"}
    assert edges["session-1:agent:explorer"]["agent_role"] == "research"
    assert edges["session-1:agent:flow"]["agent_role"] == "unspecified"
    assert edges["session-1:agent:legacy"]["agent_role"] == "unspecified"
    assert edges["session-1:agent:big"]["agent_role"] == "unspecified"
    assert edges["session-1:agent:explorer"]["status"] == "completed"
    assert edges["session-1:agent:explorer"]["tokens_used"] == 10
    assert edges["session-1:agent:explorer"]["created_at_ms"] == 1787652005000
    assert "privacy canary" not in str(snapshot.to_dict())


def test_legacy_agent_parent_ignores_directories_above_projects(
    tmp_path: Path,
) -> None:
    home = tmp_path / "subagents" / "claude"
    transcript(home)
    project = home / "projects" / "-srv-work-acme-service"
    _write_jsonl(project / "agent-legacy.jsonl", _agent_events("legacy", "m-l"))

    snapshot = ClaudeAdapter().collect([("desktop", home)])

    assert [row["parent_thread_id"] for row in snapshot.subagents] == ["session-1"]


def test_sidechain_replays_of_parent_responses_are_not_counted(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude"
    transcript(home)
    nested = home / "projects" / "-srv-work-acme-service" / "session-1" / "subagents"
    replay = _agent_events("aside", "message-2", tokens=100_000)
    replay += _agent_events("aside", "aside-answer")[1:]
    _write_jsonl(nested / "agent-aside.jsonl", replay)

    snapshot = ClaudeAdapter().collect([("desktop", home)])

    child = next(
        row
        for row in snapshot.conversations
        if row["external_id"] == "session-1:agent:aside"
    )
    assert child["model_calls"] == 1
    assert child["total_tokens"] == 10


def test_subagent_graph_is_ingested_idempotently_without_content(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude"
    transcript(home)
    nested = home / "projects" / "-srv-work-acme-service" / "session-1" / "subagents"
    _write_jsonl(nested / "agent-worker.jsonl", _agent_events("worker", "m-w"))
    (nested / "agent-worker.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "privacy canary"}),
        encoding="utf-8",
    )
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        first = ingest_snapshot(engine, ClaudeAdapter().collect([("desktop", home)]))
        second = ingest_snapshot(engine, ClaudeAdapter().collect([("desktop", home)]))
        subagents = read_table(engine, "subagents")
        rows = {name: read_table(engine, name) for name in TABLES}
    finally:
        engine.dispose()

    assert (first.written, second.written, second.skipped) == (2, 0, 2)
    assert [(row["child_thread_id"], row["agent_role"]) for row in subagents] == [
        ("session-1:agent:worker", "worker")
    ]
    assert "privacy canary" not in json.dumps(rows, default=str)


def test_subagent_transcripts_without_identity_or_through_symlinks_are_refused(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude"
    transcript(home)
    project = home / "projects" / "-srv-work-acme-service"
    _write_jsonl(project / "agent-.jsonl", _agent_events(None, "m-x"))

    snapshot = ClaudeAdapter().collect([("desktop", home)])
    assert snapshot.malformed_records == 1
    assert [row["external_id"] for row in snapshot.conversations] == ["session-1"]

    nested = project / "session-1" / "subagents"
    target = _write_jsonl(tmp_path / "outside.jsonl", _agent_events("x", "m-y"))
    nested.mkdir(parents=True)
    (nested / "agent-x.jsonl").symlink_to(target)
    with pytest.raises(ProviderDataLimitError, match="symlink"):
        ClaudeAdapter().collect([("desktop", home)])


def test_legacy_sidechain_replay_uses_winning_parent_copy(tmp_path: Path) -> None:
    small, large = tmp_path / "small", tmp_path / "large"
    transcript(small)
    winning = transcript(large, extra=True)
    # The losing copy contains an ID not present in the winning copy.
    path = small / "projects" / "-srv-work-acme-service" / "session-1.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[1]["message"]["id"] = "losing-only"
    _write_jsonl(path, events)
    assert winning.exists()
    legacy = large / "projects" / "-srv-work-acme-service" / "agent-aside.jsonl"
    _write_jsonl(
        legacy,
        _agent_events("aside", "message-2", tokens=100_000)
        + _agent_events("aside", "losing-only")[1:],
    )
    snapshot = ClaudeAdapter().collect([("small", small), ("large", large)])
    child = next(
        row for row in snapshot.conversations if ":agent:" in row["external_id"]
    )
    assert child["model_calls"] == 1
    assert child["total_tokens"] == 10
    assert snapshot.duplicate_conversations == 1


def test_agent_relationship_counts_against_retained_record_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_consumption.models import SnapshotValidationError

    home = tmp_path / "claude"
    project = home / "projects" / "p"
    _write_jsonl(project / "parent.jsonl", [{"sessionId": "parent"}])
    _write_jsonl(project / "parent" / "subagents" / "agent-a.jsonl", [{"agentId": "a"}])
    _write_jsonl(project / "parent" / "subagents" / "agent-b.jsonl", [{"agentId": "b"}])
    monkeypatch.setattr("cli_consumption.models.MAX_SNAPSHOT_RECORDS", 3)
    with pytest.raises(SnapshotValidationError, match="snapshot_too_large"):
        ClaudeAdapter().collect([("machine", home)])


def test_parent_response_identity_cache_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "p"
    _write_jsonl(
        project / "parent.jsonl",
        [
            {
                "sessionId": "parent",
                "type": "assistant",
                "message": {"id": f"m-{index}"},
            }
            for index in range(4)
        ],
    )
    _write_jsonl(project / "parent" / "subagents" / "agent-a.jsonl", [{"agentId": "a"}])
    monkeypatch.setattr("cli_consumption.models.MAX_SNAPSHOT_RECORDS", 3)
    with pytest.raises(ProviderDataLimitError, match="provider_record_limit_exceeded"):
        ClaudeAdapter().collect([("machine", home)])


def test_nested_agents_truncated_transcript_and_richer_graph_replacement(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude"
    parent = transcript(home)
    nested = parent.parent / "session-1" / "subagents"
    agent = _write_jsonl(nested / "agent-a.jsonl", _agent_events("a", "m-a"))
    _write_jsonl(
        nested / "workflows" / "wf" / "agent-b.jsonl", _agent_events("b", "m-b")
    )
    (nested / "agent-a.meta.json").write_text(
        json.dumps({"agentType": "Plan", "spawnDepth": 2})
    )
    with agent.open("a") as handle:
        handle.write('{"type":')
    snapshot = ClaudeAdapter().collect([("machine", home)])
    assert snapshot.malformed_records == 1
    assert len(snapshot.subagents) == 2
    assert snapshot.subagents[0]["agent_role"] == "planning"
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        ingest_snapshot(engine, snapshot)
        # Remove an agent from a demonstrably richer authoritative collection.
        agent.unlink()
        with parent.open("a") as handle:
            handle.write(json.dumps({"type": "system", "subtype": "compact"}) + "\n")
        ingest_snapshot(engine, ClaudeAdapter().collect([("machine", home)]))
        assert [row["child_thread_id"] for row in read_table(engine, "subagents")] == [
            "session-1:agent:b"
        ]
    finally:
        engine.dispose()


def test_agent_metadata_symlink_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    parent = transcript(home)
    nested = parent.parent / "session-1" / "subagents"
    _write_jsonl(nested / "agent-a.jsonl", _agent_events("a", "m-a"))
    target = tmp_path / "metadata.json"
    target.write_text(
        json.dumps({"agentType": "Explore", "description": "privacy canary"})
    )
    (nested / "agent-a.meta.json").symlink_to(target)
    with pytest.raises(ProviderDataLimitError, match="symlink"):
        ClaudeAdapter().collect([("machine", home)])


def test_legacy_agent_without_parent_identity_is_skipped(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    parent = transcript(home)
    _write_jsonl(parent.parent / "agent-orphan.jsonl", [{"agentId": "orphan"}])
    snapshot = ClaudeAdapter().collect([("machine", home)])
    assert snapshot.malformed_records == 1
    assert not snapshot.subagents
    assert [row["external_id"] for row in snapshot.conversations] == ["session-1"]


def test_duplicate_agent_replaces_without_double_charging_and_reads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_consumption.adapters import claude

    sources = []
    for index in range(3):
        home = tmp_path / str(index)
        project = home / "projects" / "p"
        _write_jsonl(project / "parent.jsonl", [{"sessionId": "parent"}])
        _write_jsonl(
            project / "parent" / "subagents" / "agent-a.jsonl",
            [{"agentId": "a"}, *({"ignored": "privacy canary"} for _ in range(index))],
        )
        sources.append((str(index), home))
    monkeypatch.setattr("cli_consumption.models.MAX_SNAPSHOT_RECORDS", 3)
    original = claude.iter_bounded_jsonl_bytes
    reads = []

    def counted(path, budget):
        reads.append(path)
        yield from original(path, budget)

    monkeypatch.setattr(claude, "iter_bounded_jsonl_bytes", counted)
    snapshot = ClaudeAdapter().collect(sources)
    assert len(reads) == len(set(reads)) == 6
    assert snapshot.duplicate_conversations == 4
    assert len(snapshot.conversations) == 2
    assert snapshot.subagents[0]["source_machine"] == "2"
    assert "privacy canary" not in str(snapshot.to_dict())


def test_project_named_subagents_is_not_an_agent_transcript(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    _write_jsonl(
        home / "projects" / "subagents" / "session.jsonl", [{"sessionId": "parent"}]
    )
    snapshot = ClaudeAdapter().collect([("machine", home)])
    assert [row["external_id"] for row in snapshot.conversations] == ["parent"]
    assert not snapshot.subagents
