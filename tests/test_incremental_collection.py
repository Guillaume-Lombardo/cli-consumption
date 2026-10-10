from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table
from test_amp_adapter import thread as amp_thread
from test_claude_adapter import _agent_events, _write_jsonl, transcript
from test_continue_adapter import session as continue_session
from test_gemini_adapter import session as gemini_session
from test_pi_adapter import session as pi_session
from test_qwen_adapter import session as qwen_session
from typer.testing import CliRunner

import cli_consumption.adapters._incremental as incremental_module
import cli_consumption.adapters.claude as claude_module
from cli_consumption.adapters._incremental import (
    bounded_sorted_paths,
    is_aggregate_limit,
    iter_collection_batches,
)
from cli_consumption.adapters._shared import ProviderDataLimitError
from cli_consumption.adapters.amp import AmpAdapter
from cli_consumption.adapters.base import CollectionBatch, IncrementalAdapter
from cli_consumption.adapters.claude import ClaudeAdapter
from cli_consumption.adapters.continue_cli import ContinueAdapter
from cli_consumption.adapters.gemini import GeminiAdapter
from cli_consumption.adapters.pi import PiAdapter
from cli_consumption.adapters.qwen import QwenAdapter
from cli_consumption.adapters.registry import ADAPTER_SPECS
from cli_consumption.cli import app
from cli_consumption.models import Snapshot, SnapshotValidationError
from cli_consumption.storage import TABLES, create_database_engine, ingest_snapshot

runner = CliRunner()
CANARY = "privacy canary"
GRAPH_TABLES = [name for name in TABLES if name != "ingestion_runs"]


def _session_events(session_id: str, message_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "sessionId": session_id,
            "uuid": f"{message_id}-prompt",
            "cwd": "/srv/work/acme/service",
            "timestamp": "2026-08-26T09:00:00Z",
            "message": {"role": "user", "content": CANARY},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "requestId": f"{message_id}-request",
            "timestamp": "2026-08-26T09:00:01Z",
            "message": {
                "id": message_id,
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 2},
                "content": [{"type": "text", "text": CANARY}],
            },
        },
    ]


def _claude_store(home: Path, *, extra: bool = False) -> Path:
    """Two projects with nested, workflow, and legacy sidechain transcripts."""
    parent = transcript(home, extra=extra)
    project = parent.parent
    nested = project / "session-1" / "subagents"
    _write_jsonl(nested / "agent-explorer.jsonl", _agent_events("explorer", "m-a"))
    (nested / "agent-explorer.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": CANARY}), encoding="utf-8"
    )
    _write_jsonl(
        nested / "workflows" / "wf_1" / "agent-flow.jsonl",
        _agent_events(None, "m-b"),
    )
    # The legacy transcript replays a parent response that must not count twice.
    _write_jsonl(
        project / "agent-legacy.jsonl",
        _agent_events("legacy", "message-2", tokens=100_000)
        + _agent_events("legacy", "m-c")[1:],
    )
    other = home / "projects" / "p2"
    _write_jsonl(other / "beta.jsonl", _session_events("beta", "m-beta"))
    _write_jsonl(
        other / "beta" / "subagents" / "agent-helper.jsonl",
        _agent_events("helper", "m-helper", session_id="beta"),
    )
    _write_jsonl(
        other / "agent-old.jsonl",
        _agent_events("old", "m-beta", session_id="beta", tokens=50_000)
        + _agent_events("old", "m-old", session_id="beta")[1:],
    )
    return home


def _tables(engine) -> dict[str, list[dict[str, Any]]]:
    return {name: read_table(engine, name) for name in GRAPH_TABLES}


def _ingest_batches(engine, batches: list[CollectionBatch]) -> None:
    for batch in batches:
        ingest_snapshot(
            engine,
            batch.snapshot,
            authoritative_subagent_scopes=batch.authoritative_subagent_scopes,
            subagent_merge=batch.subagent_merge,
        )


