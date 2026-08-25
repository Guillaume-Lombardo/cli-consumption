from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.models import TOKEN_FIELDS, Snapshot, empty_tokens

OUTSIDE_PROJECT = "outside-project"
TOOL_PATTERN = re.compile(r"(?:tools|collaboration)\.([A-Za-z][A-Za-z0-9_]*)\s*\(")
KNOWN_NESTED_TOOLS = {
    "apply_patch",
    "create_goal",
    "exec_command",
    "get_goal",
    "image_gen__imagegen",
    "list_mcp_resource_templates",
    "list_mcp_resources",
    "read_mcp_resource",
    "update_goal",
    "update_plan",
    "view_image",
    "wait",
    "web__run",
    "write_stdin",
}


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def infer_project(
    metadata: dict[str, Any], mappings: list[tuple[str, str]]
) -> tuple[str, str]:
    cwd = str(metadata.get("cwd") or "").rstrip("/\\")
    normalized_cwd = cwd.replace("\\", "/")
    for name, prefix in sorted(mappings, key=lambda item: len(item[1]), reverse=True):
        normalized_prefix = prefix.replace("\\", "/").rstrip("/")
        if normalized_cwd == normalized_prefix or normalized_cwd.startswith(
            normalized_prefix + "/"
        ):
            return name, "mapping"
    git = metadata.get("git")
    if isinstance(git, dict):
        repository = str(git.get("repository_url") or git.get("repository") or "")
        slug = re.split(r"[/\\:]", repository.rstrip("/\\"))[-1]
        if slug.endswith(".git"):
            slug = slug[:-4]
        if slug:
            return slug, "git"
    return OUTSIDE_PROJECT, "none"


def extract_tools(payload: dict[str, Any]) -> list[tuple[str, str]]:
    outer_name = str(payload.get("name", "unknown"))
    if outer_name != "exec":
        return [(outer_name, outer_name)]
    raw_input = payload.get("input", "")
    if not isinstance(raw_input, str):
        raw_input = json.dumps(raw_input, sort_keys=True)
    nested = [
        name
        for name in TOOL_PATTERN.findall(raw_input)
        if name in KNOWN_NESTED_TOOLS or name.startswith("mcp__")
    ]
    return [(outer_name, name) for name in nested] or [(outer_name, outer_name)]


