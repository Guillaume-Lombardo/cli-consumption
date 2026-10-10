"""Batched Claude Code collection must store exactly what one collection stores."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from storage_helpers import read_table

import cli_consumption.adapters.claude as claude_module
from cli_consumption.adapters.claude import ClaudeAdapter
from cli_consumption.storage import TABLES, create_database_engine, ingest_snapshot

GRAPH_TABLES = [name for name in TABLES if name != "ingestion_runs"]
BATCH_SIZES = (1, 2, 1_000)


def _write(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )


def _assistant(
    identity: dict[str, Any], message_id: str, tokens: int, second: int = 1
) -> dict:
    return {
        **identity,
        "type": "assistant",
        "requestId": f"request-{message_id}",
        "timestamp": f"2026-08-26T09:00:{second:02d}Z",
        "message": {
            "id": message_id,
            "model": "claude-sonnet-4-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": tokens, "output_tokens": 2},
            "content": [{"type": "text", "text": "privacy canary"}],
        },
    }


def _parent(session_id: str | None, responses: list[str]) -> list[dict[str, Any]]:
    identity = {"sessionId": session_id} if session_id is not None else {}
    return [
        {
            **identity,
            "type": "user",
            "uuid": "prompt",
            "timestamp": "2026-08-26T09:00:00Z",
            "message": {"role": "user", "content": "privacy canary"},
        },
        *(_assistant(identity, response, 5) for response in responses),
    ]


def _child(
    session_id: str | None, agent_id: str | None, replayed: list[str], own: str
) -> list[dict[str, Any]]:
    identity: dict[str, Any] = {"isSidechain": True}
    if session_id is not None:
        identity["sessionId"] = session_id
    if agent_id is not None:
        identity["agentId"] = agent_id
    return [
        {
            **identity,
            "type": "user",
            "uuid": f"{own}-prompt",
            "timestamp": "2026-08-26T09:00:05Z",
            "message": {"role": "user", "content": "privacy canary"},
        },
        *(_assistant(identity, response, 100_000, 6) for response in replayed),
        _assistant(identity, own, 7, 6),
    ]


def _tables(database: Path) -> dict[str, list[dict[str, Any]]]:
    engine = create_database_engine(database)
    try:
        return {name: read_table(engine, name) for name in GRAPH_TABLES}
    finally:
        engine.dispose()


def _ingest(database: Path, batches: list[Any]) -> None:
    engine = create_database_engine(database)
    try:
        for batch in batches:
            ingest_snapshot(
                engine,
                batch.snapshot,
                authoritative_subagent_scopes=batch.authoritative_subagent_scopes,
                subagent_merge=batch.subagent_merge,
            )
    finally:
        engine.dispose()


def assert_batches_equal_one_collection(
    sources: list[tuple[str, Path]], workspace: Path
) -> dict[str, list[dict[str, Any]]]:
    single = workspace / "single.sqlite"
    engine = create_database_engine(single)
    try:
        ingest_snapshot(engine, ClaudeAdapter().collect(sources))
    finally:
        engine.dispose()
    expected = _tables(single)
    for size in BATCH_SIZES:
        database = workspace / f"batched-{size}.sqlite"
        with mock.patch.object(claude_module, "INCREMENTAL_CANDIDATES_PER_BATCH", size):
            batches = list(ClaudeAdapter().collect_incrementally(sources))
        _ingest(database, batches)
        assert _tables(database) == expected, size
        _ingest(database, batches)
        assert _tables(database) == expected, size
    return expected


def _tokens(tables: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {row["external_id"]: row["total_tokens"] for row in tables["conversations"]}


@pytest.mark.parametrize("richer_first", [True, False])
def test_legacy_child_uses_richer_parent_from_another_project_directory(
    tmp_path: Path, richer_first: bool
) -> None:
    poorer = tmp_path / "poorer"
    _write(poorer / "projects" / "-home-a" / "parent.jsonl", _parent("parent", ["m1"]))
    _write(
        poorer / "projects" / "-home-a" / "agent-a.jsonl",
        _child("parent", "a", ["m2"], "own"),
    )
    richer = tmp_path / "richer"
    _write(
        richer / "projects" / "-Users-a" / "parent.jsonl",
        _parent("parent", ["m1", "m2"]),
    )
    sources = [("poorer", poorer), ("richer", richer)]
    if richer_first:
        sources.reverse()

    expected = assert_batches_equal_one_collection(sources, tmp_path)

    assert _tokens(expected)["parent:agent:a"] == 9


def test_nested_child_uses_richer_parent_copy_with_another_file_name(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "p"
    _write(project / "parent.jsonl", _parent("parent", ["m1"]))
    _write(project / "z-copy.jsonl", _parent("parent", ["m1", "m2"]))
    _write(
        project / "parent" / "subagents" / "agent-a.jsonl",
        _child("parent", "a", ["m2"], "own"),
    )

    expected = assert_batches_equal_one_collection([("desktop", home)], tmp_path)

    assert _tokens(expected)["parent:agent:a"] == 9
    assert [row["parent_thread_id"] for row in expected["subagents"]] == ["parent"]


NAMES = ("s1", "s2", "x")
RESPONSES = ("r1", "r2", "r3")
PARENTS = st.tuples(
    st.integers(0, 2),
    st.sampled_from(("p", "q")),
    st.sampled_from(NAMES),
    st.one_of(st.none(), st.sampled_from(NAMES[:2])),
    st.lists(st.sampled_from(RESPONSES), unique=True, max_size=3),
)
NESTED = st.tuples(
    st.integers(0, 2),
    st.sampled_from(("p", "q")),
    st.sampled_from(NAMES),
    st.one_of(st.none(), st.sampled_from(("a", "b"))),
    st.lists(st.sampled_from(RESPONSES), unique=True, max_size=3),
)
LEGACY = st.tuples(
    st.integers(0, 2),
    st.sampled_from(("p", "q")),
    st.one_of(st.none(), st.sampled_from(NAMES[:2])),
    st.sampled_from(("a", "b")),
    st.lists(st.sampled_from(RESPONSES), unique=True, max_size=3),
)


@settings(
    max_examples=40,
    derandomize=True,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    source_count=st.integers(1, 3),
    parents=st.lists(PARENTS, max_size=5),
    nested=st.lists(NESTED, max_size=3),
    legacy=st.lists(LEGACY, max_size=3),
)
def test_random_layouts_store_identical_results_in_batches(
    source_count: int,
    parents: list[tuple[int, str, str, str | None, list[str]]],
    nested: list[tuple[int, str, str, str | None, list[str]]],
    legacy: list[tuple[int, str, str | None, str, list[str]]],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        homes = [workspace / f"m{index}" for index in range(source_count)]
        for home in homes:
            (home / "projects").mkdir(parents=True)
        for source, project, name, session_id, responses in parents:
            _write(
                homes[source % source_count] / "projects" / project / f"{name}.jsonl",
                _parent(session_id, responses),
            )
        for source, project, directory_name, agent_id, replayed in nested:
            _write(
                homes[source % source_count]
                / "projects"
                / project
                / directory_name
                / "subagents"
                / f"agent-{agent_id or 'n'}.jsonl",
                _child(directory_name, agent_id, replayed, f"own-{agent_id}"),
            )
        for source, project, session_id, agent_id, replayed in legacy:
            _write(
                homes[source % source_count]
                / "projects"
                / project
                / f"agent-{agent_id}.jsonl",
                _child(session_id, agent_id, replayed, f"own-{agent_id}"),
            )
        sources = [(f"m{index}", home) for index, home in enumerate(homes)]

        expected = assert_batches_equal_one_collection(sources, workspace)

        assert "privacy canary" not in json.dumps(expected, default=str)


def test_identity_pass_skips_malformed_lines_and_uses_the_same_fallbacks(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "p"
    prefix = 'not-json\n[]\n{"type":"summary","summary":"privacy canary"}\n'
    for name, events in (
        ("late-id.jsonl", _parent("parent", ["m1", "m2"])),
        (" .jsonl", _parent(None, ["m3"])),
        ("stem-id.jsonl", _parent(None, ["m4"])),
    ):
        _write(project / name, events)
        path = project / name
        path.write_text(prefix + path.read_text(encoding="utf-8"), encoding="utf-8")
    _write(project / "parent.jsonl", _parent("parent", ["m1"]))
    legacy = project / "agent-late.jsonl"
    _write(legacy, _child("parent", "late", ["m2"], "own"))
    legacy.write_text(prefix + legacy.read_text(encoding="utf-8"), encoding="utf-8")

    expected = assert_batches_equal_one_collection([("desktop", home)], tmp_path)

    tokens = _tokens(expected)
    assert tokens["parent:agent:late"] == 9
    assert "stem-id" in tokens
    assert any(key.startswith("session-") for key in tokens)


def _advised(identity: dict[str, Any], message_id: str, second: int) -> dict:
    event = _assistant(identity, message_id, 5, second)
    event["message"]["usage"].update(
        {
            "cache_creation_input_tokens": 8,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 2,
                "ephemeral_1h_input_tokens": 6,
            },
            "output_tokens_details": {"thinking_tokens": 1},
            "iterations": [
                {"type": "message", "input_tokens": 5, "output_tokens": 2},
                {
                    "type": "advisor_message",
                    "model": "claude-opus-5",
                    "input_tokens": 40,
                    "cache_creation_input_tokens": 4,
                    "cache_creation": {"ephemeral_1h_input_tokens": 4},
                    "output_tokens": 30,
                },
                {"type": "advisor_message", "input_tokens": 3, "output_tokens": 1},
            ],
        }
    )
    return event


@pytest.mark.parametrize("richer_first", [True, False])
def test_advisor_calls_store_identical_results_in_batches(
    tmp_path: Path, richer_first: bool
) -> None:
    parent = {"sessionId": "parent"}
    poorer = tmp_path / "poorer"
    _write(
        poorer / "projects" / "-home-a" / "parent.jsonl",
        [*_parent("parent", []), _advised(parent, "a1", 1)],
    )
    child = {"sessionId": "parent", "agentId": "a", "isSidechain": True}
    _write(
        poorer / "projects" / "-home-a" / "agent-a.jsonl",
        [
            *_child("parent", "a", [], "own")[:1],
            # A sidechain replay of an advised parent response, then its own.
            _advised(child, "a2", 5),
            _advised(child, "own", 6),
        ],
    )
    richer = tmp_path / "richer"
    _write(
        richer / "projects" / "-Users-a" / "parent.jsonl",
        [*_parent("parent", []), _advised(parent, "a1", 1), _advised(parent, "a2", 2)],
    )
    _write(
        richer / "projects" / "-Users-a" / "parent" / "subagents" / "agent-b.jsonl",
        [
            *_child("parent", "b", [], "own-b")[:1],
            _advised(child | {"agentId": "b"}, "b1", 7),
        ],
    )
    sources = [("poorer", poorer), ("richer", richer)]
    if richer_first:
        sources.reverse()

    expected = assert_batches_equal_one_collection(sources, tmp_path)

    calls: dict[str, list[tuple[Any, ...]]] = {}
    for row in expected["model_calls"]:
        calls.setdefault(row["conversation_id"], []).append(
            (
                row["model"],
                row["total_tokens"],
                row["reasoning_output_tokens"],
                row["cache_write_1h_input_tokens"],
            )
        )
    advised = [
        ("claude-sonnet-4-5", 15, 1, 6),
        ("claude-opus-5", 74, 0, 4),
        ("unknown", 4, 0, None),
    ]
    assert calls == {
        "claude:parent": advised * 2,
        # The replayed parent response a2 and its advisors are not counted again.
        "claude:parent:agent:a": advised,
        "claude:parent:agent:b": advised,
    }
    assert _tokens(expected) == {
        "parent": 2 * 93,
        "parent:agent:a": 93,
        "parent:agent:b": 93,
    }