def _external_ids(batch: CollectionBatch) -> set[str]:
    return {row["external_id"] for row in batch.snapshot.conversations}


def test_generic_batches_never_split_groups_and_split_only_aggregate_limits() -> None:
    calls: list[list[int]] = []

    def collect(items: list[int]) -> Snapshot:
        calls.append(items)
        if len(items) > 3:
            raise ProviderDataLimitError("provider_read_limit_exceeded")
        return Snapshot(provider="synthetic")

    batches = list(
        iter_collection_batches(
            [[1, 2], [], [3], [4, 5], [6]], collect, candidates_per_batch=4
        )
    )

    assert len(batches) == 2
    assert calls == [[1, 2, 3], [4, 5, 6]]
    assert all(batch.authoritative_subagent_scopes == frozenset() for batch in batches)

    calls.clear()
    split = list(
        iter_collection_batches([[1, 2], [3, 4], [5]], collect, candidates_per_batch=10)
    )
    assert len(split) == 2
    assert calls == [[1, 2, 3, 4, 5], [1, 2], [3, 4, 5]]

    with pytest.raises(ProviderDataLimitError, match="read_limit"):
        list(iter_collection_batches([[1, 2, 3, 4]], collect, candidates_per_batch=1))

    def unsafe(_items: list[int]) -> Snapshot:
        raise ProviderDataLimitError("provider_line_too_large")

    with pytest.raises(ProviderDataLimitError, match="line_too_large"):
        list(iter_collection_batches([[1], [2]], unsafe, candidates_per_batch=10))


def test_aggregate_limit_classification_and_listing_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert is_aggregate_limit(ProviderDataLimitError("provider_read_limit_exceeded"))
    assert is_aggregate_limit(ProviderDataLimitError("provider_record_limit_exceeded"))
    assert is_aggregate_limit(SnapshotValidationError("snapshot_too_large"))
    assert not is_aggregate_limit(ProviderDataLimitError("provider_file_too_large"))
    assert not is_aggregate_limit(
        ProviderDataLimitError("provider_sqlite_file_too_large")
    )
    assert not is_aggregate_limit(SnapshotValidationError("invalid_snapshot"))
    assert not is_aggregate_limit(ValueError("provider_read_limit_exceeded"))

    for name in ("b", "a"):
        (tmp_path / name).touch()
    assert bounded_sorted_paths(tmp_path.iterdir()) == [tmp_path / "a", tmp_path / "b"]
    monkeypatch.setattr(incremental_module, "MAX_INCREMENTAL_LISTING", 1)
    with pytest.raises(ProviderDataLimitError, match="listing_limit"):
        bounded_sorted_paths(tmp_path.iterdir())


def test_registry_marks_only_rank_compatible_adapters_incremental() -> None:
    capable = {
        spec.name
        for spec in ADAPTER_SPECS
        if isinstance(spec.adapter_type(), IncrementalAdapter)
    }
    assert capable == {"amp", "claude", "codex", "continue", "gemini", "pi", "qwen"}


def test_claude_batches_keep_each_session_with_its_subagents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _claude_store(tmp_path / "claude")
    monkeypatch.setattr(claude_module, "INCREMENTAL_CANDIDATES_PER_BATCH", 1)

    batches = list(ClaudeAdapter().collect_incrementally([("desktop", home)]))

    assert [sorted(_external_ids(batch)) for batch in batches] == [
        ["session-1", "session-1:agent:explorer", "session-1:agent:flow"],
        ["session-1:agent:legacy"],
        ["beta", "beta:agent:helper"],
        ["beta:agent:old"],
    ]
    assert all(batch.subagent_merge for batch in batches)
    assert all(batch.authoritative_subagent_scopes == frozenset() for batch in batches)
    legacy = batches[1].snapshot
    # The parent response was read in an earlier batch and is still filtered.
    assert legacy.conversations[0]["total_tokens"] == 10
    assert legacy.subagents[0]["parent_thread_id"] == "session-1"
    assert batches[3].snapshot.conversations[0]["total_tokens"] == 10
    assert CANARY not in str([batch.snapshot.to_dict() for batch in batches])