class CodexAdapter:
    """Read local Codex rollout metadata while excluding message content."""

    name = "codex"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected, duplicates, discovery_malformed = self._discover(sources)
        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=discovery_malformed,
        )
        mappings = project_mappings or []
        for machine, path, event_count, digest in selected:
            self._read_rollout(snapshot, machine, path, event_count, digest, mappings)
        for machine, codex_home in sources:
            snapshot.subagents.extend(
                self._read_subagents(codex_home / "state_5.sqlite", machine)
            )
        return snapshot

    def _read_subagents(
        self, state_path: Path, source_machine: str
    ) -> list[dict[str, Any]]:
        if not state_path.is_file():
            return []
        connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT e.parent_thread_id, e.child_thread_id, e.status,
                       t.created_at_ms, t.updated_at_ms, t.agent_nickname,
                       t.agent_role, t.tokens_used
                FROM thread_spawn_edges e
                LEFT JOIN threads t ON t.id = e.child_thread_id
                ORDER BY t.created_at_ms, e.child_thread_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()
        return [
            {
                "id": f"codex:{source_machine}:{row['child_thread_id']}",
                "provider": self.name,
                "source_machine": source_machine,
                "parent_thread_id": str(row["parent_thread_id"]),
                "child_thread_id": str(row["child_thread_id"]),
                "status": str(row["status"] or "unknown"),
                "created_at_ms": _integer_or_none(row["created_at_ms"]),
                "updated_at_ms": _integer_or_none(row["updated_at_ms"]),
                "agent_nickname": str(row["agent_nickname"] or ""),
                "agent_role": str(row["agent_role"] or ""),
                "tokens_used": _integer_or_none(row["tokens_used"]),
            }
            for row in rows
        ]

    def _discover(
        self, sources: list[tuple[str, Path]]
    ) -> tuple[list[tuple[str, Path, int, str]], int, int]:
        selected: dict[str, tuple[str, Path, int, str]] = {}
        duplicates = 0
        malformed = 0
        for machine, codex_home in sources:
            sessions = codex_home / "sessions"
            if not sessions.is_dir():
                raise ValueError(f"Missing Codex sessions directory: {sessions}")
            for path in sorted(sessions.rglob("*.jsonl")):
                event_count = 0
                conversation_id = ""
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for raw_line in handle:
                        digest.update(raw_line)
                        try:
                            event = json.loads(raw_line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            malformed += 1
                            continue
                        if not isinstance(event, dict):
                            malformed += 1
                            continue
                        event_count += 1
                        if event.get("type") == "session_meta":
                            conversation_id = str(
                                event.get("payload", {}).get("id", "")
                            )
                conversation_id = conversation_id or path.stem
                candidate = (machine, path, event_count, digest.hexdigest())
                previous = selected.get(conversation_id)
                if previous is None:
                    selected[conversation_id] = candidate
                else:
                    duplicates += 1
                    if candidate[2:] > previous[2:]:
                        selected[conversation_id] = candidate
        return list(selected.values()), duplicates, malformed

    def _read_rollout(
        self,
        snapshot: Snapshot,
        machine: str,
        path: Path,
        event_count: int,
        digest: str,
        mappings: list[tuple[str, str]],
    ) -> None:
        events: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)

        metadata: dict[str, Any] = next(
            (
                payload
                for event in events
                if event.get("type") == "session_meta"
                and isinstance((payload := event.get("payload")), dict)
            ),
            {},
        )
        conversation_id = str(metadata.get("id") or path.stem)
        record_id = f"codex:{conversation_id}"
        project, project_source = infer_project(metadata, mappings)
        timestamps = [
            timestamp
            for event in events
            if (timestamp := parse_timestamp(event.get("timestamp"))) is not None
        ]
        started_at = min(timestamps, default=None)
        ended_at = max(timestamps, default=None)
        active_turn_id: str | None = None
        active_model: str | None = None
        turns: dict[str, dict[str, Any]] = {}
        models: set[str] = set()
        totals = empty_tokens()
        call_sequence = 0
        tool_sequence = 0
        compactions = 0

        for event in events:
            timestamp = parse_timestamp(event.get("timestamp"))
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            event_type = event.get("type")
            payload_type = payload.get("type")
            if event_type == "compacted":
                compactions += 1
            if event_type == "turn_context":
                active_turn_id = (
                    str(payload.get("turn_id") or active_turn_id or "") or None
                )
                active_model = str(payload.get("model") or active_model or "") or None
                if active_model:
                    models.add(active_model)
            if event_type == "event_msg" and payload_type == "task_started":
                active_turn_id = str(payload.get("turn_id") or "") or None
                if active_turn_id:
                    turns[active_turn_id] = {
                        "id": f"{record_id}:{active_turn_id}",
                        "conversation_id": record_id,
                        "external_id": active_turn_id,
                        "started_at": _iso(timestamp),
                        "ended_at": None,
                        "status": "in-progress",
                        "duration_ms": None,
                        "time_to_first_token_ms": None,
                        "model_calls": 0,
                        "tool_calls": 0,
                        **empty_tokens(),
                    }
                continue
            if event_type == "event_msg" and payload_type in {
                "task_complete",
                "turn_aborted",
            }:
                turn_id = str(payload.get("turn_id") or active_turn_id or "")
                if turn_id in turns:
                    turns[turn_id].update(
                        ended_at=_iso(timestamp),
                        status="completed"
                        if payload_type == "task_complete"
                        else "aborted",
                        duration_ms=_integer_or_none(payload.get("duration_ms")),
                        time_to_first_token_ms=_integer_or_none(
                            payload.get("time_to_first_token_ms")
                        ),
                    )
                active_turn_id = None
                continue
            if event_type == "event_msg" and payload_type == "token_count":
                info = payload.get("info")
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if not isinstance(usage, dict):
                    continue
                call_sequence += 1
                tokens = {
                    field: int(usage.get(field, 0) or 0) for field in TOKEN_FIELDS
                }
                tokens.update(_derived_tokens(tokens))
                for field, value in tokens.items():
                    totals[field] += value
                turn = turns.get(active_turn_id or "")
                if turn:
                    turn["model_calls"] += 1
                    for field, value in tokens.items():
                        turn[field] += value
                snapshot.model_calls.append(
                    {
                        "id": f"{record_id}:model:{call_sequence}",
                        "conversation_id": record_id,
                        "turn_id": turn["id"] if turn else None,
                        "sequence": call_sequence,
                        "timestamp": _iso(timestamp),
                        "model": active_model or "unknown",
                        **tokens,
                    }
                )
                continue
            if event_type == "response_item" and payload_type in {
                "custom_tool_call",
                "function_call",
            }:
                for outer_name, tool_name in extract_tools(payload):
                    tool_sequence += 1
                    turn = turns.get(active_turn_id or "")
                    if turn:
                        turn["tool_calls"] += 1
                    snapshot.tool_calls.append(
                        {
                            "id": f"{record_id}:tool:{tool_sequence}",
                            "conversation_id": record_id,
                            "turn_id": turn["id"] if turn else None,
                            "sequence": tool_sequence,
                            "timestamp": _iso(timestamp),
                            "tool_name": tool_name,
                            "outer_tool_name": outer_name,
                        }
                    )

        for turn in turns.values():
            if turn["ended_at"] is None:
                turn["ended_at"] = _iso(ended_at)
            snapshot.turns.append(turn)
        snapshot.conversations.append(
            {
                "id": record_id,
                "provider": self.name,
                "external_id": conversation_id,
                "source_machine": machine,
                "project": project,
                "project_source": project_source,
                "started_at": _iso(started_at),
                "ended_at": _iso(ended_at),
                "duration_seconds": (
                    (ended_at - started_at).total_seconds()
                    if started_at is not None and ended_at is not None
                    else None
                ),
                "source": str(metadata.get("source") or ""),
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": call_sequence,
                "tool_calls": tool_sequence,
                "compactions": compactions,
                "event_count": event_count,
                "content_hash": digest,
                **totals,
            }
        )


def _derived_tokens(tokens: dict[str, int]) -> dict[str, int]:
    return {
        "uncached_input_tokens": max(
            0,
            tokens["input_tokens"]
            - tokens["cached_input_tokens"]
            - tokens["cache_write_input_tokens"],
        ),
        "visible_output_tokens": max(
            0, tokens["output_tokens"] - tokens["reasoning_output_tokens"]
        ),
        "unattributed_tokens": max(
            0,
            tokens["total_tokens"] - tokens["input_tokens"] - tokens["output_tokens"],
        ),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _integer_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None
