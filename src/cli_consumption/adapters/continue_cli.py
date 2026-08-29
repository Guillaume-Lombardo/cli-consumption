from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import ProviderInputBudget, read_bounded_bytes
from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")


@dataclass(slots=True)
class _Session:
    machine: str
    external_id: str
    root: dict[str, Any]
    modified_at: datetime
    event_count: int
    digest: str


class ContinueAdapter:
    """Read Continue CLI session metadata while discarding conversation content."""

    name = "continue"

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
                raise ValueError(f"Missing Continue sessions directory: {sessions}")
            for path in budget.sorted_paths(sessions.glob("*.json")):
                if path.name == "sessions.json":
                    continue
                candidate, invalid = _read_session(path, machine, budget)
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
        root = source.root
        history = _list(root.get("history"))
        conversation_id = f"continue:{source.external_id}"
        session_model = _label(root.get("chatModelTitle"), 255)
        turns: list[dict[str, Any]] = []
        turn_models: list[set[str]] = []
        active: int | None = None
        raw_calls: list[tuple[int | None, str, dict[str, int]]] = []
        tools: list[tuple[int | None, str]] = []
        seen_tools: set[str] = set()

        for item in history:
            if not isinstance(item, dict):
                continue
            message = _mapping(item.get("message"))
            role = message.get("role")
            if role == "user" and _has_visible_content(message.get("content")):
                active = len(turns)
                external_id = f"turn-{active + 1}"
                turns.append(
                    {
                        "id": f"{conversation_id}:{external_id}",
                        "conversation_id": conversation_id,
                        "external_id": external_id,
                        "started_at": None,
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
                continue

            if role != "assistant":
                continue
            model = _message_model(item) or session_model or "unknown"
            usage = _usage(message.get("usage"))
            raw_calls.append((active, model, usage))
            if active is not None:
                turns[active]["status"] = "completed"
                turn_models[active].add(model)

            for tool in _tool_calls(message, item):
                name = _label(_mapping(tool.get("function")).get("name"), 512)
                if name is None:
                    continue
                identifier = _label(tool.get("id"), 512)
                dedupe_key = identifier or f"{len(tools)}:{name}"
                if identifier is not None and dedupe_key in seen_tools:
                    continue
                seen_tools.add(dedupe_key)
                tools.append((active, name))

        aggregate = _usage(root.get("usage"))
        residual = _residual_usage(aggregate, [usage for _, _, usage in raw_calls])
        if any(residual.values()):
            raw_calls.append((None, session_model or _last_model(raw_calls), residual))

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (turn_index, model, usage) in enumerate(raw_calls, 1):
            tokens = _tokens(usage)
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
                    "timestamp": None,
                    "model": model,
                    **tokens,
                }
            )

        for sequence, (turn_index, name) in enumerate(tools, 1):
            turn = turns[turn_index] if turn_index is not None else None
            if turn is not None:
                turn["tool_calls"] += 1
            snapshot.tool_calls.append(
                {
                    "id": f"{conversation_id}:tool:{sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": None,
                    "tool_name": name,
                    "outer_tool_name": name,
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
                    "model": (
                        next(iter(observed_models))
                        if len(observed_models) == 1
                        else None
                    ),
                    "effort": None,
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": None,
                }
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
                "started_at": None,
                "ended_at": _iso(source.modified_at),
                "duration_seconds": None,
                "source": "local-json-v1",
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": len(raw_calls),
                "tool_calls": len(tools),
                "compactions": 0,
                "event_count": source.event_count,
                "content_hash": source.digest,
                **totals,
            }
        )


def _read_session(
    path: Path, machine: str, budget: ProviderInputBudget
) -> tuple[_Session | None, int]:
    raw = read_bounded_bytes(path, budget)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        root = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, 1
    if not isinstance(root, dict):
        return None, 1
    external_id = _label(root.get("sessionId"), 512) or _label(path.stem, 512)
    history = root.get("history")
    if external_id is None or not isinstance(history, list):
        return None, 1
    malformed = sum(
        not isinstance(item, dict)
        or not isinstance(_mapping(item).get("message"), dict)
        for item in history
    )
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return (
        _Session(
            machine=machine,
            external_id=external_id,
            root=root,
            modified_at=modified_at,
            event_count=len(history),
            digest=digest,
        ),
        malformed,
    )