@pytest.mark.parametrize("batch_size", [1, 2, 1_000])
def test_claude_batches_converge_to_the_single_collection_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch_size: int
) -> None:
    desktop = _claude_store(tmp_path / "desktop")
    laptop = tmp_path / "laptop"
    transcript(laptop, extra=True)
    sources = [("desktop", desktop), ("laptop", laptop)]
    monkeypatch.setattr(claude_module, "INCREMENTAL_CANDIDATES_PER_BATCH", batch_size)
    single = create_database_engine(tmp_path / "single.sqlite")
    batched = create_database_engine(tmp_path / "batched.sqlite")
    try:
        ingest_snapshot(single, ClaudeAdapter().collect(sources))
        batches = list(ClaudeAdapter().collect_incrementally(sources))
        _ingest_batches(batched, batches)
        expected = _tables(single)
        assert _tables(batched) == expected
        # Rerunning every batch is idempotent and keeps the converged graph.
        _ingest_batches(batched, batches)
        assert _tables(batched) == expected
    finally:
        single.dispose()
        batched.dispose()

    parents = {row["child_thread_id"]: row for row in expected["subagents"]}
    assert {row["parent_thread_id"] for row in parents.values()} == {
        "session-1",
        "beta",
    }
    assert len(parents) == 5
    stored = {row["external_id"]: row for row in expected["conversations"]}
    assert stored["session-1"]["source_machine"] == "laptop"
    assert stored["session-1:agent:legacy"]["total_tokens"] == 10
    assert CANARY not in json.dumps(expected, default=str)


def test_claude_batches_split_on_read_limit_but_keep_indivisible_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _claude_store(tmp_path / "claude")
    projects = home / "projects"
    session_group = (
        sum(
            path.stat().st_size
            for path in (projects / "-srv-work-acme-service").rglob("*")
            if path.is_file()
        )
        - (projects / "-srv-work-acme-service" / "agent-legacy.jsonl").stat().st_size
    )
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_READ_BYTES", session_group
    )

    with pytest.raises(ProviderDataLimitError, match="read_limit"):
        ClaudeAdapter().collect([("desktop", home)])
    batches = list(ClaudeAdapter().collect_incrementally([("desktop", home)]))
    assert len(batches) > 1
    assert sum(len(batch.snapshot.conversations) for batch in batches) == 7

    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_READ_BYTES", session_group - 1
    )
    with pytest.raises(ProviderDataLimitError, match="read_limit"):
        list(ClaudeAdapter().collect_incrementally([("desktop", home)]))


def test_claude_batches_keep_per_line_symlink_and_identity_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "p"
    _write_jsonl(project / "a.jsonl", _session_events("a", "m-a"))
    _write_jsonl(project / "agent-.jsonl", _agent_events(None, "m-x"))
    _write_jsonl(project / "agent-orphan.jsonl", [{"agentId": "orphan"}])
    with (project / "a.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")

    batches = list(ClaudeAdapter().collect_incrementally([("desktop", home)]))
    assert sum(batch.snapshot.malformed_records for batch in batches) == 4
    assert [
        sorted(_external_ids(batch)) for batch in batches if _external_ids(batch)
    ] == [["a"]]

    target = _write_jsonl(tmp_path / "outside.jsonl", _session_events("z", "m-z"))
    (project / "z.jsonl").symlink_to(target)
    with pytest.raises(ProviderDataLimitError, match="symlink"):
        list(ClaudeAdapter().collect_incrementally([("desktop", home)]))
    (project / "z.jsonl").unlink()

    original = claude_module.iter_bounded_jsonl_bytes
    monkeypatch.setattr(
        claude_module,
        "iter_bounded_jsonl_bytes",
        lambda path, budget: original(path, budget, maximum_line=16),
    )
    with pytest.raises(ProviderDataLimitError, match="line_too_large"):
        list(ClaudeAdapter().collect_incrementally([("desktop", home)]))


