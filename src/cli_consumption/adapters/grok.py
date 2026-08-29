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
    read_json,
)
from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")
COMPLETED_REASONS = {"end_turn", "max_tokens", "stop_sequence"}
ABORTED_REASONS = {"cancelled", "error"}


@dataclass(slots=True)
class _Candidate:
    machine: str
    directory: Path
    summary: dict[str, Any]
    external_id: str
    record_count: int
    digest: str


class GrokAdapter:
    """Read Grok Build session metadata while discarding conversation content."""

    name = "grok"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Candidate] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            sessions = home / "sessions"
            if not sessions.is_dir():
                raise ValueError(f"Missing Grok Build sessions directory: {sessions}")
            for path in budget.sorted_paths(sessions.glob("*/*/summary.json")):
                summary = _read_object(path)
                if summary is None:
                    malformed += 1
                    continue
                info = summary.get("info")
                external_id = (
                    _label(info.get("id"), 512) if isinstance(info, dict) else None
                )
                if external_id is None:
                    malformed += 1
                    continue
                updates, invalid_updates, update_digest = _read_jsonl(
                    path.parent / "updates.jsonl"
                )
                events, invalid_events, event_digest = _read_jsonl(
                    path.parent / "events.jsonl"
                )
                malformed += invalid_updates + invalid_events
                digest = hashlib.sha256()
                digest.update(_canonical_json(summary))
                digest.update(update_digest.encode())
                digest.update(event_digest.encode())
                candidate = _Candidate(
                    machine=machine,
                    directory=path.parent,
                    summary=summary,
                    external_id=external_id,
                    record_count=len(updates) + len(events),
                    digest=digest.hexdigest(),
                )
                previous = selected.get(external_id)
                if previous is None:
                    selected[external_id] = candidate
                    continue
                duplicates += 1
                if _rank(candidate) > _rank(previous):
                    selected[external_id] = candidate

        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=malformed,
        )
        for candidate in sorted(selected.values(), key=lambda item: item.external_id):
            updates, _, _ = _read_jsonl(candidate.directory / "updates.jsonl")
            events, _, _ = _read_jsonl(candidate.directory / "events.jsonl")
            self._normalize(
                snapshot, candidate, updates, events, project_mappings or []
            )
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Candidate,
        updates: list[dict[str, Any]],
        events: list[dict[str, Any]],
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"grok:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        turn_order: list[str] = []
        terminals: dict[str, tuple[datetime | None, dict[str, Any]]] = {}
        compactions: list[tuple[str | None, datetime | None]] = []
        timestamps: list[datetime] = []
        active: str | None = None
        active_user_key: str | None = None

        for envelope in updates:
            timestamp = _timestamp(envelope.get("timestamp"))
            params = envelope.get("params")
            if not isinstance(params, dict):
                snapshot.malformed_records += 1
                continue
            update = params.get("update")
            if not isinstance(update, dict):
                snapshot.malformed_records += 1
                continue
            update_type = update.get("sessionUpdate")
            meta = params.get("_meta")
            if not isinstance(update_type, str):
                snapshot.malformed_records += 1
                continue
            if isinstance(meta, dict):
                timestamp = _timestamp_ms(meta.get("turnStartMs")) or timestamp
            if timestamp is not None:
                timestamps.append(timestamp)

            if envelope.get("method") == "session/update":
                if update_type != "user_message_chunk":
                    active_user_key = None
                    continue
                content = update.get("content")
                content_meta = (
                    content.get("_meta") if isinstance(content, dict) else None
                )
                if (
                    isinstance(content_meta, dict)
                    and content_meta.get("hostTurn") is True
                ):
                    continue
                prompt_id = (
                    _label(meta.get("promptId"), 512)
                    if isinstance(meta, dict)
                    else None
                )
                raw_prompt_index = (
                    content_meta.get("promptIndex")
                    if isinstance(content_meta, dict)
                    else None
                )
                prompt_index = (
                    _counter(raw_prompt_index)
                    if isinstance(raw_prompt_index, (int, float))
                    and not isinstance(raw_prompt_index, bool)
                    else None
                )
                user_key = prompt_id or (
                    f"prompt-{prompt_index}" if prompt_index is not None else None
                )
                if user_key is None:
                    user_key = active_user_key or f"turn-{len(turn_order) + 1}"
                if user_key in turns:
                    active = user_key
                    active_user_key = user_key
                    continue
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                active = user_key
                active_user_key = user_key
                turn_order.append(user_key)
                turns[user_key] = _turn(conversation_id, user_key, timestamp)
                continue

            if envelope.get("method") != "_x.ai/session/update":
                continue
            prompt_id = _label(update.get("prompt_id"), 512)
            if update_type == "turn_completed" and prompt_id is not None:
                terminals[prompt_id] = (timestamp, update)
                continue
            if update_type == "auto_compact_completed":
                compactions.append((active, timestamp))

        if not turns:
            self._turns_from_events(turns, turn_order, events, conversation_id)
        self._apply_terminals(turns, turn_order, terminals)
        self._apply_event_metadata(snapshot, conversation_id, turns, turn_order, events)

        summary_started = _timestamp(source.summary.get("created_at"))
        summary_ended = _timestamp(source.summary.get("last_active_at")) or _timestamp(
            source.summary.get("updated_at")
        )
        if summary_started is not None:
            timestamps.append(summary_started)
        if summary_ended is not None:
            timestamps.append(summary_ended)
        ended_at = max(timestamps, default=None)
        if active is not None:
            _finish_turn(turns[active], ended_at)

        current_model = _label(source.summary.get("current_model_id"), 255)
        reasoning_effort = _bounded_label(source.summary.get("reasoning_effort"), 64)
        totals = empty_tokens()
        models: set[str] = set()
        model_sequence = 0
        for key in turn_order:
            turn = turns[key]
            terminal = terminals.get(key)
            usage = terminal[1].get("usage") if terminal else None
            model_usage = usage.get("modelUsage") if isinstance(usage, dict) else None
            calls: list[tuple[str, dict[str, int], int]] = []
            if isinstance(model_usage, dict):
                for model_value, raw_usage in sorted(model_usage.items()):
                    model = _label(model_value, 255)
                    tokens = _usage(raw_usage)
                    if model is None or tokens is None:
                        snapshot.malformed_records += 1
                        continue
                    calls.append((model, tokens, _counter(raw_usage.get("modelCalls"))))
            elif isinstance(usage, dict):
                tokens = _usage(usage)
                if tokens is not None:
                    calls.append((current_model or "unknown", tokens, 1))

            observed_models: set[str] = set()
            for model, tokens, reported_calls in calls:
                model_sequence += 1
                observed_models.add(model)
                models.add(model)
                _add_tokens(totals, tokens)
                _add_tokens(turn, tokens)
                turn["model_calls"] += reported_calls or 1
                snapshot.model_calls.append(
                    {
                        "id": f"{conversation_id}:model:{model_sequence}",
                        "conversation_id": conversation_id,
                        "turn_id": turn["id"],
                        "sequence": model_sequence,
                        "timestamp": terminal and _iso(terminal[0]),
                        "model": model,
                        **tokens,
                    }
                )
            snapshot.turns.append(turn)
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:settings:{key}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"],
                    "model": (
                        next(iter(observed_models))
                        if len(observed_models) == 1
                        else current_model
                    ),
                    "effort": reasoning_effort,
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": None,
                }
            )

        if current_model is not None:
            models.add(current_model)
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

        started_at = summary_started or min(timestamps, default=None)
        project, project_source = _project(source.summary, mappings)
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
                "model_calls": len(
                    [
                        call
                        for call in snapshot.model_calls
                        if call["conversation_id"] == conversation_id
                    ]
                ),
                "tool_calls": len(
                    [
                        call
                        for call in snapshot.tool_calls
                        if call["conversation_id"] == conversation_id
                    ]
                ),
                "compactions": len(compactions),
                "event_count": source.record_count,
                "content_hash": source.digest,
                **totals,
            }
        )

    def _turns_from_events(
        self,
        turns: dict[str, dict[str, Any]],
        order: list[str],
        events: list[dict[str, Any]],
        conversation_id: str,
    ) -> None:
        for event in events:
            if event.get("type") != "turn_started":
                continue
            number = _counter(event.get("turn_number")) or len(order) + 1
            key = f"turn-{number}"
            if key in turns:
                continue
            order.append(key)
            turns[key] = _turn(conversation_id, key, _timestamp(event.get("ts")))

    def _apply_terminals(
        self,
        turns: dict[str, dict[str, Any]],
        order: list[str],
        terminals: dict[str, tuple[datetime | None, dict[str, Any]]],
    ) -> None:
        unmatched = iter(
            terminal for key, terminal in terminals.items() if key not in turns
        )
        for key in order:
            terminal = terminals.get(key)
            if terminal is None:
                terminal = next(unmatched, None)
            if terminal is None:
                continue
            timestamp, update = terminal
            reason = update.get("stop_reason")
            turns[key]["ended_at"] = _iso(timestamp) or turns[key]["ended_at"]
            if reason in COMPLETED_REASONS:
                turns[key]["status"] = "completed"
            elif reason in ABORTED_REASONS:
                turns[key]["status"] = "aborted"

    def _apply_event_metadata(
        self,
        snapshot: Snapshot,
        conversation_id: str,
        turns: dict[str, dict[str, Any]],
        order: list[str],
        events: list[dict[str, Any]],
    ) -> None:
        active_index = -1
        active: dict[str, Any] | None = None
        tool_sequence = 0
        for event in events:
            timestamp = _timestamp(event.get("ts"))
            event_type = event.get("type")
            if event_type == "turn_started":
                active_index += 1
                active = (
                    turns.get(order[active_index])
                    if active_index < len(order)
                    else None
                )
                if active is not None and active["started_at"] is None:
                    active["started_at"] = _iso(timestamp)
                continue
            if event_type == "first_token" and active is not None:
                started = _timestamp(active["started_at"])
                if (
                    started is not None
                    and timestamp is not None
                    and timestamp >= started
                ):
                    active["time_to_first_token_ms"] = int(
                        (timestamp - started).total_seconds() * 1000
                    )
                continue
            if event_type == "tool_started":
                name = _label(event.get("tool_name"), 512)
                if name is None:
                    snapshot.malformed_records += 1
                    continue
                tool_sequence += 1
                if active is not None:
                    active["tool_calls"] += 1
                snapshot.tool_calls.append(
                    {
                        "id": f"{conversation_id}:tool:{tool_sequence}",
                        "conversation_id": conversation_id,
                        "turn_id": active["id"] if active else None,
                        "sequence": tool_sequence,
                        "timestamp": _iso(timestamp),
                        "tool_name": name,
                        "outer_tool_name": name,
                    }
                )
                continue
            if event_type == "turn_ended" and active is not None:
                outcome = event.get("outcome")
                if active["status"] == "in-progress":
                    active["status"] = (
                        "completed" if outcome == "completed" else "aborted"
                    )
                active["ended_at"] = _iso(timestamp) or active["ended_at"]
                _finish_turn(active, timestamp)


