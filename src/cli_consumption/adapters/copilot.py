from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")


@dataclass(slots=True)
class _Candidate:
    machine: str
    path: Path
    external_id: str
    event_count: int
    digest: str


class CopilotAdapter:
    """Read GitHub Copilot CLI session metadata without retaining content."""

    name = "copilot"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected: dict[str, _Candidate] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            sessions = home / "session-state"
            if not sessions.is_dir():
                raise ValueError(
                    f"Missing GitHub Copilot CLI session-state directory: {sessions}"
                )
            for path in sorted(sessions.glob("*/events.jsonl")):
                events, invalid, digest = _read_events(path)
                malformed += invalid
                session_id = _session_id(events)
                if session_id is None:
                    malformed += 1
                    continue
                candidate = _Candidate(
                    machine=machine,
                    path=path,
                    external_id=session_id,
                    event_count=len(events),
                    digest=digest,
                )
                previous = selected.get(session_id)
                if previous is None:
                    selected[session_id] = candidate
                else:
                    duplicates += 1
                    if _rank(candidate) > _rank(previous):
                        selected[session_id] = candidate

        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=malformed,
        )
        for candidate in sorted(selected.values(), key=lambda item: item.external_id):
            events, _, _ = _read_events(candidate.path)
            self._normalize(snapshot, candidate, events, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Candidate,
        events: list[dict[str, Any]],
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"copilot:{source.external_id}"
        timestamps: list[datetime] = []
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        tools: dict[str, tuple[str | None, datetime | None, str]] = {}
        compactions: list[tuple[str | None, datetime | None]] = []
        active: str | None = None
        latest_shutdown: tuple[int, datetime | None, dict[str, Any]] | None = None
        selected_model: str | None = None
        reasoning_effort: str | None = None

        for index, event in enumerate(events, 1):
            timestamp = _timestamp(event.get("timestamp"))
            if timestamp is not None:
                timestamps.append(timestamp)
            event_type = event.get("type")
            data = event.get("data")
            if not isinstance(event_type, str) or not isinstance(data, dict):
                snapshot.malformed_records += 1
                continue

            if event_type == "session.start" and _root_event(event):
                selected_model = (
                    _label(data.get("selectedModel"), 255) or selected_model
                )
                reasoning_effort = (
                    _bounded_label(data.get("reasoningEffort"), 64) or reasoning_effort
                )

            if event_type == "user.message" and _root_event(event):
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = _label(event.get("id"), 512) or f"turn-{len(turns) + 1}"
                if external_id in turns:
                    external_id = f"turn-{len(turns) + 1}"
                active = external_id
                turns[active] = {
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
                turn_models[active] = set()
                continue

            if event_type == "assistant.message" and _root_event(event):
                model = _label(data.get("model"), 255)
                if active is not None:
                    if model is not None:
                        turn_models[active].add(model)
                    turns[active]["ended_at"] = (
                        _iso(timestamp) or turns[active]["ended_at"]
                    )
                continue

            if event_type == "assistant.turn_end" and _root_event(event):
                if active is not None:
                    turns[active]["status"] = "completed"
                    turns[active]["ended_at"] = (
                        _iso(timestamp) or turns[active]["ended_at"]
                    )
                continue

            if event_type == "tool.execution_start" and _root_event(event):
                name = _label(data.get("toolName"), 512)
                if name is None:
                    snapshot.malformed_records += 1
                    continue
                key = _label(data.get("toolCallId"), 512) or f"event-{index}"
                tools.setdefault(key, (active, timestamp, name))
                continue

            if (
                event_type == "session.compaction_complete"
                and _root_event(event)
                and data.get("success") is True
            ):
                compactions.append((active, timestamp))
                continue

            if event_type == "session.shutdown" and _root_event(event):
                if not isinstance(data.get("modelMetrics"), dict):
                    snapshot.malformed_records += 1
                    continue
                candidate = (index, timestamp, data)
                if latest_shutdown is None or candidate[0] > latest_shutdown[0]:
                    latest_shutdown = candidate

        ended_at = max(timestamps, default=None)
        if active is not None:
            _finish_turn(turns[active], ended_at)
            if (
                latest_shutdown is not None
                and latest_shutdown[2].get("shutdownType") == "error"
                and turns[active]["status"] != "completed"
            ):
                turns[active]["status"] = "aborted"

        totals = empty_tokens()
        models: set[str] = set()
        if latest_shutdown is not None:
            _, shutdown_at, shutdown = latest_shutdown
            model_metrics = shutdown["modelMetrics"]
            model_sequence = 0
            for model_value, metric in sorted(model_metrics.items()):
                model = _label(model_value, 255)
                tokens = _usage(metric)
                if model is None or tokens is None:
                    snapshot.malformed_records += 1
                    continue
                models.add(model)
                _add_tokens(totals, tokens)
                model_sequence += 1
                snapshot.model_calls.append(
                    {
                        "id": f"{conversation_id}:model:{model}",
                        "conversation_id": conversation_id,
                        "turn_id": None,
                        "sequence": model_sequence,
                        "timestamp": _iso(shutdown_at),
                        "model": model,
                        **tokens,
                    }
                )

        for observed in turn_models.values():
            models.update(observed)
        if selected_model is not None:
            models.add(selected_model)

        for sequence, (turn_key, timestamp, name) in enumerate(tools.values(), 1):
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
            observed_models = turn_models[key]
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:settings:{key}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"],
                    "model": (
                        next(iter(observed_models))
                        if len(observed_models) == 1
                        else None
                    ),
                    "effort": reasoning_effort,
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": None,
                }
            )

        started_at = min(timestamps, default=None)
        project, project_source = _project(events, mappings)
        model_call_count = len(
            [
                call
                for call in snapshot.model_calls
                if call["conversation_id"] == conversation_id
            ]
        )
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
                "model_calls": model_call_count,
                "tool_calls": len(tools),
                "compactions": len(compactions),
                "event_count": source.event_count,
                "content_hash": source.digest,
                **totals,
            }
        )