def test_claude_carried_parent_identifiers_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "p"
    _write_jsonl(
        project / "parent.jsonl",
        [
            {"sessionId": "parent", "type": "assistant", "message": {"id": f"m-{i}"}}
            for i in range(4)
        ],
    )
    _write_jsonl(project / "agent-a.jsonl", [{"agentId": "a", "sessionId": "parent"}])
    monkeypatch.setattr("cli_consumption.models.MAX_SNAPSHOT_RECORDS", 3)

    with pytest.raises(ProviderDataLimitError, match="provider_record_limit_exceeded"):
        list(ClaudeAdapter().collect_incrementally([("desktop", home)]))


def test_claude_incremental_handles_missing_and_empty_stores(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Missing Claude Code projects directory"):
        list(ClaudeAdapter().collect_incrementally([("desktop", tmp_path / "none")]))
    (tmp_path / "empty" / "projects").mkdir(parents=True)
    (tmp_path / "empty" / "projects" / "stray-file").write_text("x")

    batches = list(
        ClaudeAdapter().collect_incrementally([("desktop", tmp_path / "empty")])
    )

    assert len(batches) == 1
    assert batches[0].snapshot.conversations == []
    assert batches[0].subagent_merge is True


def _edge(machine: str, child: str, tokens: int = 1) -> dict[str, Any]:
    return {
        "id": f"claude:{machine}:{child}",
        "provider": "claude",
        "source_machine": machine,
        "parent_thread_id": "parent",
        "child_thread_id": child,
        "status": "completed",
        "created_at_ms": 1,
        "updated_at_ms": 2,
        "agent_role": "worker",
        "tokens_used": tokens,
    }


def _child(machine: str, child: str, event_count: int) -> Snapshot:
    snapshot = Snapshot(provider="claude")
    snapshot.conversations.append(
        {
            "id": f"claude:{child}",
            "provider": "claude",
            "external_id": child,
            "source_machine": machine,
            "project": "outside-project",
            "project_source": "none",
            "started_at": None,
            "ended_at": None,
            "duration_seconds": None,
            "source": "local-jsonl",
            "models": [],
            "iterations": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "compactions": 0,
            "event_count": event_count,
            "content_hash": f"{event_count:064x}",
            **{
                name: 0
                for name in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                    "uncached_input_tokens",
                    "visible_output_tokens",
                    "unattributed_tokens",
                )
            },
        }
    )
    snapshot.subagents.append(_edge(machine, child, event_count))
    return snapshot


def test_merged_relationships_follow_the_stored_child_copy(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "usage.sqlite")

    def merge(snapshot: Snapshot) -> None:
        ingest_snapshot(
            engine,
            snapshot,
            authoritative_subagent_scopes=frozenset(),
            subagent_merge=True,
        )

    def edges() -> list[tuple[str, int | None]]:
        return [
            (row["id"], row["tokens_used"]) for row in read_table(engine, "subagents")
        ]

    try:
        merge(_child("desktop", "child", 2))
        assert edges() == [("claude:desktop:child", 2)]
        merge(_child("desktop", "child", 2))
        assert edges() == [("claude:desktop:child", 2)]
        # A richer copy from another machine owns the relationship.
        merge(_child("laptop", "child", 3))
        assert edges() == [("claude:laptop:child", 3)]
        # A stale copy can neither add its own relationship nor regress it.
        merge(_child("desktop", "child", 2))
        assert edges() == [("claude:laptop:child", 3)]
        # A relationship whose child is absent from the batch is ignored.
        orphan = Snapshot(provider="claude")
        orphan.subagents.append(_edge("desktop", "absent"))
        merge(orphan)
        assert edges() == [("claude:laptop:child", 3)]
        # A stored child without a relationship gains it from an equal copy.
        unrelated = _child("laptop", "other", 4)
        stored_without_edge = Snapshot.from_dict(unrelated.to_dict())
        stored_without_edge.subagents.clear()
        ingest_snapshot(
            engine, stored_without_edge, authoritative_subagent_scopes=frozenset()
        )
        merge(unrelated)
        assert edges() == [("claude:laptop:child", 3), ("claude:laptop:other", 4)]
        with pytest.raises(ValueError, match="invalid_snapshot"):
            ingest_snapshot(
                engine,
                _child("laptop", "child", 5),
                authoritative_subagent_scopes=frozenset((("claude", "laptop"),)),
                subagent_merge=True,
            )
        assert {
            row["source_machine"] for row in read_table(engine, "conversations")
        } == {"laptop"}
    finally:
        engine.dispose()


