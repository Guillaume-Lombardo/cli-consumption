from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from cli_consumption.adapters.codex import (
    MAX_BIGINT,
    CodexAdapter,
    _agent_role,
    _integer_or_none,
    _subagent_status,
    _work_item_status,
    infer_project,
)


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
    assert conversation["source"] == "local-jsonl"
    assert conversation["models"] == ["gpt-5.6"]
    assert conversation["uncached_input_tokens"] == 50
    assert conversation["visible_output_tokens"] == 15
    assert snapshot.model_calls[0]["model"] == "gpt-5.6"
    assert snapshot.tool_calls[0]["tool_name"] == "exec_command"
    assert snapshot.work_items == [
        {
            "id": "codex:conversation-1:work:1",
            "conversation_id": "codex:conversation-1",
            "turn_id": "codex:conversation-1:turn-1",
            "sequence": 1,
            "kind": "command",
            "tool_name": None,
            "started_at_ms": 1000,
            "completed_at_ms": 2000,
            "duration_ms": 1000,
            "status": "completed",
        }
    ]
    assert snapshot.context_samples[0]["input_tokens"] == 100
    assert snapshot.context_samples[0]["context_window_tokens"] == 1000
    assert snapshot.turn_settings[0]["effort"] == "high"
    assert snapshot.turn_settings[0]["collaboration_mode"] == "default"
    assert snapshot.turn_settings[0]["service_tier"] == "priority"
    assert snapshot.compaction_events[0]["timestamp"] == "2026-08-25T10:00:05+00:00"
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
                'child', 1, 2, 'private nickname', 'tester',
                '/private/agent/path', 42
            );
            """
        )
        connection.commit()

    snapshot = CodexAdapter().collect([("desktop", home)])

    assert snapshot.subagents[0]["child_thread_id"] == "child"
    assert snapshot.subagents[0]["tokens_used"] == 42
    assert snapshot.subagents[0]["status"] == "completed"
    assert snapshot.subagents[0]["agent_role"] == "test"
    assert "agent_nickname" not in snapshot.subagents[0]
    assert "private nickname" not in str(snapshot.to_dict())
    assert "agent_path" not in snapshot.subagents[0]
    assert "/private/agent/path" not in str(snapshot.to_dict())


def test_subagent_dimensions_and_rollout_source_discard_arbitrary_values(
    tmp_path: Path, rollout_factory
) -> None:
    canary = "PRIVACY_CANARY_DO_NOT_PERSIST"
    home = tmp_path / "codex"
    rollout = rollout_factory(home)
    events = [
        json.loads(line) for line in rollout.read_text(encoding="utf-8").splitlines()
    ]
    events[0]["payload"]["source"] = canary
    rollout.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
        connection.executescript(
            f"""
            CREATE TABLE thread_spawn_edges (
                parent_thread_id, child_thread_id, status
            );
            CREATE TABLE threads (
                id, created_at_ms, updated_at_ms, agent_nickname,
                agent_role, agent_path, tokens_used
            );
            INSERT INTO thread_spawn_edges VALUES ('parent', 'child', '{canary}');
            INSERT INTO threads VALUES (
                'child', 1, 2, '{canary}', '{canary}', '{canary}', 42
            );
            """
        )
        connection.commit()

    snapshot = CodexAdapter().collect([("desktop", home)])

    assert snapshot.conversations[0]["source"] == "local-jsonl"
    assert snapshot.subagents[0]["status"] == "unknown"
    assert snapshot.subagents[0]["agent_role"] == "other"
    assert canary not in str(snapshot.to_dict())


def test_work_items_normalize_failures_and_reject_arbitrary_dimensions(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    path = rollout_factory(home)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"timestamp":"2026-08-25T10:00:06Z","type":"event_msg",'
            '"payload":{"type":"item_completed","turn_id":"turn-1",'
            '"started_at_ms":3000,"completed_at_ms":2500,"item":{'
            '"type":"McpToolCall","tool":"secret value with spaces",'
            '"status":"completed","error":{"secret":"privacy canary"}}}}\n'
        )
        handle.write(
            '{"timestamp":"2026-08-25T10:00:07Z","type":"turn_context",'
            '"payload":{"turn_id":"turn-1","model":"privacy canary",'
            '"effort":"privacy canary","collaboration_mode":{'
            '"mode":"privacy canary","settings":{"secret":"privacy canary"}}}}\n'
        )
        handle.write(
            '{"timestamp":"2026-08-25T10:00:08Z","type":"event_msg",'
            '"payload":{"type":"token_count","info":{'
            '"model_context_window":Infinity,"last_token_usage":{'
            '"input_tokens":Infinity,"cached_input_tokens":-1,'
            '"output_tokens":"privacy canary","total_tokens":Infinity}}}}\n'
        )

    snapshot = CodexAdapter().collect([("machine", home)])

    failed = snapshot.work_items[-1]
    assert failed["kind"] == "mcp-tool"
    assert failed["tool_name"] is None
    assert failed["duration_ms"] == 0
    assert failed["status"] == "failed"
    assert snapshot.turn_settings[0]["model"] == "gpt-5.6"
    assert snapshot.turn_settings[0]["effort"] == "medium"
    assert snapshot.model_calls[-1]["total_tokens"] == 0
    assert len(snapshot.context_samples) == 1
    assert "privacy canary" not in str(snapshot.to_dict())


def test_numeric_and_work_status_normalizers_are_bounded() -> None:
    assert _integer_or_none(True) is None
    assert _integer_or_none(float("inf")) is None
    assert _integer_or_none(MAX_BIGINT + 1) is None
    assert _work_item_status({"status": "running"}) == "in-progress"
    assert _work_item_status({"status": "unexpected"}) == "unknown"
    assert _work_item_status({"success": False}) == "failed"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("done", "completed"),
        ("error", "failed"),
        ("cancelled", "aborted"),
        ("in_progress", "in-progress"),
        ("privacy-canary", "unknown"),
        (None, "unknown"),
    ],
)
def test_subagent_status_normalizer_is_closed(value: object, expected: str) -> None:
    assert _subagent_status(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("worker", "worker"),
        ("explorer", "research"),
        ("reviewer", "review"),
        ("tester", "test"),
        ("planner", "planning"),
        ("privacy-canary", "other"),
        ("", "unspecified"),
        (None, "unspecified"),
    ],
)
def test_agent_role_normalizer_is_closed(value: object, expected: str) -> None:
    assert _agent_role(value) == expected
