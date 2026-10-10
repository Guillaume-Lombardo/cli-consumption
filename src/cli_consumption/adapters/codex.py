from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Any

from cli_consumption import models
from cli_consumption.adapters._incremental import (
    INCREMENTAL_CANDIDATES_PER_BATCH as INCREMENTAL_CANDIDATES_PER_BATCH,
)
from cli_consumption.adapters._incremental import iter_source_batches
from cli_consumption.adapters._shared import (
    MAX_BIGINT as MAX_BIGINT,
)
from cli_consumption.adapters._shared import (
    ProviderInputBudget,
    iter_bounded_jsonl_bytes,
    open_provider_sqlite,
)
from cli_consumption.adapters.base import CollectionBatch
from cli_consumption.models import (
    TOKEN_FIELDS,
    Snapshot,
    empty_tokens,
)

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
WORK_ITEM_KINDS = {
    "AgentMessage": "message",
    "CollabAgentToolCall": "agent-coordination",
    "CommandExecution": "command",
    "ContextCompaction": "compaction",
    "DynamicToolCall": "dynamic-tool",
    "Extension": "extension",
    "FileChange": "file-change",
    "ImageView": "media",
    "McpToolCall": "mcp-tool",
    "Reasoning": "reasoning",
    "SubAgentActivity": "subagent-activity",
    "UserMessage": "user-message",
}
SUBAGENT_STATUS_ALIASES = {
    "aborted": "aborted",
    "active": "in-progress",
    "canceled": "aborted",
    "cancelled": "aborted",
    "complete": "completed",
    "completed": "completed",
    "done": "completed",
    "error": "failed",
    "errored": "failed",
    "failed": "failed",
    "failure": "failed",
    "in-progress": "in-progress",
    "interrupted": "aborted",
    "pending": "in-progress",
    "running": "in-progress",
    "succeeded": "completed",
    "success": "completed",
    "unknown": "unknown",
}
AGENT_ROLE_ALIASES = {
    "explorer": "research",
    "implementer": "worker",
    "planner": "planning",
    "planning": "planning",
    "research": "research",
    "researcher": "research",
    "review": "review",
    "reviewer": "review",
    "test": "test",
    "tester": "test",
    "worker": "worker",
}
SAFE_DIMENSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")


SESSIONS_SUBDIR = "sessions"
ARCHIVED_SESSIONS_SUBDIR = "archived_sessions"


def _rollout_roots(codex_home: Path) -> tuple[Path, ...]:
    """Return the active and archived rollout trees of one Codex home, in order.

    Codex archiving moves the rollout files of a thread and of its archived
    descendant threads from the dated ``sessions/YYYY/MM/DD/`` tree to
    ``archived_sessions/``, and unarchiving moves them back. Archiving writes them
    flat, while Codex's own rollout lookups walk ``archived_sessions/`` recursively,
    so both layouts are read with the same walk. ``sessions/`` stays required;
    ``archived_sessions/`` is optional because Codex creates it on first archive.

    ``sessions/`` keeps its existing semantics, including a symlinked root. Codex
    itself only ever creates ``archived_sessions/`` as a real directory, so a
    symlink there is skipped rather than followed outside the selected home.
    """
    sessions = codex_home / SESSIONS_SUBDIR
    if not sessions.is_dir():
        raise ValueError("Missing Codex sessions directory")
    archived = codex_home / ARCHIVED_SESSIONS_SUBDIR
    return (sessions, archived) if _is_real_directory(archived) else (sessions,)


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _incremental_session_files(codex_home: Path) -> Iterator[Path]:
    # Resolve the roots eagerly so that a missing sessions directory fails at once.
    return chain.from_iterable(
        _iter_session_files(root) for root in _rollout_roots(codex_home)
    )


