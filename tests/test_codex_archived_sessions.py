"""Archived Codex rollouts under ``$CODEX_HOME/archived_sessions/``.

Codex archiving moves the rollout files of a thread, and those of its archived
descendant threads, from the dated ``sessions/YYYY/MM/DD/`` tree to a flat
``archived_sessions/`` directory; unarchiving moves them back into the dated tree.
The subagent graph in ``state_5.sqlite`` keeps its edges across both moves. These
synthetic fixtures reproduce those moves with upstream-shaped file names.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table
from typer.testing import CliRunner

import cli_consumption.adapters.codex as codex_module
from cli_consumption.adapters._shared import ProviderDataLimitError
from cli_consumption.adapters.base import CollectionBatch
from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.cli import app
from cli_consumption.models import Snapshot
from cli_consumption.storage import TABLES, create_database_engine, ingest_snapshot

runner = CliRunner()
CANARY = "secret value"
PATH_CANARY = "PRIVATE_PATH_CANARY"
GRAPH_TABLES = [name for name in TABLES if name != "ingestion_runs"]
RolloutFactory = Callable[..., Path]


def _file_name(conversation_id: str) -> str:
    return f"rollout-2026-08-25T10-00-00-{conversation_id}.jsonl"


def _active(home: Path, conversation_id: str) -> Path:
    return home / "sessions" / "2026" / "08" / "25" / _file_name(conversation_id)


def _archived(home: Path, conversation_id: str) -> Path:
    return home / "archived_sessions" / _file_name(conversation_id)


def _content(
    tmp_path: Path,
    rollout_factory: RolloutFactory,
    conversation_id: str,
    *,
    extra_event: bool = False,
) -> bytes:
    staging = tmp_path / "staging" / f"{conversation_id}-{extra_event}"
    return rollout_factory(
        staging, conversation_id, extra_event=extra_event
    ).read_bytes()


def _place(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _archive(home: Path, conversation_id: str) -> None:
    destination = _archived(home, conversation_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(_active(home, conversation_id), destination)


def _unarchive(home: Path, conversation_id: str) -> None:
    destination = _active(home, conversation_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(_archived(home, conversation_id), destination)


def _home(
    tmp_path: Path,
    rollout_factory: RolloutFactory,
    conversation_ids: Iterable[str],
    *,
    name: str = "codex",
) -> Path:
    home = tmp_path / name
    for conversation_id in conversation_ids:
        _place(
            _active(home, conversation_id),
            _content(tmp_path, rollout_factory, conversation_id),
        )
    return home


def _thread_graph(home: Path) -> None:
    """A parent thread with a child that spawned its own grandchild."""
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
        connection.executescript(
            """
            CREATE TABLE thread_spawn_edges (
                parent_thread_id, child_thread_id, status
            );
            CREATE TABLE threads (
                id, created_at_ms, updated_at_ms, agent_role, tokens_used,
                archived_at
            );
            INSERT INTO thread_spawn_edges VALUES ('parent', 'child', 'done');
            INSERT INTO thread_spawn_edges VALUES ('child', 'grandchild', 'done');
            INSERT INTO threads VALUES ('child', 1, 2, 'worker', 3, NULL);
            INSERT INTO threads VALUES ('grandchild', 4, 5, 'explorer', 6, NULL);
            """
        )
        connection.commit()


def _mark_archived(home: Path, thread_ids: Iterable[str]) -> None:
    # Codex records archive state on the thread row and keeps every spawn edge.
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
        connection.executemany(
            "UPDATE threads SET archived_at = 99 WHERE id = ?",
            [(thread_id,) for thread_id in thread_ids],
        )
        connection.commit()


def _collect(home: Path) -> Snapshot:
    return CodexAdapter().collect([("desktop", home)])


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


def _records(snapshot: Snapshot) -> dict[str, Any]:
    """Snapshot records independent of the order in which rollouts were listed."""
    return {
        name: sorted(value, key=lambda record: str(record["id"]))
        if isinstance(value, list)
        else value
        for name, value in snapshot.to_dict().items()
    }


def _ids(snapshot: Snapshot) -> list[str]:
    return sorted(str(row["id"]) for row in snapshot.conversations)


def test_flat_and_nested_archives_are_collected_with_active_sessions(
    tmp_path: Path, rollout_factory: RolloutFactory
) -> None:
    home = _home(tmp_path, rollout_factory, ["active"])
    _place(
        _archived(home, "flat"),
        _content(tmp_path, rollout_factory, "flat"),
    )
    # Codex archives flat, but its own lookups also walk nested archive trees.
    nested = home / "archived_sessions" / "2026" / "08" / "25" / _file_name("nested")
    _place(nested, _content(tmp_path, rollout_factory, "nested"))
    (home / "archived_sessions" / "index.json").write_text("{}", encoding="utf-8")
    # Compressed rollouts are outside the supported plain-JSONL format.
    (home / "archived_sessions" / f"{_file_name('zst')}.zst").write_bytes(b"\x28")

    snapshot = _collect(home)

    assert _ids(snapshot) == [
        "codex:active",
        "codex:flat",
        "codex:nested",
    ]
    assert snapshot.duplicate_conversations == 0
    assert snapshot.malformed_records == 0
    assert {row["source_machine"] for row in snapshot.conversations} == {"desktop"}
    assert CANARY not in str(snapshot.to_dict())


def test_a_missing_or_non_directory_archive_keeps_the_existing_semantics(
    tmp_path: Path, rollout_factory: RolloutFactory
) -> None:
    home = _home(tmp_path, rollout_factory, ["active"])
    assert _ids(_collect(home)) == ["codex:active"]

    # A regular file where the archive directory should be is not an archive.
    (home / "archived_sessions").write_text("not a directory", encoding="utf-8")
    assert _ids(_collect(home)) == ["codex:active"]

    # The active sessions directory stays the required Codex home marker, even when
    # an archive exists, for both single and batched collection.
    archive_only = tmp_path / "archive-only"
    _place(
        _archived(archive_only, "flat"),
        _content(tmp_path, rollout_factory, "flat"),
    )
    with pytest.raises(ValueError, match="Missing Codex sessions directory"):
        _collect(archive_only)
    with pytest.raises(ValueError, match="Missing Codex sessions directory"):
        list(CodexAdapter().collect_incrementally([("desktop", archive_only)]))


def test_a_session_archived_after_collection_keeps_its_identity_and_data(
    tmp_path: Path, rollout_factory: RolloutFactory
) -> None:
    home = _home(tmp_path, rollout_factory, ["conversation-1", "other"])
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        first = _collect(home)
        ingest_snapshot(engine, first)
        before = _tables(engine)

        _archive(home, "conversation-1")
        archived = _collect(home)
        result = ingest_snapshot(engine, archived)

        assert _records(archived) == _records(first)
        assert (result.written, result.skipped) == (0, 2)
        assert _tables(engine) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("richer_location", ["sessions", "archived_sessions"])
def test_a_copy_in_both_locations_converges_to_the_most_complete(
    tmp_path: Path, rollout_factory: RolloutFactory, richer_location: str
) -> None:
    base = _content(tmp_path, rollout_factory, "conversation-1")
    richer = _content(tmp_path, rollout_factory, "conversation-1", extra_event=True)
    home = tmp_path / "codex"
    active, archived = (
        _active(home, "conversation-1"),
        _archived(home, "conversation-1"),
    )
    _place(active, richer if richer_location == "sessions" else base)
    _place(archived, base if richer_location == "sessions" else richer)

    snapshot = _collect(home)

    assert snapshot.duplicate_conversations == 1
    assert _ids(snapshot) == ["codex:conversation-1"]
    (conversation,) = snapshot.conversations
    assert conversation["event_count"] == len(richer.splitlines())
    assert conversation["compactions"] == 1

    # Either ingestion order converges on the same stored copy and identifier.
    poorer_home = tmp_path / "poorer"
    _place(_active(poorer_home, "conversation-1"), base)
    results = []
    for name, homes in (
        ("forward", (poorer_home, home)),
        ("reverse", (home, poorer_home)),
    ):
        engine = create_database_engine(tmp_path / f"{name}.sqlite")
        try:
            for source in homes:
                ingest_snapshot(engine, _collect(source))
            results.append(_tables(engine))
        finally:
            engine.dispose()
    assert results[0] == results[1]
    assert [row["id"] for row in results[0]["conversations"]] == [
        "codex:conversation-1"
    ]
    assert results[0]["conversations"][0]["compactions"] == 1


def test_identical_copies_in_both_locations_count_as_one_conversation(
    tmp_path: Path, rollout_factory: RolloutFactory
) -> None:
    home = _home(tmp_path, rollout_factory, ["conversation-1"])
    _place(
        _archived(home, "conversation-1"),
        _active(home, "conversation-1").read_bytes(),
    )

    snapshot = _collect(home)

    assert snapshot.duplicate_conversations == 1
    assert _ids(snapshot) == ["codex:conversation-1"]


@pytest.mark.parametrize(
    "archived_threads",
    [
        ("parent", "child", "grandchild"),
        ("parent",),
        ("child",),
        ("grandchild",),
    ],
    ids=["descendants", "parent-only", "child-only", "grandchild-only"],
)
def test_archiving_threads_keeps_subagent_relationships(
    tmp_path: Path,
    rollout_factory: RolloutFactory,
    archived_threads: tuple[str, ...],
) -> None:
    home = _home(tmp_path, rollout_factory, ["parent", "child", "grandchild"])
    _thread_graph(home)
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        ingest_snapshot(engine, _collect(home))
        before = _tables(engine)
        assert {
            (row["parent_thread_id"], row["child_thread_id"])
            for row in before["subagents"]
        } == {("parent", "child"), ("child", "grandchild")}

        for thread_id in archived_threads:
            _archive(home, thread_id)
        _mark_archived(home, archived_threads)
        snapshot = _collect(home)
        ingest_snapshot(engine, snapshot)

        assert _ids(snapshot) == [
            "codex:child",
            "codex:grandchild",
            "codex:parent",
        ]
        assert _tables(engine) == before
    finally:
        engine.dispose()

    # A fresh database built only from the archived layout stores the same graph.
    fresh = create_database_engine(tmp_path / "fresh.sqlite")
    try:
        ingest_snapshot(fresh, _collect(home))
        assert _tables(fresh) == before
    finally:
        fresh.dispose()


def test_unarchiving_back_to_dated_sessions_keeps_identity_and_graph(
    tmp_path: Path, rollout_factory: RolloutFactory
) -> None:
    home = _home(tmp_path, rollout_factory, ["parent", "child", "grandchild"])
    _thread_graph(home)
    original = _collect(home)
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        ingest_snapshot(engine, original)
        before = _tables(engine)
        for thread_id in ("parent", "child", "grandchild"):
            _archive(home, thread_id)
        ingest_snapshot(engine, _collect(home))
        for thread_id in ("parent", "child", "grandchild"):
            _unarchive(home, thread_id)

        restored = _collect(home)
        result = ingest_snapshot(engine, restored)

        assert not any((home / "archived_sessions").iterdir())
        assert _records(restored) == _records(original)
        assert result.written == 0
        assert _tables(engine) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("richer_location", ["sessions", "archived_sessions"])
def test_batched_collection_matches_one_collection_across_locations(
    tmp_path: Path,
    rollout_factory: RolloutFactory,
    monkeypatch: pytest.MonkeyPatch,
    richer_location: str,
) -> None:
    home = _home(tmp_path, rollout_factory, ["active", "shared"])
    _place(_archived(home, "flat"), _content(tmp_path, rollout_factory, "flat"))
    _place(
        home / "archived_sessions" / "2026" / "08" / "25" / _file_name("nested"),
        _content(tmp_path, rollout_factory, "nested"),
    )
    richer = _content(tmp_path, rollout_factory, "shared", extra_event=True)
    if richer_location == "sessions":
        _place(_archived(home, "shared"), _active(home, "shared").read_bytes())
        _place(_active(home, "shared"), richer)
    else:
        _place(_archived(home, "shared"), richer)
    monkeypatch.setattr(codex_module, "INCREMENTAL_CANDIDATES_PER_BATCH", 1)

    batches = list(CodexAdapter().collect_incrementally([("desktop", home)]))
    single = create_database_engine(tmp_path / "single.sqlite")
    batched = create_database_engine(tmp_path / "batched.sqlite")
    rerun = create_database_engine(tmp_path / "rerun.sqlite")
    try:
        ingest_snapshot(single, _collect(home))
        _ingest_batches(batched, batches)
        _ingest_batches(rerun, batches)
        _ingest_batches(rerun, list(reversed(batches)))
        assert _tables(batched) == _tables(single) == _tables(rerun)
        stored = {row["id"]: row for row in _tables(single)["conversations"]}
    finally:
        single.dispose()
        batched.dispose()
        rerun.dispose()

    # Active files are batched before archived ones; each tree is walked with a
    # directory's own files before its subdirectories.
    assert [_ids(batch.snapshot) for batch in batches] == [
        ["codex:active"],
        ["codex:shared"],
        ["codex:flat"],
        ["codex:shared"],
        ["codex:nested"],
    ]
    assert sorted(stored) == [
        "codex:active",
        "codex:flat",
        "codex:nested",
        "codex:shared",
    ]
    assert stored["codex:shared"]["compactions"] == 1
    assert all(batch.authoritative_subagent_scopes == frozenset() for batch in batches)
    assert CANARY not in str([batch.snapshot.to_dict() for batch in batches])


def test_collect_command_reads_archives_with_automatic_and_forced_batches(
    tmp_path: Path, rollout_factory: RolloutFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, rollout_factory, ["active"], name=PATH_CANARY)
    _place(_archived(home, "flat"), _content(tmp_path, rollout_factory, "flat"))
    _place(
        home / "archived_sessions" / PATH_CANARY / _file_name("nested"),
        _content(tmp_path, rollout_factory, "nested"),
    )
    source = ["--provider", "codex", "--source", f"desktop={home}", "--json"]

    def run(name: str, *arguments: str):
        database = tmp_path / f"{name}.sqlite"
        result = runner.invoke(
            app, ["collect", *source, *arguments, "--database", str(database)]
        )
        assert result.exit_code == 0, result.output
        assert PATH_CANARY not in result.output
        assert str(tmp_path) not in result.output
        assert CANARY not in result.output
        engine = create_database_engine(database)
        try:
            return json.loads(result.stdout), _tables(engine)
        finally:
            engine.dispose()

    single_output, single = run("single")
    forced_output, forced = run("forced", "--incremental")
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    refused = runner.invoke(
        app,
        [
            "collect",
            *source,
            "--no-incremental",
            "--database",
            str(tmp_path / "refused.sqlite"),
        ],
    )
    automatic_output, automatic = run("automatic")

    assert [row["id"] for row in single["conversations"]] == [
        "codex:active",
        "codex:flat",
        "codex:nested",
    ]
    assert single == forced == automatic
    assert single_output["ingestions"][0]["written"] == 3
    assert forced_output["incremental_trigger"] == "requested"
    assert automatic_output["incremental_trigger"] == "automatic"
    assert automatic_output["ingestions"][0]["batched"] is True
    assert json.loads(refused.stdout)["error"] == {
        "code": "provider_limit_exceeded",
        "provider": "codex",
    }


def test_archive_entries_share_the_single_pass_candidate_budget(
    tmp_path: Path, rollout_factory: RolloutFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, rollout_factory, ["active"])
    for conversation_id in ("flat-a", "flat-b"):
        _place(
            _archived(home, conversation_id),
            _content(tmp_path, rollout_factory, conversation_id),
        )
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)

    with pytest.raises(ProviderDataLimitError, match="candidate_limit_exceeded"):
        _collect(home)
    batches = list(CodexAdapter().collect_incrementally([("desktop", home)]))
    assert sorted(id_ for batch in batches for id_ in _ids(batch.snapshot)) == [
        "codex:active",
        "codex:flat-a",
        "codex:flat-b",
    ]


def test_adversarial_archive_layouts_stay_within_existing_limits(
    tmp_path: Path, rollout_factory: RolloutFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, rollout_factory, ["active"])
    archive = home / "archived_sessions"
    # Unexpected nesting and a directory named like a rollout are walked alike by
    # the single pass and by batches.
    _place(
        archive / "a" / "b" / "c" / "d" / _file_name("deep"),
        _content(tmp_path, rollout_factory, "deep"),
    )
    _place(
        archive / "trap.jsonl" / _file_name("inside"),
        _content(tmp_path, rollout_factory, "inside"),
    )
    # Directory symlinks are never followed, even when they point at real rollouts.
    outside = tmp_path / "outside"
    _place(
        outside / _file_name("outside"),
        _content(tmp_path, rollout_factory, "outside"),
    )
    (archive / "linked-directory").symlink_to(outside, target_is_directory=True)
    (archive / "loop").symlink_to(archive, target_is_directory=True)
    malformed = _place(
        _archived(home, "malformed"),
        _content(tmp_path, rollout_factory, "malformed") + b"not-json\n[]\n",
    )

    expected = [
        "codex:active",
        "codex:deep",
        "codex:inside",
        "codex:malformed",
    ]
    snapshot = _collect(home)
    batches = list(CodexAdapter().collect_incrementally([("desktop", home)]))
    assert _ids(snapshot) == expected
    assert snapshot.malformed_records == 2
    assert sorted(id_ for batch in batches for id_ in _ids(batch.snapshot)) == expected
    assert sum(batch.snapshot.malformed_records for batch in batches) == 2

    # A symlinked rollout file aborts collection, as it does under sessions/.
    linked = archive / _file_name("linked-file")
    linked.symlink_to(outside / _file_name("outside"))
    with pytest.raises(ProviderDataLimitError, match="symlink"):
        _collect(home)
    with pytest.raises(ProviderDataLimitError, match="symlink"):
        list(CodexAdapter().collect_incrementally([("desktop", home)]))
    linked.unlink()

    # Oversized archived files and lines keep the per-file and per-line limits in
    # both modes; batching never relaxes them.
    original = codex_module.iter_bounded_jsonl_bytes
    size = malformed.stat().st_size
    for limits, code in (
        ({"maximum_file": size - 1}, "provider_file_too_large"),
        ({"maximum_line": 16}, "provider_line_too_large"),
    ):
        monkeypatch.setattr(
            codex_module,
            "iter_bounded_jsonl_bytes",
            lambda path, budget, limits=limits: original(path, budget, **limits),
        )
        with pytest.raises(ProviderDataLimitError, match=code):
            _collect(home)
        with pytest.raises(ProviderDataLimitError, match=code):
            list(CodexAdapter().collect_incrementally([("desktop", home)]))
    assert "outside" not in str(snapshot.to_dict())


def test_a_symlinked_archive_root_is_never_followed(
    tmp_path: Path, rollout_factory: RolloutFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, rollout_factory, ["active"])
    outside = tmp_path / "unrelated"
    _place(
        outside / "private.jsonl",
        _content(tmp_path, rollout_factory, "outside"),
    )
    (home / "archived_sessions").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(codex_module, "INCREMENTAL_CANDIDATES_PER_BATCH", 1)

    snapshot = _collect(home)
    batches = list(CodexAdapter().collect_incrementally([("desktop", home)]))
    single = create_database_engine(tmp_path / "single.sqlite")
    batched = create_database_engine(tmp_path / "batched.sqlite")
    try:
        ingest_snapshot(single, snapshot)
        _ingest_batches(batched, batches)
        stored = [_tables(single), _tables(batched)]
    finally:
        single.dispose()
        batched.dispose()

    assert _ids(snapshot) == ["codex:active"]
    assert [_ids(batch.snapshot) for batch in batches] == [["codex:active"]]
    for tables in stored:
        assert [row["id"] for row in tables["conversations"]] == ["codex:active"]
        assert "outside" not in str(tables)


def test_a_symlinked_sessions_root_keeps_its_existing_semantics(
    tmp_path: Path, rollout_factory: RolloutFactory
) -> None:
    # Unlike the optional archive, the required sessions root has always been
    # followed when it is a symlink; this change deliberately leaves it alone.
    real = _home(tmp_path, rollout_factory, ["active"], name="real")
    home = tmp_path / "codex"
    home.mkdir()
    (home / "sessions").symlink_to(real / "sessions", target_is_directory=True)

    assert _ids(_collect(home)) == ["codex:active"]
    batches = list(CodexAdapter().collect_incrementally([("desktop", home)]))
    assert [_ids(batch.snapshot) for batch in batches] == [["codex:active"]]
