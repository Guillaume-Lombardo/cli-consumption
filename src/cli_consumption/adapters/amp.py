from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderInputBudget,
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
from cli_consumption.adapters._shared import (
    list_value as _list,
)
from cli_consumption.adapters._shared import (
    mapping as _mapping,
)
from cli_consumption.models import Snapshot, empty_tokens


@dataclass(slots=True)
class _Thread:
    machine: str
    external_id: str
    root: dict[str, Any]
    event_count: int
    digest: str


class AmpAdapter:
    """Read Amp thread metadata while discarding conversation content."""

    name = "amp"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Thread] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            threads = home / "threads"
            if not threads.is_dir():
                raise ValueError(f"Missing Amp threads directory: {threads}")
            for path in budget.sorted_paths(threads.glob("T-*.json")):
                candidate, invalid = _read_thread(path, machine, budget)
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
        for thread in sorted(selected.values(), key=lambda value: value.external_id):
            self._normalize(snapshot, thread, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Thread,
        mappings: list[tuple[str, str]],
    ) -> None:
        root = source.root
        messages = _list(root.get("messages"))
        conversation_id = f"amp:{source.external_id}"
        created = _timestamp(root.get("created"))
        timestamps = [created]
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        message_turns: dict[str, str | None] = {}
        calls: list[
            tuple[str | None, datetime | None, str, dict[str, int], int | None]
        ] = []
        tools: list[tuple[str | None, datetime | None, str]] = []

        for message in messages:
            if not isinstance(message, dict):
                continue
            usage = _mapping(message.get("usage"))
            timestamp = _timestamp(usage.get("timestamp")) or _timestamp(
                message.get("timestamp")
            )
            timestamps.append(timestamp)
            role = message.get("role")
            if role == "user" and _is_visible_user_message(message.get("content")):
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = (
                    _identifier(message.get("messageId"))
                    or _label(message.get("id"), 512)
                    or f"turn-{len(turns) + 1}"
                )
                if external_id in turns:
                    external_id = f"turn-{len(turns) + 1}"
                active = external_id
                turns[active] = {
                    "id": f"{conversation_id}:{active}",
                    "conversation_id": conversation_id,
                    "external_id": active,
                    "started_at": _iso(
                        timestamp or (created if len(turns) == 0 else None)
                    ),
                    "ended_at": None,
                    "status": "in-progress",
                    "duration_ms": None,
                    "time_to_first_token_ms": None,
                    "model_calls": 0,
                    "tool_calls": 0,
                    **empty_tokens(),
                }
                turn_models[active] = set()

            message_id = _identifier(message.get("messageId"))
            if message_id is not None:
                message_turns[message_id] = active

            if role != "assistant":
                continue
            if active:
                turns[active]["ended_at"] = _iso(timestamp) or turns[active]["ended_at"]
                stop_reason = message.get("stop_reason") or message.get("stopReason")
                if stop_reason in {"error", "aborted", "cancelled"}:
                    turns[active]["status"] = "aborted"
                elif stop_reason in {"end_turn", "stop", "length", "max_tokens"}:
                    turns[active]["status"] = "completed"

            if _has_usage(usage):
                model = (
                    _label(usage.get("model"), 255)
                    or _label(message.get("model"), 255)
                    or "unknown"
                )
                tokens = _message_tokens(usage, model)
                context_window = _positive_counter(usage.get("maxInputTokens"))
                calls.append((active, timestamp, model, tokens, context_window))

            for block in _list(message.get("content")):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = _label(block.get("name"), 512)
                if name:
                    tools.append((active, timestamp, name))

        ledger = _mapping(root.get("usageLedger"))
        ledger_events = ledger.get("events")
        if isinstance(ledger_events, list):
            ledger_calls = _ledger_calls(ledger_events, messages, message_turns)
            if ledger_calls:
                calls = ledger_calls

        ended_at = max(
            (value for value in timestamps if value is not None), default=None
        )
        for trace in _list(_mapping(root.get("meta")).get("traces")):
            if isinstance(trace, dict):
                ended_at = max(
                    (
                        value
                        for value in (ended_at, _timestamp(trace.get("endTime")))
                        if value
                    ),
                    default=None,
                )
        if active:
            _finish_turn(turns[active], ended_at)

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (turn_key, timestamp, model, tokens, context_window) in enumerate(
            calls, 1
        ):
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
            if context_window is not None:
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
            if turn["status"] == "in-progress" and turn["model_calls"]:
                turn["status"] = "completed"
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

        started_at = min(
            (value for value in timestamps if value is not None), default=None
        )
        project, project_source = _project(root, mappings)
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
                "source": "local-json",
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