def _turn(
    conversation_id: str, external_id: str, timestamp: datetime | None
) -> dict[str, Any]:
    return {
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


def _finish_turn(turn: dict[str, Any], timestamp: datetime | None) -> None:
    if turn["ended_at"] is None:
        turn["ended_at"] = _iso(timestamp)
    started = _timestamp(turn["started_at"])
    ended = _timestamp(turn["ended_at"])
    if started is not None and ended is not None and ended >= started:
        turn["duration_ms"] = int((ended - started).total_seconds() * 1000)


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int, str]:
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    malformed = 0
    try:
        for line in iter_bounded_jsonl_bytes(path):
            digest.update(line)
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                malformed += 1
    except OSError:
        return records, malformed, digest.hexdigest()
    return records, malformed, digest.hexdigest()


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    full_input = _counter(value.get("inputTokens"))
    cached = min(full_input, _counter(value.get("cachedReadTokens")))
    cache_creation = min(
        full_input - cached, _counter(value.get("cacheCreationTokens"))
    )
    output = _counter(value.get("outputTokens"))
    reasoning = min(output, _counter(value.get("reasoningTokens")))
    total = min(MAX_BIGINT, full_input + output)
    reported_total = _counter(value.get("totalTokens"))
    return {
        "input_tokens": full_input,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_creation,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": max(total, reported_total),
        "uncached_input_tokens": full_input - cached - cache_creation,
        "visible_output_tokens": output - reasoning,
        "unattributed_tokens": max(0, reported_total - total),
    }