JSON_ADAPTERS: list[tuple[str, type, Callable[..., Path], str, str]] = [
    ("amp", AmpAdapter, amp_thread, "T-a-copy.json", "T-z-copy.json"),
    ("continue", ContinueAdapter, continue_session, "a-copy.json", "z-copy.json"),
    (
        "gemini",
        GeminiAdapter,
        gemini_session,
        "session-a-copy.jsonl",
        "session-z-copy.jsonl",
    ),
    ("pi", PiAdapter, pi_session, "a-copy.jsonl", "z-copy.jsonl"),
    ("qwen", QwenAdapter, qwen_session, "a-copy.jsonl", "z-copy.jsonl"),
]


@pytest.mark.parametrize("richer_first", [True, False])
@pytest.mark.parametrize(
    ("name", "adapter_type", "factory", "first_name", "last_name"),
    JSON_ADAPTERS,
    ids=[case[0] for case in JSON_ADAPTERS],
)
def test_file_adapters_converge_duplicates_across_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    adapter_type: type,
    factory: Callable[..., Path],
    first_name: str,
    last_name: str,
    richer_first: bool,
) -> None:
    home = tmp_path / "home"
    base = factory(home)
    richer = factory(tmp_path / "richer", extra=True)
    shutil.copyfile(richer, base.with_name(first_name if richer_first else last_name))
    laptop = tmp_path / "laptop"
    factory(laptop, extra=True)
    sources = [("desktop", home), ("laptop", laptop)]
    module = __import__(adapter_type.__module__, fromlist=["_"])
    monkeypatch.setattr(module, "INCREMENTAL_CANDIDATES_PER_BATCH", 1)
    adapter = adapter_type()
    assert isinstance(adapter, IncrementalAdapter)

    batches = list(adapter.collect_incrementally(sources))
    single = create_database_engine(tmp_path / "single.sqlite")
    batched = create_database_engine(tmp_path / "batched.sqlite")
    try:
        ingest_snapshot(single, adapter.collect(sources))
        _ingest_batches(batched, batches)
        assert _tables(batched) == _tables(single)
    finally:
        single.dispose()
        batched.dispose()

    assert len(batches) == 3
    assert all(len(batch.snapshot.conversations) == 1 for batch in batches)
    assert {batch.snapshot.provider for batch in batches} == {name}
    assert "canary" not in str([batch.snapshot.to_dict() for batch in batches])
    with pytest.raises(ValueError, match="Missing"):
        list(adapter.collect_incrementally([("desktop", tmp_path / "missing")]))


def _claude_sessions(home: Path, count: int) -> Path:
    project = home / "projects" / "p"
    for index in range(count):
        _write_jsonl(
            project / f"s{index}.jsonl",
            _session_events(f"s{index}", f"m-{index}"),
        )
    return home


def _collect(*arguments: str):
    return runner.invoke(app, ["collect", "--provider", "claude", *arguments])


