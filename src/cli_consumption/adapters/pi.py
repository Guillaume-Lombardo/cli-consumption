from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderInputBudget,
    iter_bounded_jsonl_bytes,
)
from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")
THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}


@dataclass(slots=True)
class _Session:
    machine: str
    external_id: str
    cwd: str | None
    entries: list[dict[str, Any]]
    event_count: int
    digest: str


class PiAdapter:
    """Read Pi session metadata while discarding conversation content."""

    name = "pi"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Session] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            sessions = home / "sessions"
            if not sessions.is_dir():
                raise ValueError(f"Missing Pi sessions directory: {sessions}")
            for path in budget.sorted_paths(sessions.rglob("*.jsonl")):
                candidate, invalid = _read_session(path, machine)
                malformed += invalid
                if candidate is None:
                    continue
                previous = selected.get(candidate.external_id)
                if previous is None:
                    selected[candidate.external_id] = candidate
                    continue
                duplicates += 1
                if _rank(candidate) > _rank(previous):
                    selected[candidate.external_id] = candidate

        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=malformed,
        )
        for session in sorted(selected.values(), key=lambda value: value.external_id):
            self._normalize(snapshot, session, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Session,
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"pi:{source.external_id}"
        entry_by_id = {
            identifier: entry
            for entry in source.entries
            if (identifier := _label(entry.get("id"), 512)) is not None
        }
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        turn_efforts: dict[str, str | None] = {}
        active: str | None = None
        calls: list[tuple[str | None, datetime | None, str, dict[str, int]]] = []
        tools: list[tuple[str | None, datetime | None, str]] = []
        compactions: list[tuple[str | None, datetime | None]] = []
        timestamps: list[datetime | None] = []

        for entry in source.entries:
            timestamp = _timestamp(entry.get("timestamp"))
            timestamps.append(timestamp)
            message = _mapping(entry.get("message"))
            role = message.get("role") if entry.get("type") == "message" else None

            if role == "user":
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = _label(entry.get("id"), 512) or f"turn-{len(turns) + 1}"
                if external_id in turns:
                    external_id = f"turn-{len(turns) + 1}"
                active = external_id
                turns[external_id] = {
                    "id": f"{conversation_id}:{external_id}",
                    "conversation_id": conversation_id,
                    "external_id": external_id,
                    "started_at": _iso(timestamp),
                    "ended_at": None,
                    "status": "in-progress",
                    "duration_ms": None,
                    "time_to_first_token_ms": None,
                    "model_calls": 0,
                    "tool_calls": 0,
                    **empty_tokens(),
                }
                turn_models[external_id] = set()
                turn_efforts[external_id] = _ancestral_effort(entry, entry_by_id)
                continue

            turn_key = _ancestral_turn(entry, entry_by_id) or active
            if entry.get("type") == "compaction":
                compactions.append((turn_key, timestamp))

            if role == "assistant":
                model = (
                    _model(message) or _ancestral_model(entry, entry_by_id) or "unknown"
                )
                tokens = _usage(message.get("usage"))
                calls.append((turn_key, timestamp, model, tokens))
                turn = turns.get(turn_key or "")
                if turn:
                    turn["ended_at"] = _iso(timestamp) or turn["ended_at"]
                    stop_reason = message.get("stopReason")
                    if stop_reason in {"error", "aborted"}:
                        turn["status"] = "aborted"
                    elif stop_reason in {"stop", "length"}:
                        turn["status"] = "completed"

                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if (
                            not isinstance(block, dict)
                            or block.get("type") != "toolCall"
                        ):
                            continue
                        name = _label(block.get("name"), 512)
                        if name:
                            tools.append((turn_key, timestamp, name))

            if entry.get("type") in {"compaction", "branch_summary"} and isinstance(
                entry.get("usage"), dict
            ):
                calls.append(
                    (
                        turn_key,
                        timestamp,
                        _ancestral_model(entry, entry_by_id) or "unknown",
                        _usage(entry.get("usage")),
                    )
                )

        ended_at = max(
            (value for value in timestamps if value is not None), default=None
        )
        for turn in turns.values():
            _finish_turn(turn, ended_at)

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (turn_key, timestamp, model, tokens) in enumerate(calls, 1):
            models.add(model)
            _add_tokens(totals, tokens)
            turn = turns.get(turn_key or "")
            if turn:
                turn["model_calls"] += 1
                turn_models[turn_key or ""].add(model)
                _add_tokens(turn, tokens)
            snapshot.model_calls.append(
                {
                    "id": f"{conversation_id}:model:{sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": _iso(timestamp),
                    "model": model,
                    **tokens,
                }
            )

        for sequence, (turn_key, timestamp, name) in enumerate(tools, 1):
            turn = turns.get(turn_key or "")
            if turn:
                turn["tool_calls"] += 1
            snapshot.tool_calls.append(
                {
                    "id": f"{conversation_id}:tool:{sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": _iso(timestamp),
                    "tool_name": name,
                    "outer_tool_name": name,
                }
            )

        for sequence, (turn_key, timestamp) in enumerate(compactions, 1):
            turn = turns.get(turn_key or "")
            snapshot.compaction_events.append(
                {
                    "id": f"{conversation_id}:compaction:{sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": _iso(timestamp),
                }
            )

        for key, turn in turns.items():
            snapshot.turns.append(turn)
            observed = turn_models[key]
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:settings:{key}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"],
                    "model": next(iter(observed)) if len(observed) == 1 else None,
                    "effort": turn_efforts[key],
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": None,
                }
            )

        started_at = min(
            (value for value in timestamps if value is not None), default=None
        )
        project, project_source = _project(source.cwd, mappings)
        snapshot.conversations.append(
            {
                "id": conversation_id,
                "provider": self.name,
                "external_id": source.external_id,
                "source_machine": source.machine,
                "project": project,
                "project_source": project_source,
                "started_at": _iso(started_at),
                "ended_at": _iso(ended_at),
                "duration_seconds": (
                    (ended_at - started_at).total_seconds()
                    if started_at and ended_at
                    else None
                ),
                "source": "local-jsonl",
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": len(calls),
                "tool_calls": len(tools),
                "compactions": len(compactions),
                "event_count": source.event_count,
                "content_hash": source.digest,
                **totals,
            }
        )


