from __future__ import annotations

import hashlib
import json
import math
import re
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


class ClaudeAdapter:
    """Read Claude Code transcript metadata while discarding conversation content."""

    name = "claude"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected, duplicates, malformed = self._discover(sources)
        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=malformed,
        )
        for machine, path, count, digest, session_id in selected:
            self._read(
                snapshot,
                machine,
                path,
                count,
                digest,
                session_id,
                project_mappings or [],
            )
        return snapshot

    def _discover(
        self, sources: list[tuple[str, Path]]
    ) -> tuple[list[tuple[str, Path, int, str, str]], int, int]:
        budget = ProviderInputBudget()
        selected: dict[str, tuple[str, Path, int, str, str]] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            projects = home / "projects"
            if not projects.is_dir():
                raise ValueError(f"Missing Claude Code projects directory: {projects}")
            for path in budget.sorted_paths(projects.glob("*/*.jsonl")):
                digest = hashlib.sha256()
                count = 0
                session_id: str | None = None
                for line in iter_bounded_jsonl_bytes(path):
                    digest.update(line)
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        malformed += 1
                        continue
                    if not isinstance(event, dict):
                        malformed += 1
                        continue
                    count += 1
                    session_id = session_id or _label(event.get("sessionId"), 512)
                content_hash = digest.hexdigest()
                session_id = (
                    session_id
                    or _label(path.stem, 512)
                    or f"session-{content_hash[:24]}"
                )
                candidate = (machine, path, count, content_hash, session_id)
                previous = selected.get(session_id)
                if previous is None:
                    selected[session_id] = candidate
                else:
                    duplicates += 1
                    if candidate[2:4] > previous[2:4]:
                        selected[session_id] = candidate
        return list(selected.values()), duplicates, malformed

    def _read(
        self,
        snapshot: Snapshot,
        machine: str,
        path: Path,
        event_count: int,
        digest: str,
        session_id: str,
        mappings: list[tuple[str, str]],
    ) -> None:
        events: list[dict[str, Any]] = []
        for line in iter_bounded_jsonl_bytes(path):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)

        conversation_id = f"claude:{session_id}"
        timestamps = [
            value
            for event in events
            if (value := _timestamp(event.get("timestamp"))) is not None
        ]
        started_at, ended_at = (
            min(timestamps, default=None),
            max(timestamps, default=None),
        )
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        # request/message ID -> (rank, event index, turn, timestamp, model, tokens)
        calls: dict[
            str,
            tuple[
                tuple[bool, int, int],
                int,
                str | None,
                datetime | None,
                str,
                dict[str, int],
            ],
        ] = {}
        tools: dict[str, tuple[str | None, datetime | None, str]] = {}
        compactions = 0

        for index, event in enumerate(events, 1):
            timestamp = _timestamp(event.get("timestamp"))
            if _starts_turn(event):
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = _label(event.get("uuid"), 512) or f"turn-{len(turns) + 1}"
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

            if event.get("type") == "system" and event.get("subtype") in {
                "compact",
                "compact_boundary",
            }:
                compactions += 1
                snapshot.compaction_events.append(
                    {
                        "id": f"{conversation_id}:compaction:{compactions}",
                        "conversation_id": conversation_id,
                        "turn_id": turns[active]["id"] if active else None,
                        "sequence": compactions,
                        "timestamp": _iso(timestamp),
                    }
                )

            if event.get("type") != "assistant" or not isinstance(
                (message := event.get("message")), dict
            ):
                continue
            if active:
                turn = turns[active]
                turn["ended_at"] = _iso(timestamp) or turn["ended_at"]
                turn["status"] = (
                    "aborted" if event.get("isApiErrorMessage") is True else "completed"
                )
            model = _label(message.get("model"), 255) or "unknown"
            call_key = (
                _label(event.get("requestId"), 512)
                or _label(message.get("id"), 512)
                or _label(event.get("uuid"), 512)
                or f"event-{index}"
            )
            if isinstance((usage := message.get("usage")), dict):
                tokens = _usage(usage)
                rank = (
                    message.get("stop_reason") is not None,
                    sum(tokens.values()),
                    index,
                )
                previous = calls.get(call_key)
                if previous is None or rank > previous[0]:
                    calls[call_key] = (rank, index, active, timestamp, model, tokens)
            if not isinstance((content := message.get("content")), list):
                continue
            for block_index, block in enumerate(content, 1):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = _label(block.get("name"), 512)
                if name:
                    key = _label(block.get("id"), 512) or (
                        f"{call_key}:{block_index}:{name}"
                    )
                    tools.setdefault(key, (active, timestamp, name))

        if active:
            _finish_turn(turns[active], ended_at)

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, call in enumerate(
            sorted(calls.values(), key=lambda row: row[1]), 1
        ):
            _, _, turn_key, timestamp, model, tokens = call
            models.add(model)
            turn = turns.get(turn_key or "")
            if turn:
                turn["model_calls"] += 1
                turn_models[turn_key or ""].add(model)
                _add_tokens(turn, tokens)
            _add_tokens(totals, tokens)
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

        project, project_source = _project(events, mappings)
        snapshot.conversations.append(
            {
                "id": conversation_id,
                "provider": self.name,
                "external_id": session_id,
                "source_machine": machine,
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
                "compactions": compactions,
                "event_count": event_count,
                "content_hash": digest,
                **totals,
            }
        )


def _starts_turn(event: dict[str, Any]) -> bool:
    if (
        event.get("type") != "user"
        or event.get("isMeta") is True
        or event.get("isSidechain") is True
        or event.get("toolUseResult") is not None
        or not isinstance((message := event.get("message")), dict)
    ):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    types = {str(item.get("type")) for item in content if isinstance(item, dict)}
    return bool(types - {"tool_result"})


def _usage(value: dict[str, Any]) -> dict[str, int]:
    uncached = _counter(value.get("input_tokens"))
    cached = _counter(value.get("cache_read_input_tokens"))
    cache_write = _counter(value.get("cache_creation_input_tokens"))
    output = _counter(value.get("output_tokens"))
    input_tokens = min(MAX_BIGINT, uncached + cached + cache_write)
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": min(MAX_BIGINT, input_tokens + output),
        "uncached_input_tokens": uncached,
        "visible_output_tokens": output,
        "unattributed_tokens": 0,
    }


def _project(
    events: list[dict[str, Any]], mappings: list[tuple[str, str]]
) -> tuple[str, str]:
    for event in events:
        if not isinstance((cwd := event.get("cwd")), str):
            continue
        cwd = cwd.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            prefix = prefix.replace("\\", "/").rstrip("/")
            if cwd == prefix or cwd.startswith(prefix + "/"):
                return name, "mapping"
    return "outside-project", "none"


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


def _add_tokens(target: dict[str, Any], tokens: dict[str, int]) -> None:
    for field, value in tokens.items():
        target[field] = min(MAX_BIGINT, int(target[field]) + value)