def test_collect_switches_to_batches_automatically_with_deterministic_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _claude_sessions(tmp_path / "PRIVATE_PATH_CANARY", 4)
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    common = ["--source", f"desktop={home}", "--json"]

    first = _collect(*common, "--database", str(tmp_path / "first.sqlite"))
    second = _collect(*common, "--database", str(tmp_path / "second.sqlite"))
    refused = _collect(
        *common, "--no-incremental", "--database", str(tmp_path / "refused.sqlite")
    )

    assert first.exit_code == second.exit_code == 0, first.output
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == {
        "incremental": True,
        "incremental_trigger": "automatic",
        "ingestions": [
            {
                "provider": "claude",
                "batched": True,
                "batches": 2,
                "received": 4,
                "written": 4,
                "skipped": 0,
                "malformed": 0,
                "batch_duplicates": 0,
            }
        ],
    }
    assert refused.exit_code == 2
    assert json.loads(refused.stdout) == {
        "error": {"code": "provider_limit_exceeded", "provider": "claude"},
        "ingestions": [],
    }
    assert not (tmp_path / "refused.sqlite").exists()
    for result in (first, refused):
        assert "PRIVATE_PATH_CANARY" not in result.output
        assert str(tmp_path) not in result.output
        assert CANARY not in result.output

    human = _collect(
        "--source", f"desktop={home}", "--database", str(tmp_path / "human.sqlite")
    )
    assert human.exit_code == 0, human.output
    assert "collected in bounded batches: claude." in human.stdout
    assert "Incremental ingestion claude: 2 batches, 4 written" in human.stdout


def test_automatic_batches_resume_after_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _claude_sessions(tmp_path / "claude", 2)
    project = home / "projects" / "p"
    target = _write_jsonl(tmp_path / "outside.jsonl", _session_events("z", "m-z"))
    (project / "z.jsonl").symlink_to(target)
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    monkeypatch.setattr(claude_module, "INCREMENTAL_CANDIDATES_PER_BATCH", 1)
    database = tmp_path / "resume.sqlite"
    arguments = ["--source", f"desktop={home}", "--database", str(database)]

    failed = _collect(*arguments, "--json")
    human_failure = _collect(*arguments)

    assert failed.exit_code == human_failure.exit_code == 2
    payload = json.loads(failed.stdout)
    assert payload["error"] == {"code": "provider_limit_exceeded", "provider": "claude"}
    assert payload["incremental"] is True
    assert payload["incremental_trigger"] == "automatic"
    assert payload["ingestions"][0]["batches"] == 2
    assert payload["ingestions"][0]["written"] == 2
    assert "2 earlier incremental batch(es) were committed" in human_failure.stderr
    assert str(tmp_path) not in failed.output + human_failure.output

    (project / "z.jsonl").unlink()
    shutil.copyfile(target, project / "z.jsonl")
    resumed = _collect(*arguments, "--json")
    fresh_database = tmp_path / "fresh.sqlite"
    fresh = _collect(
        "--source", f"desktop={home}", "--database", str(fresh_database), "--json"
    )

    assert resumed.exit_code == fresh.exit_code == 0, resumed.output
    assert (
        json.loads(resumed.stdout)["ingestions"][0] | {"written": 3, "skipped": 0}
        == (json.loads(fresh.stdout)["ingestions"][0])
    )
    assert json.loads(resumed.stdout)["ingestions"][0]["skipped"] == 2
    resumed_engine = create_database_engine(database)
    fresh_engine = create_database_engine(fresh_database)
    try:
        assert _tables(resumed_engine) == _tables(fresh_engine)
        assert len(read_table(resumed_engine, "conversations")) == 3
    finally:
        resumed_engine.dispose()
        fresh_engine.dispose()