def _read_events(path: Path) -> tuple[list[dict[str, Any]], int, str]:
    digest = hashlib.sha256()
    events: list[dict[str, Any]] = []
    malformed = 0
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                malformed += 1
    return events, malformed, digest.hexdigest()


def _session_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") != "session.start" or not isinstance(
            (data := event.get("data")), dict
        ):
            continue
        if session_id := _label(data.get("sessionId"), 512):
            return session_id
    return None


def _usage(metric: Any) -> dict[str, int] | None:
    if not isinstance(metric, dict) or not isinstance(
        (usage := metric.get("usage")), dict
    ):
        return None
    raw_input = _counter(usage.get("inputTokens"))
    cached = min(raw_input, _counter(usage.get("cacheReadTokens")))
    cache_write = min(raw_input - cached, _counter(usage.get("cacheWriteTokens")))
    output = _counter(usage.get("outputTokens"))
    reasoning = min(output, _counter(usage.get("reasoningTokens")))
    return {
        "input_tokens": raw_input,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": min(MAX_BIGINT, raw_input + output),
        "uncached_input_tokens": raw_input - cached - cache_write,
        "visible_output_tokens": output - reasoning,
        "unattributed_tokens": 0,
    }


def _project(
    events: list[dict[str, Any]], mappings: list[tuple[str, str]]
) -> tuple[str, str]:
    for event in events:
        if event.get("type") == "session.start" and isinstance(
            (data := event.get("data")), dict
        ):
            context = data.get("context")
            cwd = context.get("cwd") if isinstance(context, dict) else None
        elif event.get("type") == "session.context_changed" and isinstance(
            (data := event.get("data")), dict
        ):
            cwd = data.get("cwd")
        else:
            continue
        if not isinstance(cwd, str):
            continue
        normalized = cwd.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            normalized_prefix = prefix.replace("\\", "/").rstrip("/")
            if normalized == normalized_prefix or normalized.startswith(
                normalized_prefix + "/"
            ):
                return name, "mapping"
    return "outside-project", "none"


def _root_event(event: dict[str, Any]) -> bool:
    return event.get("agentId") is None


def _finish_turn(turn: dict[str, Any], fallback: datetime | None) -> None:
    turn["ended_at"] = turn["ended_at"] or _iso(fallback)
    start, end = _timestamp(turn["started_at"]), _timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))


def _rank(candidate: _Candidate) -> tuple[int, str, str]:
    return candidate.event_count, candidate.digest, str(candidate.path)


def _label(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not (value := value.strip()) or len(value) > limit:
        return None
    return value if SAFE_LABEL.fullmatch(value) else None


def _bounded_label(value: Any, limit: int) -> str | None:
    return _label(value, limit)


def _counter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value < 0:
        return 0
    return min(int(value), MAX_BIGINT)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _add_tokens(target: dict[str, Any], tokens: dict[str, int]) -> None:
    for key, value in tokens.items():
        target[key] = min(MAX_BIGINT, int(target[key]) + value)