def _iter_session_files(root: Path) -> Iterator[Path]:
    """Walk a provider tree deterministically without materializing every path."""
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        child_directories: list[Path] = []
        for entry in ordered:
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                child_directories.append(path)
            elif entry.name.endswith(".jsonl"):
                yield path
        pending.extend(reversed(child_directories))


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def infer_project(
    metadata: dict[str, Any], mappings: list[tuple[str, str]]
) -> tuple[str, str]:
    raw_cwd = metadata.get("cwd")
    cwd = raw_cwd.rstrip("/\\") if isinstance(raw_cwd, str) else ""
    normalized_cwd = cwd.replace("\\", "/")
    for name, prefix in sorted(mappings, key=lambda item: len(item[1]), reverse=True):
        normalized_prefix = prefix.replace("\\", "/").rstrip("/")
        if normalized_cwd == normalized_prefix or normalized_cwd.startswith(
            normalized_prefix + "/"
        ):
            return name, "mapping"
    git = metadata.get("git")
    if isinstance(git, dict):
        raw_repository = git.get("repository_url") or git.get("repository")
        repository = raw_repository if isinstance(raw_repository, str) else ""
        slug = re.split(r"[/\\:]", repository.rstrip("/\\"))[-1]
        if slug.endswith(".git"):
            slug = slug[:-4]
        if _safe_dimension(slug, 255):
            return slug, "git"
    return OUTSIDE_PROJECT, "none"


def extract_tools(payload: dict[str, Any]) -> list[tuple[str, str]]:
    outer_name = _safe_dimension(payload.get("name"), 512) or "unknown"
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


_ROLLOUT_COLLECTIONS = (
    "conversations",
    "turns",
    "model_calls",
    "tool_calls",
    "work_items",
    "context_samples",
    "turn_settings",
    "compaction_events",
)


def _load_rollout(
    path: Path, budget: ProviderInputBudget
) -> tuple[list[dict[str, Any]], str, str, int]:
    digest = hashlib.sha256()
    events: list[dict[str, Any]] = []
    malformed = 0
    conversation_id = ""
    for raw_line in iter_bounded_jsonl_bytes(path, budget):
        digest.update(raw_line)
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        events.append(event)
        if event.get("type") == "session_meta":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                malformed += 1
                continue
            candidate_id = _safe_dimension(payload.get("id"), 512)
            if candidate_id and not conversation_id:
                conversation_id = candidate_id
    content_hash = digest.hexdigest()
    return events, content_hash, conversation_id or f"content-{content_hash}", malformed


def _merge_rollouts(
    provider: str, selected: list[Snapshot], duplicates: int, malformed: int
) -> Snapshot:
    snapshot = Snapshot(
        provider=provider,
        duplicate_conversations=duplicates,
        malformed_records=malformed,
    )
    for records in selected:
        for name in _ROLLOUT_COLLECTIONS:
            getattr(snapshot, name).extend(getattr(records, name))
    return snapshot