def test_automatic_strict_batches_validate_before_creating_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _claude_sessions(tmp_path / "claude", 3)
    with (home / "projects" / "p" / "s2.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("PROMPT_SECRET_CANARY\n")
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    database = tmp_path / "strict.sqlite"

    result = _collect(
        "--source", f"desktop={home}", "--database", str(database), "--strict", "--json"
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": {"code": "malformed_records", "provider": "claude"},
        "incremental": True,
        "incremental_trigger": "automatic",
        "ingestions": [],
    }
    assert "PROMPT_SECRET_CANARY" not in result.output
    assert not database.exists()


def test_automatic_switch_batches_only_the_overflowing_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollout_factory
) -> None:
    claude_home = _claude_sessions(tmp_path / "claude", 3)
    codex_home = tmp_path / "codex"
    rollout_factory(codex_home)
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    common = [
        "collect",
        "--provider",
        "all",
        "--source",
        f"desktop={claude_home}",
        "--source",
        f"laptop={codex_home}",
    ]

    result = runner.invoke(
        app, [*common, "--database", str(tmp_path / "all.sqlite"), "--json"]
    )
    human = runner.invoke(app, [*common, "--database", str(tmp_path / "human.sqlite")])

    assert result.exit_code == human.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["incremental_trigger"] == "automatic"
    outcomes = {
        row["provider"]: (row["batched"], row["batches"], row["written"])
        for row in payload["ingestions"]
    }
    # The registry order is preserved; Codex homes also match generic markers.
    assert [row["provider"] for row in payload["ingestions"]] == [
        "codex",
        "continue",
        "claude",
        "pi",
    ]
    assert outcomes["claude"] == (True, 2, 3)
    assert outcomes["codex"] == (False, 1, 1)
    assert not outcomes["continue"][0] and not outcomes["pi"][0]
    assert "Ingestion codex: 1 written, 0 unchanged" in human.stdout
    assert "Incremental ingestion claude: 2 batches" in human.stdout


def test_non_incremental_provider_keeps_all_or_nothing_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "kimi"
    for session in ("a", "b"):
        path = home / "sessions" / "hash" / session / "wire.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"type":"metadata","protocol_version":"1"}\n')
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 1)

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "kimi",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "kimi.sqlite"),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": {"code": "provider_limit_exceeded", "provider": "kimi"},
        "ingestions": [],
    }


def test_carried_parent_copy_wins_over_a_poorer_copy_in_a_later_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "p"
    richer = _session_events("s", "m-shared") + _session_events("s", "m-only-a")[1:]
    _write_jsonl(project / "a.jsonl", richer)
    _write_jsonl(
        project / "a" / "subagents" / "agent-n.jsonl", _agent_events("n", "m-n")
    )
    _write_jsonl(project / "b.jsonl", _session_events("s", "m-only-b"))
    _write_jsonl(
        project / "agent-legacy.jsonl",
        _agent_events("legacy", "m-only-a", session_id="s", tokens=100_000)
        + _agent_events("legacy", "m-own", session_id="s")[1:],
    )
    monkeypatch.setattr(claude_module, "INCREMENTAL_CANDIDATES_PER_BATCH", 2)

    batches = list(ClaudeAdapter().collect_incrementally([("desktop", home)]))
    single = create_database_engine(tmp_path / "single.sqlite")
    batched = create_database_engine(tmp_path / "batched.sqlite")
    try:
        ingest_snapshot(single, ClaudeAdapter().collect([("desktop", home)]))
        _ingest_batches(batched, batches)
        assert _tables(batched) == _tables(single)
    finally:
        single.dispose()
        batched.dispose()

    assert [sorted(_external_ids(batch)) for batch in batches] == [
        ["a:agent:n", "s"],
        ["s", "s:agent:legacy"],
    ]
    legacy = next(
        row
        for row in batches[1].snapshot.conversations
        if row["external_id"] == "s:agent:legacy"
    )
    assert legacy["total_tokens"] == 10


def test_strict_forced_batches_stage_merged_relationships(tmp_path: Path) -> None:
    home = _claude_store(tmp_path / "claude")
    database = tmp_path / "strict.sqlite"

    result = _collect(
        "--incremental",
        "--strict",
        "--source",
        f"desktop={home}",
        "--database",
        str(database),
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["incremental_trigger"] == "requested"
    assert payload["ingestions"][0]["batched"] is True
    engine = create_database_engine(database)
    try:
        assert len(read_table(engine, "subagents")) == 5
        assert CANARY not in json.dumps(_tables(engine), default=str)
    finally:
        engine.dispose()
