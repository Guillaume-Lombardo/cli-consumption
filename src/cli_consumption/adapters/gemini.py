from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderInputBudget,
    iter_bounded_jsonl_bytes,
    read_bounded_bytes,
)
from cli_consumption.adapters._shared import (
    add_tokens as _add_tokens,
)
from cli_consumption.adapters._shared import (
    basic_label as _label,
)
from cli_consumption.adapters._shared import (
    bounded_sum as _sum,
)
from cli_consumption.adapters._shared import (
    counter as _counter,
)
from cli_consumption.models import Snapshot, empty_tokens


@dataclass(slots=True)
class _Candidate:
    machine: str
    path: Path
    external_id: str
    event_count: int
    digest: str


class GeminiAdapter:
    """Read Gemini CLI chat metadata while discarding conversation content."""

    name = "gemini"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        del project_mappings  # Gemini stores only a one-way project hash.
        selected: dict[str, _Candidate] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            temporary = home / "tmp"
            if not temporary.is_dir():
                raise ValueError(f"Missing Gemini CLI temporary directory: {temporary}")
            paths = budget.sorted_paths(temporary.glob("*/chats/session-*.json"))
            paths.extend(budget.sorted_paths(temporary.glob("*/chats/session-*.jsonl")))
            for path in paths:
                metadata, _, invalid, event_count, digest = _read_session(path, budget)
                malformed += invalid
                external_id = _label(metadata.get("sessionId"), 512)
                if external_id is None:
                    malformed += 1
                    continue
                candidate = _Candidate(
                    machine=machine,
                    path=path,
                    external_id=external_id,
                    event_count=event_count,
                    digest=digest,
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
            metadata, messages, _, _, _ = _read_session(candidate.path, budget)
            self._normalize(snapshot, candidate, metadata, messages)
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Candidate,
        metadata: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> None:
        conversation_id = f"gemini:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        calls: list[tuple[str | None, datetime | None, str, dict[str, int]]] = []
        tools: list[tuple[str | None, datetime | None, str]] = []
        timestamps = [
            timestamp
            for value in (metadata.get("startTime"), metadata.get("lastUpdated"))
            if (timestamp := _timestamp(value)) is not None
        ]

        for message in messages:
            timestamp = _timestamp(message.get("timestamp"))
            if timestamp is not None:
                timestamps.append(timestamp)
            message_type = message.get("type")
            if message_type == "user":
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = _label(message.get("id"), 512) or (
                    f"turn-{len(turns) + 1}"
                )
                if external_id in turns:
                    external_id = f"turn-{len(turns) + 1}"
                active = external_id
                turns[active] = {
                    "id": f"{conversation_id}:{active}",
                    "conversation_id": conversation_id,
                    "external_id": active,
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

            if message_type in {"error", "warning"} and active is not None:
                turns[active]["status"] = "aborted"
                turns[active]["ended_at"] = _iso(timestamp)
                continue
            if message_type != "gemini":
                continue

            model = _label(message.get("model"), 255) or "unknown"
            tokens = _usage(message.get("tokens"))
            calls.append((active, timestamp, model, tokens))
            if active is not None:
                turns[active]["status"] = "completed"
                turns[active]["ended_at"] = _iso(timestamp)

            tool_calls = message.get("toolCalls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                name = _label(tool_call.get("name"), 512)
                if name:
                    tools.append(
                        (
                            active,
                            _timestamp(tool_call.get("timestamp")) or timestamp,
                            name,
                        )
                    )

        ended_at = max(timestamps, default=None)
        if active is not None:
            _finish_turn(turns[active], ended_at)

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

        for key, turn in turns.items():
            snapshot.turns.append(turn)
            observed_models = turn_models[key]
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:settings:{key}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"],
                    "model": next(iter(observed_models))
                    if len(observed_models) == 1
                    else None,
                    "effort": None,
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": None,
                }
            )

        started_at = min(timestamps, default=None)
        snapshot.conversations.append(
            {
                "id": conversation_id,
                "provider": self.name,
                "external_id": source.external_id,
                "source_machine": source.machine,
                "project": "outside-project",
                "project_source": "none",
                "started_at": _iso(started_at),
                "ended_at": _iso(ended_at),
                "duration_seconds": (
                    (ended_at - started_at).total_seconds()
                    if started_at and ended_at
                    else None
                ),
                "source": "local-jsonl"
                if source.path.suffix == ".jsonl"
                else "local-json",
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": len(calls),
                "tool_calls": len(tools),
                "compactions": 0,
                "event_count": source.event_count,
                "content_hash": source.digest,
                **totals,
            }
        )


def _read_session(
    path: Path, budget: ProviderInputBudget
) -> tuple[dict[str, Any], list[dict[str, Any]], int, int, str]:
    digest_builder = hashlib.sha256()
    records: list[dict[str, Any]] = []
    malformed = 0
    if path.suffix == ".jsonl":
        for line in iter_bounded_jsonl_bytes(path, budget):
            digest_builder.update(line)
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
    else:
        payload = read_bounded_bytes(path, budget)
        digest_builder.update(payload)
        try:
            record = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}, [], 1, 0, digest_builder.hexdigest()
        if not isinstance(record, dict):
            return {}, [], 1, 0, digest_builder.hexdigest()
        records.append(record)

    metadata: dict[str, Any] = {}
    messages: dict[str, dict[str, Any]] = {}
    for record in records:
        rewind_to = _label(record.get("$rewindTo"), 512)
        if rewind_to is not None:
            identifiers = list(messages)
            try:
                start = identifiers.index(rewind_to)
            except ValueError:
                messages.clear()
            else:
                for identifier in identifiers[start:]:
                    del messages[identifier]
            continue

        updates = record.get("$set")
        if isinstance(updates, dict):
            checkpoint = updates.get("messages")
            if isinstance(checkpoint, list):
                messages.clear()
                for message in checkpoint:
                    if isinstance(message, dict) and (
                        identifier := _label(message.get("id"), 512)
                    ):
                        messages[identifier] = message
            metadata.update(
                {key: value for key, value in updates.items() if key != "messages"}
            )
            continue

        identifier = _label(record.get("id"), 512)
        if identifier is not None:
            messages[identifier] = record
            continue

        if isinstance(record.get("sessionId"), str):
            metadata.update(
                {key: value for key, value in record.items() if key != "messages"}
            )
            legacy_messages = record.get("messages")
            if isinstance(legacy_messages, list):
                for message in legacy_messages:
                    if isinstance(message, dict) and (
                        identifier := _label(message.get("id"), 512)
                    ):
                        messages[identifier] = message
    return (
        metadata,
        list(messages.values()),
        malformed,
        len(records),
        digest_builder.hexdigest(),
    )


def _usage(value: object) -> dict[str, int]:
    usage = value if isinstance(value, dict) else {}
    input_tokens = _counter(usage.get("input"))
    cached = min(input_tokens, _counter(usage.get("cached")))
    visible = _counter(usage.get("output"))
    reasoning = _counter(usage.get("thoughts"))
    output_tokens = _sum(visible, reasoning)
    attributed = _sum(input_tokens, output_tokens)
    total = max(attributed, _counter(usage.get("total")))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "uncached_input_tokens": input_tokens - cached,
        "visible_output_tokens": visible,
        "unattributed_tokens": max(0, total - attributed),
    }


def _rank(value: _Candidate) -> tuple[int, str]:
    return value.event_count, value.digest


def _finish_turn(turn: dict[str, Any], fallback: datetime | None) -> None:
    turn["ended_at"] = turn["ended_at"] or _iso(fallback)
    start, end = _timestamp(turn["started_at"]), _timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