def _add_tokens(target: dict[str, Any], tokens: dict[str, int]) -> None:
    for key in empty_tokens():
        target[key] = min(MAX_BIGINT, int(target[key]) + tokens[key])


def _counter(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return min(MAX_BIGINT, max(0, value))
    if isinstance(value, float) and math.isfinite(value):
        return min(MAX_BIGINT, max(0, int(value)))
    return 0


def _label(value: Any, limit: int) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or not SAFE_LABEL.fullmatch(value)
    ):
        return None
    return value


def _bounded_label(value: Any, limit: int) -> str | None:
    return _label(value, limit)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _timestamp_ms(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _project(
    summary: dict[str, Any], mappings: list[tuple[str, str]]
) -> tuple[str, str]:
    info = summary.get("info")
    cwd = info.get("cwd") if isinstance(info, dict) else None
    if not isinstance(cwd, str):
        return "outside-project", "none"
    matched = [(name, prefix) for name, prefix in mappings if _inside(cwd, prefix)]
    if not matched:
        return "outside-project", "none"
    return max(matched, key=lambda item: len(item[1]))[0], "mapping"


def _inside(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/\\") + "/")


def _rank(candidate: _Candidate) -> tuple[int, str, str]:
    updated = candidate.summary.get("updated_at")
    return (
        candidate.record_count,
        updated if isinstance(updated, str) else "",
        candidate.digest,
    )
