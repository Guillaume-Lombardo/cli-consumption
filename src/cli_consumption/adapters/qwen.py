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

RECORD_TYPES = {"user", "assistant", "tool_result", "system"}
ARTIFACT_SUBTYPES = {"session_artifact_event", "session_artifact_snapshot"}


@dataclass(slots=True)
class _Candidate:
    machine: str
    path: Path
    external_id: str
    event_count: int
    digest: str


class QwenAdapter:
    """Read Qwen Code transcript metadata while discarding conversation content."""

    name = "qwen"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Candidate] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            projects = home / "projects"
            if not projects.is_dir():
                raise ValueError(f"Missing Qwen Code projects directory: {projects}")
            for path in budget.sorted_paths(projects.glob("*/chats/*.jsonl")):
                records, invalid, digest = _read_records(path, budget)
                malformed += invalid
                session_id = _session_id(records)
                if session_id is None:
                    malformed += 1
                    continue
                candidate = _Candidate(
                    machine=machine,
                    path=path,
                    external_id=session_id,
                    event_count=len(records),
                    digest=digest,
                )
                previous = selected.get(session_id)
                if previous is None:
                    selected[session_id] = candidate
                    continue
                duplicates += 1
                if _rank(candidate) > _rank(previous):
                    selected[session_id] = candidate

        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=malformed,
        )
        for candidate in sorted(selected.values(), key=lambda item: item.external_id):
            records, _, _ = _read_records(candidate.path, budget)
            active, invalid = _active_branch(records, candidate.external_id)
            snapshot.malformed_records += invalid
            self._normalize(snapshot, candidate, active, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Candidate,
        records: list[dict[str, Any]],
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"qwen:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        calls: list[
            tuple[str | None, datetime | None, str, dict[str, int], int | None]
        ] = []
        tools: list[tuple[str | None, datetime | None, str]] = []
        compactions: list[tuple[str | None, datetime | None]] = []
        timestamps: list[datetime] = []

        for record in records:
            timestamp = _timestamp(record.get("timestamp"))
            if timestamp is not None:
                timestamps.append(timestamp)
            record_type = record.get("type")
            subtype = record.get("subtype")
            if (
                record_type == "user"
                and subtype is None
                and not record.get("isSidechain")
            ):
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = _label(record.get("uuid"), 512) or (
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

            if record_type == "system" and subtype == "chat_compression":
                compactions.append((active, timestamp))
                continue
            if (
                record_type != "assistant"
                or record.get("isSidechain")
                or subtype == "realtime_message"
            ):
                continue

            model = _label(record.get("model"), 255) or "unknown"
            tokens = _usage(record.get("usageMetadata"))
            context_window = _positive_counter(record.get("contextWindowSize"))
            calls.append((active, timestamp, model, tokens, context_window))
            if active is not None:
                turns[active]["status"] = "completed"
                turns[active]["ended_at"] = _iso(timestamp)

            message = record.get("message")
            parts = message.get("parts") if isinstance(message, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                function_call = part.get("functionCall")
                if not isinstance(function_call, dict):
                    continue
                name = _label(function_call.get("name"), 512)
                if name is not None:
                    tools.append((active, timestamp, name))

        ended_at = max(timestamps, default=None)
        if active is not None:
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
            model_call_id = f"{conversation_id}:model:{sequence}"
            snapshot.model_calls.append(
                {
                    "id": model_call_id,
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
        project, project_source = _project(records, mappings)
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


def _read_records(
    path: Path, budget: ProviderInputBudget
) -> tuple[list[dict[str, Any]], int, str]:
    digest = hashlib.sha256()
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in iter_bounded_jsonl_bytes(path, budget):
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
    return records, malformed, digest.hexdigest()


def _session_id(records: list[dict[str, Any]]) -> str | None:
    for record in records:
        if session_id := _label(record.get("sessionId"), 512):
            return session_id
    return None


def _active_branch(
    records: list[dict[str, Any]], session_id: str
) -> tuple[list[dict[str, Any]], int]:
    valid: list[dict[str, Any]] = []
    first_by_uuid: dict[str, dict[str, Any]] = {}
    fragments_by_uuid: dict[str, list[dict[str, Any]]] = {}
    malformed = 0
    for record in records:
        uuid = _label(record.get("uuid"), 512)
        parent = record.get("parentUuid")
        record_session = _label(record.get("sessionId"), 512)
        if (
            uuid is None
            or (parent is not None and _label(parent, 512) is None)
            or record_session != session_id
            or record.get("type") not in RECORD_TYPES
        ):
            malformed += 1
            continue
        valid.append(record)
        first_by_uuid.setdefault(uuid, record)
        fragments_by_uuid.setdefault(uuid, []).append(record)

    leaf = next(
        (
            record
            for record in reversed(valid)
            if not (
                record.get("type") == "system"
                and record.get("subtype") in ARTIFACT_SUBTYPES
            )
        ),
        None,
    )
    if leaf is None:
        return [], malformed

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = leaf
    while current is not None:
        uuid = str(current["uuid"])
        if uuid in seen:
            malformed += 1
            break
        seen.add(uuid)
        chain.append(_aggregate_fragments(fragments_by_uuid[uuid]))
        parent = current.get("parentUuid")
        if parent is None:
            break
        current = first_by_uuid.get(str(parent))
        if current is None:
            malformed += 1
            break
    chain.reverse()
    return chain, malformed


def _aggregate_fragments(records: list[dict[str, Any]]) -> dict[str, Any]:
    result = dict(records[0])
    first_message = records[0].get("message")
    message = dict(first_message) if isinstance(first_message, dict) else None
    if message is not None:
        parts = message.get("parts")
        message["parts"] = list(parts) if isinstance(parts, list) else []

    for record in records[1:]:
        fragment_message = record.get("message")
        if isinstance(fragment_message, dict):
            fragment_parts = fragment_message.get("parts")
            fragment_parts = fragment_parts if isinstance(fragment_parts, list) else []
            if message is None:
                message = dict(fragment_message)
                message["parts"] = list(fragment_parts)
            else:
                message["parts"].extend(fragment_parts)
        if isinstance(record.get("usageMetadata"), dict):
            result["usageMetadata"] = record["usageMetadata"]
        if result.get("model") is None and isinstance(record.get("model"), str):
            result["model"] = record["model"]
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str) and timestamp > str(result.get("timestamp", "")):
            result["timestamp"] = timestamp
    if message is not None:
        result["message"] = message
    return result


def _usage(value: object) -> dict[str, int]:
    usage = value if isinstance(value, dict) else {}
    input_tokens = _counter(usage.get("promptTokenCount"))
    cached = min(input_tokens, _counter(usage.get("cachedContentTokenCount")))
    visible = _counter(usage.get("candidatesTokenCount"))
    reasoning = _counter(usage.get("thoughtsTokenCount"))
    output_tokens = _sum(visible, reasoning)
    attributed = _sum(input_tokens, output_tokens)
    total = max(attributed, _counter(usage.get("totalTokenCount")))
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


def _project(
    records: list[dict[str, Any]], mappings: list[tuple[str, str]]
) -> tuple[str, str]:
    for record in records:
        if not isinstance((cwd := record.get("cwd")), str):
            continue
        cwd = cwd.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            prefix = prefix.replace("\\", "/").rstrip("/")
            if cwd == prefix or cwd.startswith(prefix + "/"):
                return name, "mapping"
    return "outside-project", "none"


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


def _positive_counter(value: object) -> int | None:
    counter = _counter(value)
    return counter if counter > 0 else None