class CodexAdapter:
    """Read local Codex rollout metadata while excluding message content."""

    name = "codex"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected, duplicates, discovery_malformed = self._discover(
            sources, project_mappings or [], budget
        )
        snapshot = _merge_rollouts(self.name, selected, duplicates, discovery_malformed)
        for machine, codex_home in sources:
            snapshot.subagents.extend(
                self._read_subagents(codex_home / "state_5.sqlite", machine, budget)
            )
        return snapshot

    def collect_incrementally(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Iterator[CollectionBatch]:
        """Yield deterministic, bounded snapshots from arbitrarily many rollouts."""
        mappings = project_mappings or []
        return iter_source_batches(
            self.name,
            sources,
            _incremental_session_files,
            lambda candidates: self._collect_candidates(candidates, mappings),
            candidates_per_batch=INCREMENTAL_CANDIDATES_PER_BATCH,
        )

    def _collect_candidates(
        self,
        candidates: list[tuple[str, Path]],
        mappings: list[tuple[str, str]],
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected, duplicates, malformed = self._discover_candidates(
            candidates, mappings, budget
        )
        return _merge_rollouts(self.name, selected, duplicates, malformed)

    def _read_subagents(
        self,
        state_path: Path,
        source_machine: str,
        budget: ProviderInputBudget,
    ) -> list[dict[str, Any]]:
        if not budget.candidate(state_path).is_file():
            return []
        manager = open_provider_sqlite(state_path, budget)
        connection = manager.__enter__()
        connection.row_factory = sqlite3.Row
        try:
            rows = list(
                budget.rows(
                    connection.execute(
                        """
                SELECT e.parent_thread_id, e.child_thread_id, e.status,
                       t.created_at_ms, t.updated_at_ms, t.agent_role,
                       t.tokens_used
                FROM thread_spawn_edges e
                LEFT JOIN threads t ON t.id = e.child_thread_id
                ORDER BY t.created_at_ms, e.child_thread_id
                        """
                    )
                )
            )
        except sqlite3.OperationalError:
            return []
        finally:
            manager.__exit__(None, None, None)
        return [
            {
                "id": f"codex:{source_machine}:{row['child_thread_id']}",
                "provider": self.name,
                "source_machine": source_machine,
                "parent_thread_id": str(row["parent_thread_id"]),
                "child_thread_id": str(row["child_thread_id"]),
                "status": _subagent_status(row["status"]),
                "created_at_ms": _integer_or_none(row["created_at_ms"]),
                "updated_at_ms": _integer_or_none(row["updated_at_ms"]),
                "agent_role": _agent_role(row["agent_role"]),
                "tokens_used": _integer_or_none(row["tokens_used"]),
            }
            for row in rows
        ]

    def _discover(
        self,
        sources: list[tuple[str, Path]],
        mappings: list[tuple[str, str]],
        budget: ProviderInputBudget,
    ) -> tuple[list[Snapshot], int, int]:
        candidates: list[tuple[str, Path]] = []
        for machine, codex_home in sources:
            # The single pass lists exactly the files that batches would visit.
            for root in _rollout_roots(codex_home):
                candidates.extend(
                    (machine, path)
                    for path in budget.sorted_paths(_iter_session_files(root))
                )
        return self._discover_candidates(
            candidates, mappings, budget, charge_candidates=False
        )

    def _discover_candidates(
        self,
        candidates: list[tuple[str, Path]],
        mappings: list[tuple[str, str]],
        budget: ProviderInputBudget,
        *,
        charge_candidates: bool = True,
    ) -> tuple[list[Snapshot], int, int]:
        # Each rollout is read once. Only the normalized, content-free records of
        # the most complete copy of each conversation are retained between files.
        selected: dict[str, tuple[tuple[int, str], Snapshot]] = {}
        retained_records = 0
        duplicates = 0
        malformed = 0
        for machine, path in candidates:
            if charge_candidates:
                budget.item()
            events, content_hash, conversation_id, invalid = _load_rollout(path, budget)
            malformed += invalid
            rank = (len(events), content_hash)
            previous = selected.get(conversation_id)
            if previous is not None:
                duplicates += 1
                if rank <= previous[0]:
                    del events
                    continue
            if previous is not None:
                retained_records -= sum(
                    len(getattr(previous[1], name)) for name in _ROLLOUT_COLLECTIONS
                )
                for name in _ROLLOUT_COLLECTIONS:
                    getattr(previous[1], name).clear()
                previous = None
            records = Snapshot(
                provider=self.name,
                _record_limit=models.MAX_SNAPSHOT_RECORDS - retained_records,
            )
            self._read_rollout(records, machine, events, rank, mappings)
            del events
            retained_records += sum(
                len(getattr(records, name)) for name in _ROLLOUT_COLLECTIONS
            )
            selected[conversation_id] = (rank, records)
        return [records for _, records in selected.values()], duplicates, malformed

    def _read_rollout(
        self,
        snapshot: Snapshot,
        machine: str,
        events: list[dict[str, Any]],
        selection: tuple[int, str],
        mappings: list[tuple[str, str]],
    ) -> None:
        event_count, digest = selection

        metadata: dict[str, Any] = next(
            (
                payload
                for event in events
                if event.get("type") == "session_meta"
                and isinstance((payload := event.get("payload")), dict)
            ),
            {},
        )
        conversation_id = next(
            (
                candidate_id
                for event in events
                if event.get("type") == "session_meta"
                and isinstance((payload := event.get("payload")), dict)
                and (candidate_id := _safe_dimension(payload.get("id"), 512))
            ),
            f"content-{digest}",
        )
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
        work_sequence = 0
        compaction_sequence = 0
        compactions = 0
        setting_defaults: dict[str, str | int | None] = {
            "model": None,
            "effort": None,
            "collaboration_mode": None,
            "service_tier": None,
            "context_window_tokens": None,
        }
        settings_by_turn: dict[str, dict[str, str | int | None]] = {}

        for event in events:
            timestamp = parse_timestamp(event.get("timestamp"))
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            event_type = event.get("type")
            payload_type = payload.get("type")
            if event_type == "compacted":
                compactions += 1
                compaction_sequence += 1
                snapshot.compaction_events.append(
                    {
                        "id": f"{record_id}:compaction:{compaction_sequence}",
                        "conversation_id": record_id,
                        "turn_id": turns.get(active_turn_id or "", {}).get("id"),
                        "sequence": compaction_sequence,
                        "timestamp": _iso(timestamp),
                    }
                )
            if event_type == "event_msg" and payload_type == "thread_settings_applied":
                raw_settings = payload.get("thread_settings")
                if isinstance(raw_settings, dict):
                    updates: dict[str, str | int | None] = {
                        "model": _safe_dimension(raw_settings.get("model"), 255),
                        "effort": _safe_dimension(
                            raw_settings.get("reasoning_effort"), 64
                        ),
                        "collaboration_mode": _collaboration_mode(
                            raw_settings.get("collaboration_mode")
                        ),
                        "service_tier": _safe_dimension(
                            raw_settings.get("service_tier"), 64
                        ),
                    }
                    _merge_present(setting_defaults, updates)
                    if active_turn_id and active_turn_id in settings_by_turn:
                        _merge_present(settings_by_turn[active_turn_id], updates)
            if event_type == "turn_context":
                active_turn_id = (
                    _safe_dimension(payload.get("turn_id"), 512) or active_turn_id
                )
                active_model = (
                    _safe_dimension(payload.get("model"), 255) or active_model
                )
                if active_model:
                    models.add(active_model)
                if active_turn_id:
                    settings_by_turn[active_turn_id] = {
                        **setting_defaults,
                        "model": active_model,
                        "effort": _safe_dimension(payload.get("effort"), 64)
                        or setting_defaults["effort"],
                        "collaboration_mode": _collaboration_mode(
                            payload.get("collaboration_mode")
                        )
                        or setting_defaults["collaboration_mode"],
                    }
            if event_type == "event_msg" and payload_type == "task_started":
                active_turn_id = _safe_dimension(payload.get("turn_id"), 512)
                if active_turn_id:
                    settings = settings_by_turn.setdefault(
                        active_turn_id, dict(setting_defaults)
                    )
                    settings["model"] = active_model or settings["model"]
                    context_window = _positive_integer_or_none(
                        payload.get("model_context_window")
                    )
                    if context_window is not None:
                        settings["context_window_tokens"] = context_window
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
                turn_id = (
                    _safe_dimension(payload.get("turn_id"), 512) or active_turn_id or ""
                )
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
            if event_type == "event_msg" and payload_type == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict):
                    continue
                work_sequence += 1
                started_at_ms = _integer_or_none(payload.get("started_at_ms"))
                completed_at_ms = _integer_or_none(payload.get("completed_at_ms"))
                turn_id = _safe_dimension(payload.get("turn_id") or active_turn_id, 512)
                turn = turns.get(turn_id or "")
                snapshot.work_items.append(
                    {
                        "id": f"{record_id}:work:{work_sequence}",
                        "conversation_id": record_id,
                        "turn_id": turn["id"] if turn else None,
                        "sequence": work_sequence,
                        "kind": WORK_ITEM_KINDS.get(
                            str(item.get("type") or ""), "other"
                        ),
                        "tool_name": _safe_dimension(item.get("tool"), 512),
                        "started_at_ms": started_at_ms,
                        "completed_at_ms": completed_at_ms,
                        "duration_ms": _interval_duration(
                            started_at_ms, completed_at_ms
                        ),
                        "status": _work_item_status(item),
                    }
                )
                continue
            if event_type == "event_msg" and payload_type == "token_count":
                info = payload.get("info")
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if not isinstance(usage, dict):
                    continue
                call_sequence += 1
                tokens = _usage_tokens(usage)
                _accumulate_tokens(totals, tokens)
                turn = turns.get(active_turn_id or "")
                if turn:
                    turn["model_calls"] += 1
                    _accumulate_tokens(turn, tokens)
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
                context_window = _positive_integer_or_none(
                    info.get("model_context_window")
                )
                if context_window is not None:
                    if active_turn_id and active_turn_id in settings_by_turn:
                        settings_by_turn[active_turn_id]["context_window_tokens"] = (
                            context_window
                        )
                    snapshot.context_samples.append(
                        {
                            "id": f"{record_id}:context:{call_sequence}",
                            "conversation_id": record_id,
                            "turn_id": turn["id"] if turn else None,
                            "sequence": call_sequence,
                            "timestamp": _iso(timestamp),
                            "input_tokens": max(0, tokens["input_tokens"]),
                            "context_window_tokens": context_window,
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
            external_turn_id = str(turn["external_id"])
            settings = settings_by_turn.get(external_turn_id, setting_defaults)
            snapshot.turn_settings.append(
                {
                    "id": f"{record_id}:settings:{external_turn_id}",
                    "conversation_id": record_id,
                    "turn_id": str(turn["id"]),
                    "model": settings["model"],
                    "effort": settings["effort"],
                    "collaboration_mode": settings["collaboration_mode"],
                    "service_tier": settings["service_tier"],
                    "context_window_tokens": settings["context_window_tokens"],
                }
            )
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
                "source": "local-jsonl",
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


def _usage_tokens(usage: dict[str, Any]) -> dict[str, int]:
    raw = {field: _nonnegative_integer(usage.get(field)) for field in TOKEN_FIELDS}
    cached = raw["cached_input_tokens"]
    cache_write = min(raw["cache_write_input_tokens"], MAX_BIGINT - cached)
    input_tokens = max(raw["input_tokens"], cached + cache_write)
    uncached = input_tokens - cached - cache_write
    remaining = MAX_BIGINT - input_tokens
    reasoning = min(raw["reasoning_output_tokens"], remaining)
    visible = min(
        max(0, raw["output_tokens"] - raw["reasoning_output_tokens"]),
        remaining - reasoning,
    )
    output_tokens = reasoning + visible
    unattributed = min(
        max(0, raw["total_tokens"] - input_tokens - output_tokens),
        MAX_BIGINT - input_tokens - output_tokens,
    )
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output_tokens + unattributed,
        "uncached_input_tokens": uncached,
        "visible_output_tokens": visible,
        "unattributed_tokens": unattributed,
    }


def _accumulate_tokens(target: dict[str, Any], value: dict[str, int]) -> None:
    remaining = MAX_BIGINT
    for field in (
        "uncached_input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "visible_output_tokens",
        "reasoning_output_tokens",
        "unattributed_tokens",
    ):
        amount = min(remaining, int(target[field]) + value[field])
        target[field] = amount
        remaining -= amount
    target["input_tokens"] = (
        target["uncached_input_tokens"]
        + target["cached_input_tokens"]
        + target["cache_write_input_tokens"]
    )
    target["output_tokens"] = (
        target["visible_output_tokens"] + target["reasoning_output_tokens"]
    )
    target["total_tokens"] = (
        target["input_tokens"] + target["output_tokens"] + target["unattributed_tokens"]
    )


def _safe_dimension(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized if SAFE_DIMENSION.fullmatch(normalized) else None


def _subagent_status(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().casefold().replace("_", "-")
    return SUBAGENT_STATUS_ALIASES.get(normalized, "unknown")


def _agent_role(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unspecified"
    normalized = value.strip().casefold().replace("_", "-")
    if normalized in {"unknown", "unspecified"}:
        return "unspecified"
    return AGENT_ROLE_ALIASES.get(normalized, "other")


def _merge_present(
    target: dict[str, str | int | None], updates: dict[str, str | int | None]
) -> None:
    target.update((key, value) for key, value in updates.items() if value is not None)


def _collaboration_mode(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("mode")
    return _safe_dimension(value, 64)


def _work_item_status(item: dict[str, Any]) -> str:
    exit_code = item.get("exit_code")
    if (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
    ):
        return "failed"
    if item.get("success") is False or bool(item.get("error")):
        return "failed"
    status = str(item.get("status") or "").lower().replace("_", "-")
    if status in {"completed", "success", "succeeded"}:
        return "completed"
    if status in {"failed", "error", "errored"}:
        return "failed"
    if status in {"in-progress", "running", "pending"}:
        return "in-progress"
    return "unknown"


def _interval_duration(
    started_at_ms: int | None, completed_at_ms: int | None
) -> int | None:
    if started_at_ms is None or completed_at_ms is None:
        return None
    return max(0, completed_at_ms - started_at_ms)


def _positive_integer_or_none(value: object) -> int | None:
    parsed = _integer_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _integer_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    parsed = int(value)
    return parsed if -MAX_BIGINT <= parsed <= MAX_BIGINT else None


def _nonnegative_integer(value: object) -> int:
    parsed = (
        _integer_or_none(value)
        if not isinstance(value, float) or value.is_integer()
        else None
    )
    return max(0, parsed or 0)