def _read_session(path: Path, machine: str) -> tuple[_Session | None, int]:
    digest = hashlib.sha256()
    entries: list[dict[str, Any]] = []
    malformed = 0
    for line in iter_bounded_jsonl_bytes(path):
        digest.update(line)
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(entry, dict):
            malformed += 1
            continue
        entries.append(entry)

    header = next((entry for entry in entries if entry.get("type") == "session"), None)
    if header is None or (external_id := _label(header.get("id"), 512)) is None:
        return None, malformed + 1
    return (
        _Session(
            machine=machine,
            external_id=external_id,
            cwd=header.get("cwd") if isinstance(header.get("cwd"), str) else None,
            entries=entries,
            event_count=len(entries),
            digest=digest.hexdigest(),
        ),
        malformed,
    )


def _rank(value: _Session) -> tuple[int, str]:
    return value.event_count, value.digest


def _ancestral_turn(
    entry: dict[str, Any], entry_by_id: dict[str, dict[str, Any]]
) -> str | None:
    for ancestor in _ancestors(entry, entry_by_id):
        message = _mapping(ancestor.get("message"))
        if ancestor.get("type") == "message" and message.get("role") == "user":
            return _label(ancestor.get("id"), 512)
    return None


def _ancestral_model(
    entry: dict[str, Any], entry_by_id: dict[str, dict[str, Any]]
) -> str | None:
    for ancestor in _ancestors(entry, entry_by_id):
        if ancestor.get("type") == "model_change":
            return _model_change(ancestor)
        message = _mapping(ancestor.get("message"))
        if (
            ancestor.get("type") == "message"
            and message.get("role") == "assistant"
            and (model := _model(message))
        ):
            return model
    return None


def _ancestral_effort(
    entry: dict[str, Any], entry_by_id: dict[str, dict[str, Any]]
) -> str | None:
    for ancestor in _ancestors(entry, entry_by_id):
        if ancestor.get("type") == "thinking_level_change":
            value = ancestor.get("thinkingLevel")
            return value if value in THINKING_LEVELS else None
    return None


def _ancestors(
    entry: dict[str, Any], entry_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    parent_id = _label(entry.get("parentId"), 512)
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = entry_by_id.get(parent_id)
        if parent is None:
            break
        result.append(parent)
        parent_id = _label(parent.get("parentId"), 512)
    return result


def _usage(value: object) -> dict[str, int]:
    usage = _mapping(value)
    uncached = _counter(usage.get("input"))
    cached = _counter(usage.get("cacheRead"))
    cache_write = _counter(usage.get("cacheWrite"))
    output = _counter(usage.get("output"))
    reasoning = min(output, _counter(usage.get("reasoning")))
    input_tokens = _sum(uncached, cached, cache_write)
    attributed = _sum(input_tokens, output)
    total = max(attributed, _counter(usage.get("totalTokens")))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "uncached_input_tokens": uncached,
        "visible_output_tokens": output - reasoning,
        "unattributed_tokens": max(0, total - attributed),
    }


def _model(message: dict[str, Any]) -> str | None:
    provider = _label(message.get("provider"), 127)
    identifier = _label(message.get("model"), 127)
    if provider and identifier:
        return f"{provider}/{identifier}"
    return identifier


def _model_change(entry: dict[str, Any]) -> str | None:
    provider = _label(entry.get("provider"), 127)
    identifier = _label(entry.get("modelId"), 127)
    if provider and identifier:
        return f"{provider}/{identifier}"
    return identifier


def _project(cwd: str | None, mappings: list[tuple[str, str]]) -> tuple[str, str]:
    if cwd:
        normalized = cwd.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            prefix = prefix.replace("\\", "/").rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return name, "mapping"
    return "outside-project", "none"


def _finish_turn(turn: dict[str, Any], fallback: datetime | None) -> None:
    turn["ended_at"] = turn["ended_at"] or _iso(fallback)
    start, end = _timestamp(turn["started_at"]), _timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float) and math.isfinite(value):
            return datetime.fromtimestamp(value / 1000, UTC)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        pass
    return None


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _label(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= maximum and SAFE_LABEL.fullmatch(value) else None


def _counter(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return min(MAX_BIGINT, max(0, int(value)))


def _sum(*values: int) -> int:
    return min(MAX_BIGINT, sum(values))


def _add_tokens(target: dict[str, Any], tokens: dict[str, int]) -> None:
    for field, value in tokens.items():
        target[field] = _sum(int(target[field]), value)
