from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    MAX_BIGINT as MAX_BIGINT,
)
from cli_consumption.adapters._shared import (
    SAFE_BASIC_LABEL as SAFE_LABEL,
)
from cli_consumption.adapters._shared import (
    ProviderInputBudget,
    iter_bounded_jsonl_bytes,
)
from cli_consumption.adapters._shared import (
    add_tokens as _add_tokens,
)
from cli_consumption.adapters._shared import (
    bounded_sum as _sum,
)
from cli_consumption.models import Snapshot, empty_tokens


@dataclass(slots=True)
class _Candidate:
    machine: str
    external_id: str
    events: list[dict[str, Any]]
    digest: str


class AiderAdapter:
    """Read Aider analytics logs while discarding arbitrary event properties."""

    name = "aider"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        del project_mappings  # Aider analytics do not record a project path.
        budget = ProviderInputBudget()
        selected: dict[str, _Candidate] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            path = budget.candidate(home / "analytics.jsonl")
            if not path.is_file():
                raise ValueError(f"Missing Aider analytics log: {path}")
            sessions, invalid = _read_sessions(path, budget)
            malformed += invalid
            for external_id, events, digest in sessions:
                candidate = _Candidate(machine, external_id, events, digest)
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
            self._normalize(snapshot, candidate)
        return snapshot

    def _normalize(self, snapshot: Snapshot, source: _Candidate) -> None:
        conversation_id = f"aider:{source.external_id}"
        timestamps = [
            timestamp
            for event in source.events
            if (timestamp := _timestamp(event.get("time"))) is not None
        ]
        turns: list[dict[str, Any]] = []
        turn_models: list[set[str]] = []
        active: int | None = None
        calls: list[tuple[int | None, datetime | None, str, dict[str, int]]] = []

        for event in source.events:
            name = event.get("event")
            timestamp = _timestamp(event.get("time"))
            if name == "message_send_starting":
                if active is not None:
                    _finish_turn(turns[active], timestamp, "aborted")
                active = len(turns)
                turns.append(_new_turn(conversation_id, active + 1, timestamp))
                turn_models.append(set())
                continue
            if name == "message_send_exception":
                if active is not None:
                    _finish_turn(turns[active], timestamp, "aborted")
                    active = None
                continue
            if name == "exit":
                if active is not None:
                    _finish_turn(turns[active], timestamp, "aborted")
                    active = None
                continue
            if name != "message_send":
                continue

            properties = event.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            model = _label(properties.get("main_model"), 255) or "unknown"
            tokens = _usage(properties)
            calls.append((active, timestamp, model, tokens))
            if active is not None:
                _finish_turn(turns[active], timestamp, "completed")
                turn_models[active].add(model)
                active = None

        ended_at = max(timestamps, default=None)
        if active is not None:
            _finish_turn(turns[active], ended_at, "in-progress")

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (turn_index, timestamp, model, tokens) in enumerate(calls, 1):
            models.add(model)
            _add_tokens(totals, tokens)
            turn = turns[turn_index] if turn_index is not None else None
            if turn is not None:
                turn["model_calls"] += 1
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

        for index, turn in enumerate(turns):
            snapshot.turns.append(turn)
            observed_models = turn_models[index]
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:settings:{index + 1}",
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
                "source": "local-analytics-jsonl",
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": len(calls),
                "tool_calls": 0,
                "compactions": 0,
                "event_count": len(source.events),
                "content_hash": source.digest,
                **totals,
            }
        )


def _read_sessions(
    path: Path,
    budget: ProviderInputBudget,
) -> tuple[list[tuple[str, list[dict[str, Any]], str]], int]:
    sessions: list[tuple[str, list[dict[str, Any]], str]] = []
    active: list[dict[str, Any]] | None = None
    malformed = 0
    ordinals: dict[tuple[str, int], int] = {}
    for line in iter_bounded_jsonl_bytes(path, budget):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            malformed += 1
            continue
        if _epoch(event.get("time")) is None:
            malformed += 1
            continue
        if event["event"] == "launched":
            if active:
                sessions.append(_session(active, ordinals))
            active = [event]
            continue
        if active is None:
            continue
        active.append(event)
        if event["event"] == "exit":
            sessions.append(_session(active, ordinals))
            active = None
    if active:
        sessions.append(_session(active, ordinals))
    return sessions, malformed


def _session(
    events: list[dict[str, Any]], ordinals: dict[tuple[str, int], int]
) -> tuple[str, list[dict[str, Any]], str]:
    launched = events[0]
    user_id = launched.get("user_id")
    user_id = user_id if isinstance(user_id, str) else "anonymous"
    started = _epoch(launched.get("time")) or 0
    key = (user_id, started)
    ordinal = ordinals.get(key, 0) + 1
    ordinals[key] = ordinal
    stable = hashlib.sha256(f"{user_id}\0{started}\0{ordinal}".encode()).hexdigest()[
        :32
    ]
    approved = [
        {
            "event": event.get("event"),
            "time": _epoch(event.get("time")),
            "properties": _approved_properties(event),
        }
        for event in events
    ]
    digest = hashlib.sha256(
        json.dumps(approved, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return stable, events, digest


def _approved_properties(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("event") != "message_send":
        return {}
    properties = event.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {
        key: properties.get(key)
        for key in ("main_model", "prompt_tokens", "completion_tokens", "total_tokens")
    }


def _new_turn(
    conversation_id: str, sequence: int, timestamp: datetime | None
) -> dict[str, Any]:
    return {
        "id": f"{conversation_id}:turn:{sequence}",
        "conversation_id": conversation_id,
        "external_id": f"turn-{sequence}",
        "started_at": _iso(timestamp),
        "ended_at": None,
        "status": "in-progress",
        "duration_ms": None,
        "time_to_first_token_ms": None,
        "model_calls": 0,
        "tool_calls": 0,
        **empty_tokens(),
    }


def _finish_turn(turn: dict[str, Any], timestamp: datetime | None, status: str) -> None:
    turn["ended_at"] = _iso(timestamp)
    turn["status"] = status


def _usage(properties: dict[str, Any]) -> dict[str, int]:
    input_tokens = _counter(properties.get("prompt_tokens"))
    output_tokens = _counter(properties.get("completion_tokens"))
    attributed = _sum(input_tokens, output_tokens)
    total = max(attributed, _counter(properties.get("total_tokens")))
    return {
        "input_tokens": input_tokens,
        "uncached_input_tokens": input_tokens,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "visible_output_tokens": output_tokens,
        "total_tokens": total,
        "unattributed_tokens": total - attributed,
    }


def _counter(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value < 0 or value > MAX_BIGINT:
        return 0
    return int(value)


def _epoch(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > 253_402_300_799:
        return None
    return int(value)


def _timestamp(value: object) -> datetime | None:
    epoch = _epoch(value)
    return datetime.fromtimestamp(epoch, tz=UTC) if epoch is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _label(value: object, limit: int) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or not SAFE_LABEL.fullmatch(value)
    ):
        return None
    return value


def _rank(candidate: _Candidate) -> tuple[int, int, str, str]:
    ended = max(
        (_epoch(event.get("time")) or 0 for event in candidate.events), default=0
    )
    return len(candidate.events), ended, candidate.digest, candidate.machine
