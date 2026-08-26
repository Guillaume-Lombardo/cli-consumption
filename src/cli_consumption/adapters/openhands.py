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
EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}


@dataclass(slots=True)
class _Conversation:
    machine: str
    external_id: str
    root: dict[str, Any]
    events: list[dict[str, Any]]
    event_count: int
    usage_count: int
    digest: str


class OpenHandsAdapter:
    """Read OpenHands CLI SDK persistence while discarding conversation content."""

    name = "openhands"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected: dict[str, _Conversation] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            conversations = home / "conversations"
            if not conversations.is_dir():
                raise ValueError(
                    f"Missing OpenHands conversations directory: {conversations}"
                )
            for path in sorted(
                value for value in conversations.iterdir() if value.is_dir()
            ):
                candidate, invalid = _read_conversation(path, machine)
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
        for conversation in sorted(
            selected.values(), key=lambda value: value.external_id
        ):
            self._normalize(snapshot, conversation, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Conversation,
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"openhands:{source.external_id}"
        turns: list[dict[str, Any]] = []
        turn_models: list[set[str]] = []
        turn_contexts: list[int | None] = []
        response_metadata: dict[str, tuple[int | None, datetime | None, int]] = {}
        response_ids: list[str] = []
        tools: list[tuple[int | None, datetime | None, str]] = []
        compactions: list[tuple[int | None, datetime | None]] = []
        timestamps: list[datetime] = []
        active: int | None = None
        saw_agent_response: set[int] = set()
        errored_turns: set[int] = set()

        for event_index, event in enumerate(source.events):
            timestamp = _timestamp(event.get("timestamp"))
            if timestamp is not None:
                timestamps.append(timestamp)
            kind = event.get("kind")
            message = _mapping(event.get("llm_message"))
            if kind == "MessageEvent" and (
                event.get("source") == "user" or message.get("role") == "user"
            ):
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = _label(event.get("id"), 512) or f"turn-{len(turns) + 1}"
                if any(turn["external_id"] == external_id for turn in turns):
                    external_id = f"turn-{len(turns) + 1}"
                active = len(turns)
                turns.append(
                    {
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
                )
                turn_models.append(set())
                turn_contexts.append(None)
                continue

            response_id = _label(event.get("llm_response_id"), 512)
            is_response = kind in {"ActionEvent", "Condensation"} or (
                kind == "MessageEvent" and event.get("source") == "agent"
            )
            if is_response and response_id is not None:
                if response_id not in response_metadata:
                    response_ids.append(response_id)
                    response_metadata[response_id] = (active, timestamp, event_index)
                if active is not None:
                    saw_agent_response.add(active)
                    turns[active]["ended_at"] = (
                        _iso(timestamp) or turns[active]["ended_at"]
                    )

            if kind == "ActionEvent":
                name = _label(event.get("tool_name"), 512)
                if name is not None:
                    tools.append((active, timestamp, name))
            elif kind == "Condensation":
                compactions.append((active, timestamp))
            elif (
                kind in {"AgentErrorEvent", "ConversationErrorEvent"}
                and active is not None
            ):
                errored_turns.add(active)

        raw_calls = _model_calls(source.root, response_metadata)
        seen_responses = {response_id for response_id, _, _, _, _ in raw_calls}
        fallback_model = _agent_model(source.root) or "unknown"
        for response_id in response_ids:
            if response_id not in seen_responses:
                turn_index, timestamp, event_index = response_metadata[response_id]
                raw_calls.append(
                    (response_id, fallback_model, empty_tokens(), 0, event_index)
                )
        raw_calls.sort(key=lambda call: (call[4], call[0] or "", call[1]))

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (response_id, model, tokens, context_window, _) in enumerate(
            raw_calls, 1
        ):
            turn_index, timestamp, _ = response_metadata.get(
                response_id or "", (None, None, 0)
            )
            turn = turns[turn_index] if turn_index is not None else None
            models.add(model)
            _add_tokens(totals, tokens)
            if turn_index is not None:
                turn = turns[turn_index]
                turn["model_calls"] += 1
                turn_models[turn_index].add(model)
                _add_tokens(turn, tokens)
                if context_window > 0:
                    turn_contexts[turn_index] = context_window
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
            if context_window > 0:
                snapshot.context_samples.append(
                    {
                        "id": f"{conversation_id}:context:{sequence}",
                        "conversation_id": conversation_id,
                        "turn_id": turn["id"] if turn else None,
                        "sequence": sequence,
                        "timestamp": _iso(timestamp),
                        "input_tokens": tokens["input_tokens"],
                        "context_window_tokens": context_window,
                    }
                )

        for sequence, (turn_index, timestamp, name) in enumerate(tools, 1):
            turn = turns[turn_index] if turn_index is not None else None
            if turn is not None:
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

        for sequence, (turn_index, timestamp) in enumerate(compactions, 1):
            turn = turns[turn_index] if turn_index is not None else None
            snapshot.compaction_events.append(
                {
                    "id": f"{conversation_id}:compaction:{sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": _iso(timestamp),
                }
            )

        ended_at = max(timestamps, default=None)
        terminal_status = source.root.get("execution_status")
        for index, turn in enumerate(turns):
            _finish_turn(turn, ended_at)
            if index in saw_agent_response:
                turn["status"] = "completed"
            if index in errored_turns or (
                index == len(turns) - 1 and terminal_status in {"error", "stuck"}
            ):
                turn["status"] = "aborted"
            snapshot.turns.append(turn)
            observed = turn_models[index]
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:settings:{index + 1}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"],
                    "model": next(iter(observed)) if len(observed) == 1 else None,
                    "effort": _effort(source.root),
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": turn_contexts[index],
                }
            )

        started_at = min(timestamps, default=None)
        project, project_source = _project(source.root, mappings)
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
                    if started_at is not None and ended_at is not None
                    else None
                ),
                "source": "local-sdk-json-v1",
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": len(raw_calls),
                "tool_calls": len(tools),
                "compactions": len(compactions),
                "event_count": source.event_count,
                "content_hash": source.digest,
                **totals,
            }
        )