def _read_thread(
    path: Path, machine: str, budget: ProviderInputBudget
) -> tuple[_Thread | None, int]:
    raw = read_bounded_bytes(path, budget)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        root = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, 1
    if not isinstance(root, dict):
        return None, 1
    external_id = _label(root.get("id"), 512) or _label(path.stem, 512)
    messages = root.get("messages")
    if external_id is None or not isinstance(messages, list):
        return None, 1
    malformed = sum(not isinstance(message, dict) for message in messages)
    events = _mapping(root.get("usageLedger")).get("events")
    if events is not None and not isinstance(events, list):
        malformed += 1
        events = []
    malformed += sum(not isinstance(event, dict) for event in events or [])
    return (
        _Thread(
            machine=machine,
            external_id=external_id,
            root=root,
            event_count=len(messages) + len(events or []),
            digest=digest,
        ),
        malformed,
    )


def _rank(value: _Thread) -> tuple[int, str]:
    return value.event_count, value.digest


def _ledger_calls(
    events: list[Any],
    messages: list[Any],
    message_turns: dict[str, str | None],
) -> list[tuple[str | None, datetime | None, str, dict[str, int], int | None]]:
    cache_by_message: dict[str, tuple[int, int]] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        identifier = _identifier(message.get("messageId"))
        if identifier is None:
            continue
        usage = _mapping(message.get("usage"))
        cache_by_message[identifier] = (
            _counter(usage.get("cacheCreationInputTokens")),
            _counter(usage.get("cacheReadInputTokens")),
        )

    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        model = _label(event.get("model"), 255)
        tokens = _mapping(event.get("tokens"))
        if model is None or not tokens:
            continue
        target = _identifier(event.get("toMessageId"))
        cache_write, cache_read = cache_by_message.get(target or "", (0, 0))
        uncached = _counter(tokens.get("input"))
        if model.startswith("gpt-"):
            uncached = _sum(uncached, cache_write)
            cache_write = 0
        output = _counter(tokens.get("output"))
        input_tokens = _sum(uncached, cache_write, cache_read)
        attributed = _sum(input_tokens, output)
        total = max(attributed, _counter(tokens.get("total")))
        if total == 0:
            continue
        result.append(
            (
                message_turns.get(target or ""),
                _timestamp(event.get("timestamp")),
                model,
                {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cache_read,
                    "cache_write_input_tokens": cache_write,
                    "output_tokens": output,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total,
                    "uncached_input_tokens": uncached,
                    "visible_output_tokens": output,
                    "unattributed_tokens": max(0, total - attributed),
                },
                None,
            )
        )
    return result


def _message_tokens(usage: dict[str, Any], model: str) -> dict[str, int]:
    uncached = _counter(usage.get("inputTokens"))
    cache_write = _counter(usage.get("cacheCreationInputTokens"))
    cache_read = _counter(usage.get("cacheReadInputTokens"))
    if model.startswith("gpt-"):
        uncached = _sum(uncached, cache_write)
        cache_write = 0
    output = _counter(usage.get("outputTokens"))
    input_tokens = _sum(uncached, cache_write, cache_read)
    attributed = _sum(input_tokens, output)
    total = max(attributed, _counter(usage.get("totalTokens")))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": total,
        "uncached_input_tokens": uncached,
        "visible_output_tokens": output,
        "unattributed_tokens": max(0, total - attributed),
    }


def _has_usage(usage: dict[str, Any]) -> bool:
    return any(
        key in usage
        for key in (
            "inputTokens",
            "outputTokens",
            "cacheCreationInputTokens",
            "cacheReadInputTokens",
            "totalTokens",
        )
    )


def _is_visible_user_message(content: object) -> bool:
    if isinstance(content, str):
        return bool(content)
    if not isinstance(content, list):
        return False
    blocks = [block for block in content if isinstance(block, dict)]
    return any(block.get("type") != "tool_result" for block in blocks)


def _project(root: dict[str, Any], mappings: list[tuple[str, str]]) -> tuple[str, str]:
    initial = _mapping(_mapping(root.get("env")).get("initial"))
    tree = next(
        (value for value in _list(initial.get("trees")) if isinstance(value, dict)),
        {},
    )
    directory = next(
        (
            value
            for value in (
                initial.get("cwd"),
                tree.get("path"),
                tree.get("root"),
                tree.get("directory"),
            )
            if isinstance(value, str) and value
        ),
        None,
    )
    if directory:
        normalized = directory.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            prefix = prefix.replace("\\", "/").rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return name, "mapping"
    return "outside-project", "none"


def _finish_turn(turn: dict[str, Any], fallback: datetime | None) -> None:
    turn["ended_at"] = turn["ended_at"] or _iso(fallback)
    if turn["status"] == "in-progress" and turn["model_calls"]:
        turn["status"] = "completed"
    start, end = _timestamp(turn["started_at"]), _timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))


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


def _identifier(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _label(value, 512)


def _positive_counter(value: object) -> int | None:
    result = _counter(value)
    return result or None
