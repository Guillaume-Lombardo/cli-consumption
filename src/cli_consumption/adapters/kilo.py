from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderDataLimitError,
    ProviderInputBudget,
    open_provider_sqlite,
)
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")


@dataclass(slots=True)
class _Part:
    external_id: str
    message_id: str
    created_at: datetime | None
    data: dict[str, Any]


@dataclass(slots=True)
class _Message:
    external_id: str
    created_at: datetime | None
    updated_at: datetime | None
    data: dict[str, Any]
    parts: list[_Part]


@dataclass(slots=True)
class _Conversation:
    machine: str
    external_id: str
    directory: str | None
    created_at: datetime | None
    updated_at: datetime | None
    messages: list[_Message]
    event_count: int
    digest: str


class KiloAdapter:
    """Read metadata from Kilo Code's local SQLite session store."""

    name = "kilo"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Conversation] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            database = budget.candidate(home / "kilo.db")
            if not database.is_file():
                raise ValueError(f"Missing Kilo Code database: {database}")
            conversations, invalid = _read_database(database, machine, budget)
            malformed += invalid
            for candidate in conversations:
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
            selected.values(), key=lambda item: item.external_id
        ):
            self._normalize(snapshot, conversation, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Conversation,
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"kilo:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        calls: list[tuple[str | None, datetime | None, str, dict[str, int]]] = []
        tools: list[tuple[str | None, datetime | None, str]] = []
        compactions: list[tuple[str | None, datetime | None]] = []
        timestamps = [source.created_at, source.updated_at]

        for message in source.messages:
            role = _label(message.data.get("role"), 32)
            timestamp = message.created_at or _timestamp(
                _mapping(message.data.get("time")).get("created")
            )
            completed_at = _timestamp(
                _mapping(message.data.get("time")).get("completed")
            )
            timestamps.extend((timestamp, completed_at))

            if role == "user":
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                active = message.external_id
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

            turn_key = active
            if role == "assistant":
                parent = _label(message.data.get("parentID"), 512)
                if parent in turns:
                    turn_key = parent
                model = _model(message.data) or "unknown"
                tokens = _usage(message.data.get("tokens"))
                calls.append((turn_key, timestamp, model, tokens))
                turn = turns.get(turn_key or "")
                if turn:
                    turn["model_calls"] += 1
                    turn_models[turn_key or ""].add(model)
                    _add_tokens(turn, tokens)
                    turn["ended_at"] = (
                        _iso(completed_at or timestamp) or turn["ended_at"]
                    )
                    if isinstance(message.data.get("error"), dict):
                        turn["status"] = "aborted"
                    elif (
                        completed_at is not None
                        or _label(message.data.get("finish"), 255)
                    ) and turn["status"] != "aborted":
                        turn["status"] = "completed"

            for part in message.parts:
                kind = _label(part.data.get("type"), 64)
                if kind == "tool":
                    name = _label(part.data.get("tool"), 512)
                    if not name:
                        continue
                    state_time = _mapping(_mapping(part.data.get("state")).get("time"))
                    tools.append(
                        (
                            turn_key,
                            _timestamp(state_time.get("start"))
                            or part.created_at
                            or timestamp,
                            name,
                        )
                    )
                elif kind == "compaction":
                    compactions.append((turn_key, part.created_at or timestamp))

        ended_at = max(
            (value for value in timestamps if value is not None), default=None
        )
        if active is not None:
            _finish_turn(turns[active], ended_at)

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (turn_key, timestamp, model, tokens) in enumerate(calls, 1):
            models.add(model)
            _add_tokens(totals, tokens)
            turn = turns.get(turn_key or "")
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
                    "effort": None,
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": None,
                }
            )

        started_at = source.created_at or min(
            (value for value in timestamps if value is not None), default=None
        )
        project, project_source = _project(source.directory, mappings)
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
                    max(0.0, (ended_at - started_at).total_seconds())
                    if started_at and ended_at
                    else None
                ),
                "source": "local-sqlite-v1",
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