def _read_conversation(path: Path, machine: str) -> tuple[_Conversation | None, int]:
    base_path = path / "base_state.json"
    if not base_path.is_file():
        return None, 0
    digest = hashlib.sha256()
    try:
        raw_base = base_path.read_bytes()
        digest.update(raw_base)
        root = json.loads(raw_base)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, 1
    if not isinstance(root, dict):
        return None, 1
    external_id = _label(root.get("id"), 512) or _label(path.name, 512)
    if external_id is None:
        return None, 1

    events: list[dict[str, Any]] = []
    malformed = 0
    events_dir = path / "events"
    if events_dir.is_dir():
        for event_path in sorted(events_dir.glob("event-*.json")):
            try:
                raw_event = event_path.read_bytes()
                digest.update(event_path.name.encode())
                digest.update(raw_event)
                event = json.loads(raw_event)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
                malformed += 1
                continue
            events.append(event)

    return (
        _Conversation(
            machine=machine,
            external_id=external_id,
            root=root,
            events=events,
            event_count=len(events),
            usage_count=_usage_count(root),
            digest=digest.hexdigest(),
        ),
        malformed,
    )


def _rank(value: _Conversation) -> tuple[int, int, str]:
    return value.event_count, value.usage_count, value.digest


def _usage_count(root: dict[str, Any]) -> int:
    return sum(
        len(_list(_mapping(metrics).get("token_usages")))
        for metrics in _mapping(
            _mapping(root.get("stats")).get("usage_to_metrics")
        ).values()
    )


def _model_calls(
    root: dict[str, Any],
    responses: dict[str, tuple[int | None, datetime | None, int]],
) -> list[tuple[str | None, str, dict[str, int], int, int]]:
    result: list[tuple[str | None, str, dict[str, int], int, int]] = []
    unmatched_order = len(responses) + 1
    metrics_map = _mapping(_mapping(root.get("stats")).get("usage_to_metrics"))
    for metrics in metrics_map.values():
        metrics = _mapping(metrics)
        fallback_model = _label(metrics.get("model_name"), 255) or "unknown"
        usages = [
            value
            for value in _list(metrics.get("token_usages"))
            if isinstance(value, dict)
        ]
        if not usages:
            aggregate = _mapping(metrics.get("accumulated_token_usage"))
            if any(
                _counter(aggregate.get(field))
                for field in (
                    "prompt_tokens",
                    "completion_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
            ):
                usages = [aggregate]
        for offset, usage in enumerate(usages):
            response_id = _label(usage.get("response_id"), 512)
            model = _label(usage.get("model"), 255) or fallback_model
            event_index = responses.get(
                response_id or "", (None, None, unmatched_order + offset)
            )[2]
            result.append(
                (
                    response_id,
                    model,
                    _tokens(usage),
                    _counter(usage.get("context_window")),
                    event_index,
                )
            )
    return result


def _tokens(usage: dict[str, Any]) -> dict[str, int]:
    prompt = _counter(usage.get("prompt_tokens"))
    cached = _counter(usage.get("cache_read_tokens"))
    cache_write = _counter(usage.get("cache_write_tokens"))
    output = _counter(usage.get("completion_tokens"))
    reasoning = min(output, _counter(usage.get("reasoning_tokens")))
    if _sum(cached, cache_write) <= prompt:
        input_tokens = prompt
        uncached = prompt - cached - cache_write
    else:
        input_tokens = _sum(prompt, cached, cache_write)
        uncached = prompt
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": _sum(input_tokens, output),
        "uncached_input_tokens": uncached,
        "visible_output_tokens": output - reasoning,
        "unattributed_tokens": 0,
    }


def _agent_model(root: dict[str, Any]) -> str | None:
    return _label(_mapping(_mapping(root.get("agent")).get("llm")).get("model"), 255)


def _effort(root: dict[str, Any]) -> str | None:
    value = _mapping(_mapping(root.get("agent")).get("llm")).get("reasoning_effort")
    return value if value in EFFORTS else None


def _project(root: dict[str, Any], mappings: list[tuple[str, str]]) -> tuple[str, str]:
    working_dir = _mapping(root.get("workspace")).get("working_dir")
    if isinstance(working_dir, str):
        normalized = working_dir.replace("\\", "/").rstrip("/")
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
    if start is not None and end is not None:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (OverflowError, ValueError):
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
