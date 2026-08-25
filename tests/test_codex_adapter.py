from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from cli_consumption.adapters.codex import CodexAdapter, infer_project


def test_collects_and_deduplicates_copied_rollouts(
    tmp_path: Path, rollout_factory
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    rollout_factory(first)
    rollout_factory(second, extra_event=True)

    snapshot = CodexAdapter().collect(
        [("desktop", first), ("laptop", second)],
        [("acme", "/srv/work/acme")],
    )

    assert snapshot.duplicate_conversations == 1
    assert len(snapshot.conversations) == 1
    conversation = snapshot.conversations[0]
    assert conversation["source_machine"] == "laptop"
    assert conversation["project"] == "acme"
    assert conversation["models"] == ["gpt-5.6"]
    assert conversation["uncached_input_tokens"] == 50
    assert conversation["visible_output_tokens"] == 15
    assert snapshot.model_calls[0]["model"] == "gpt-5.6"
    assert snapshot.tool_calls[0]["tool_name"] == "exec_command"
    assert "secret value" not in str(snapshot.to_dict())


def test_project_inference_falls_back_to_repository_then_safe_category() -> None:
    assert infer_project({"git": {"repository_url": "git@host:org/repo.git"}}, []) == (
        "repo",
        "git",
    )
    assert infer_project({"cwd": "/plain/directory"}, []) == (
        "outside-project",
        "none",
    )


def test_missing_sessions_directory_is_rejected(tmp_path: Path) -> None:
    try:
        CodexAdapter().collect([("machine", tmp_path)])
    except ValueError as error:
        assert "Missing Codex sessions directory" in str(error)
    else:
        raise AssertionError("Expected a missing sessions directory error")


def test_malformed_and_non_object_records_are_counted(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    path = rollout_factory(home)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")

    snapshot = CodexAdapter().collect([("machine", home)])

    assert snapshot.malformed_records == 2
    assert len(snapshot.conversations) == 1


def test_subagent_metadata_excludes_agent_paths(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
        connection.executescript(
            """
            CREATE TABLE thread_spawn_edges (
                parent_thread_id, child_thread_id, status
            );
            CREATE TABLE threads (
                id, created_at_ms, updated_at_ms, agent_nickname,
                agent_role, agent_path, tokens_used
            );
            INSERT INTO thread_spawn_edges VALUES ('parent', 'child', 'done');
            INSERT INTO threads VALUES (
                'child', 1, 2, 'worker', 'tester', '/private/agent/path', 42
            );
            """
        )
        connection.commit()

    snapshot = CodexAdapter().collect([("desktop", home)])

    assert snapshot.subagents[0]["child_thread_id"] == "child"
    assert snapshot.subagents[0]["tokens_used"] == 42
    assert "agent_path" not in snapshot.subagents[0]
    assert "/private/agent/path" not in str(snapshot.to_dict())