def _read_database(
    path: Path, machine: str, budget: ProviderInputBudget
) -> tuple[list[_Conversation], int]:
    try:
        manager = open_provider_sqlite(path, budget)
        connection = manager.__enter__()
        connection.row_factory = sqlite3.Row
        session_columns = _columns(connection, "session")
        message_columns = _columns(connection, "message")
        part_columns = _columns(connection, "part")
        if (
            not {"id", "time_created", "time_updated"} <= session_columns
            or not {
                "id",
                "session_id",
                "time_created",
                "time_updated",
                "data",
            }
            <= message_columns
            or not {
                "id",
                "message_id",
                "session_id",
                "time_created",
                "time_updated",
                "data",
            }
            <= part_columns
        ):
            raise UnsupportedProviderFormat(
                f"Unsupported Kilo Code database schema: {path}"
            )

        directory = "directory" if "directory" in session_columns else "NULL"
        rows = budget.rows(
            connection.execute(
                f"SELECT id, {directory} AS directory, time_created, time_updated "
                "FROM session ORDER BY id"
            )
        )
        conversations: list[_Conversation] = []
        malformed = 0
        for row in rows:
            external_id = _label(row["id"], 500)
            if not external_id:
                malformed += 1
                continue
            message_rows = budget.rows(
                connection.execute(
                    "SELECT id, time_created, time_updated, data FROM message "
                    "WHERE session_id = ? ORDER BY time_created, id",
                    (row["id"],),
                )
            )
            part_rows = budget.rows(
                connection.execute(
                    "SELECT id, message_id, time_created, data FROM part "
                    "WHERE session_id = ? ORDER BY time_created, id",
                    (row["id"],),
                )
            )
            digest = hashlib.sha256()
            parts_by_message: dict[str, list[_Part]] = {}
            part_count = 0
            for part_row in part_rows:
                part_count += 1
                budget.json_field(part_row["data"])
                digest.update(str(tuple(part_row)).encode())
                part_id = _label(part_row["id"], 512)
                message_id = _label(part_row["message_id"], 512)
                try:
                    data = json.loads(part_row["data"])
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                    malformed += 1
                    continue
                if not part_id or not message_id or not isinstance(data, dict):
                    malformed += 1
                    continue
                parts_by_message.setdefault(message_id, []).append(
                    _Part(
                        part_id, message_id, _timestamp(part_row["time_created"]), data
                    )
                )

            messages: list[_Message] = []
            message_count = 0
            for message_row in message_rows:
                message_count += 1
                budget.json_field(message_row["data"])
                digest.update(str(tuple(message_row)).encode())
                message_id = _label(message_row["id"], 512)
                try:
                    data = json.loads(message_row["data"])
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                    malformed += 1
                    continue
                if not message_id or not isinstance(data, dict):
                    malformed += 1
                    continue
                messages.append(
                    _Message(
                        message_id,
                        _timestamp(message_row["time_created"]),
                        _timestamp(message_row["time_updated"]),
                        data,
                        parts_by_message.pop(message_id, []),
                    )
                )
            malformed += sum(len(parts) for parts in parts_by_message.values())
            conversations.append(
                _Conversation(
                    machine=machine,
                    external_id=external_id,
                    directory=row["directory"]
                    if isinstance(row["directory"], str)
                    else None,
                    created_at=_timestamp(row["time_created"]),
                    updated_at=_timestamp(row["time_updated"]),
                    messages=messages,
                    event_count=message_count + part_count,
                    digest=digest.hexdigest(),
                )
            )
        return conversations, malformed
    except sqlite3.DataError:
        raise ProviderDataLimitError("provider_sqlite_field_too_large") from None
    except sqlite3.DatabaseError:
        raise ValueError(f"Could not read Kilo Code database: {path}") from None
    finally:
        if "connection" in locals():
            manager.__exit__(None, None, None)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _rank(value: _Conversation) -> tuple[int, datetime, str]:
    return (
        value.event_count,
        value.updated_at or datetime.min.replace(tzinfo=UTC),
        value.digest,
    )


def _usage(value: object) -> dict[str, int]:
    usage = _mapping(value)
    cache = _mapping(usage.get("cache"))
    uncached = _counter(usage.get("input"))
    cached = _counter(cache.get("read"))
    cache_write = _counter(cache.get("write"))
    visible = _counter(usage.get("output"))
    reasoning = _counter(usage.get("reasoning"))
    input_tokens = _sum(uncached, cached, cache_write)
    output_tokens = _sum(visible, reasoning)
    attributed = _sum(input_tokens, output_tokens)
    total = max(attributed, _counter(usage.get("total")))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "uncached_input_tokens": uncached,
        "visible_output_tokens": visible,
        "unattributed_tokens": max(0, total - attributed),
    }


def _model(value: object) -> str | None:
    message = _mapping(value)
    provider = _label(message.get("providerID"), 127)
    identifier = _label(message.get("modelID"), 127)
    if provider and identifier:
        return f"{provider}/{identifier}"
    return identifier


def _project(directory: str | None, mappings: list[tuple[str, str]]) -> tuple[str, str]:
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