def _rank(value: _Session) -> tuple[int, str]:
    return value.event_count, value.digest


def _message_model(item: dict[str, Any]) -> str | None:
    logs = _list(item.get("promptLogs"))
    for entry in reversed(logs):
        if not isinstance(entry, dict):
            continue
        model = _label(entry.get("modelTitle"), 255)
        provider = _label(entry.get("modelProvider"), 128)
        if model and provider and not model.startswith(f"{provider}/"):
            return f"{provider}/{model}"
        if model:
            return model
    return None


def _tool_calls(message: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    result = [
        value for value in _list(message.get("toolCalls")) if isinstance(value, dict)
    ]
    for state in _list(item.get("toolCallStates")):
        if isinstance(state, dict):
            call = state.get("toolCall")
            if isinstance(call, dict):
                result.append(call)
    return result


def _usage(value: object) -> dict[str, int]:
    usage = _mapping(value)
    prompt_details = _mapping(usage.get("promptTokensDetails"))
    completion_details = _mapping(usage.get("completionTokensDetails"))
    return {
        "prompt": _counter(usage.get("promptTokens")),
        "completion": _counter(usage.get("completionTokens")),
        "cached": _counter(prompt_details.get("cachedTokens")),
        "cache_write": _counter(prompt_details.get("cacheWriteTokens")),
        "reasoning": _counter(completion_details.get("reasoningTokens")),
    }


def _residual_usage(
    aggregate: dict[str, int], calls: list[dict[str, int]]
) -> dict[str, int]:
    return {
        key: max(0, aggregate[key] - sum(call[key] for call in calls))
        for key in aggregate
    }


def _tokens(usage: dict[str, int]) -> dict[str, int]:
    input_tokens = usage["prompt"]
    cached = min(input_tokens, usage["cached"])
    cache_write = min(max(0, input_tokens - cached), usage["cache_write"])
    output_tokens = usage["completion"]
    reasoning = min(output_tokens, usage["reasoning"])
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "total_tokens": _sum(input_tokens, output_tokens),
        "uncached_input_tokens": input_tokens - cached - cache_write,
        "visible_output_tokens": output_tokens - reasoning,
        "unattributed_tokens": 0,
    }


def _last_model(calls: list[tuple[int | None, str, dict[str, int]]]) -> str:
    return calls[-1][1] if calls else "unknown"


def _project(root: dict[str, Any], mappings: list[tuple[str, str]]) -> tuple[str, str]:
    directory = root.get("workspaceDirectory")
    if isinstance(directory, str) and directory:
        matches = [
            (len(prefix), name)
            for name, prefix in mappings
            if directory == prefix
            or directory.startswith(
                prefix.rstrip("/\\") + ("\\" if "\\" in prefix else "/")
            )
        ]
        if matches:
            return max(matches)[1], "explicit"
    return "unmapped", "unmapped"


def _has_visible_content(value: object) -> bool:
    if isinstance(value, str):
        return bool(value)
    if not isinstance(value, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") in {"text", "imageUrl"}
        for part in value
    )


def _add_tokens(target: dict[str, Any], values: dict[str, int]) -> None:
    for key in empty_tokens():
        target[key] = _sum(target[key], values[key])


def _counter(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= MAX_BIGINT else 0
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        integer = int(value)
        return integer if 0 <= integer <= MAX_BIGINT else 0
    return 0


def _sum(*values: int) -> int:
    return min(MAX_BIGINT, sum(values))


def _label(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not (1 <= len(value) <= maximum):
        return None
    return value if SAFE_LABEL.fullmatch(value) else None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
