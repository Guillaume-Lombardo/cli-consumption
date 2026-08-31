from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderInputBudget,
    ensure_provider_sqlite_fields,
    open_provider_sqlite,
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
    mapping as _mapping,
)
from cli_consumption.adapters._shared import (
    project as _project,
)
from cli_consumption.adapters._shared import (
    sqlite_columns as _columns,
)
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.models import Snapshot, empty_tokens


@dataclass(slots=True)
class _Part:
    external_id: str
    kind: str
    created_at: datetime | None
    updated_at: datetime | None
    data: dict[str, Any]


@dataclass(slots=True)
class _Message:
    external_id: str
    kind: str
    sequence: int
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
    digest: str


class OpenCodeAdapter:
    """Read metadata from OpenCode's SQLite v2 store without retaining content."""

    name = "opencode"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Conversation] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            database = budget.candidate(home / "opencode.db")
            if not database.is_file():
                raise ValueError(f"Missing OpenCode database: {database}")
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
        conversation_id = f"opencode:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        effective_model: str | None = None
        calls: list[tuple[str | None, datetime | None, str, dict[str, int]]] = []
        tools: list[tuple[str | None, datetime | None, str]] = []
        compactions: list[tuple[str | None, datetime | None]] = []
        timestamps = [source.created_at]

        for message in source.messages:
            timestamp = message.created_at or _timestamp(
                _mapping(message.data.get("time")).get("created")
            )
            completed_at = _timestamp(
                _mapping(message.data.get("time")).get("completed")
            )
            timestamps.extend((timestamp, completed_at))
            for part in message.parts:
                timestamps.extend((part.created_at, part.updated_at))

            if message.kind == "user":
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
            elif message.kind == "model-switched":
                effective_model = _model(message.data.get("model"))
            elif message.kind == "compaction":
                compactions.append((active, timestamp))
            elif message.kind == "assistant":
                model = _message_model(message.data) or effective_model or "unknown"
                effective_model = model if model != "unknown" else effective_model
                tokens = _usage(message.data.get("tokens"))
                calls.append((active, timestamp, model, tokens))
                turn = turns.get(active or "")
                if turn:
                    turn["model_calls"] += 1
                    turn_models[active or ""].add(model)
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
                if part.kind == "compaction":
                    compactions.append((active, part.created_at or timestamp))
                    continue
                if part.kind != "tool":
                    continue
                name = _label(part.data.get("tool") or part.data.get("name"), 512)
                if not name:
                    continue
                state_time = _mapping(_mapping(part.data.get("state")).get("time"))
                part_time = _mapping(part.data.get("time"))
                tools.append(
                    (
                        active,
                        part.created_at
                        or _timestamp(
                            part_time.get("created") or part_time.get("start")
                        )
                        or _timestamp(state_time.get("start"))
                        or timestamp,
                        name,
                    )
                )

        ended_at = max(
            (value for value in timestamps if value is not None), default=None
        )
        if active is not None:
            _finish_turn(turns[active], ended_at or source.updated_at)

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
        ended_at = ended_at or source.updated_at
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
                "source": "local-sqlite-v2",
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": len(calls),
                "tool_calls": len(tools),
                "compactions": len(compactions),
                "event_count": len(source.messages),
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
        required_session = {"id", "time_created", "time_updated"}
        if not required_session <= session_columns:
            raise UnsupportedProviderFormat(
                f"Unsupported OpenCode database schema: {path}"
            )

        message_columns = _columns(connection, "message")
        part_columns = _columns(connection, "part")
        current_schema_present = bool(message_columns or part_columns)
        if current_schema_present:
            required_message = {
                "id",
                "session_id",
                "time_created",
                "time_updated",
                "data",
            }
            required_part = {
                "id",
                "message_id",
                "session_id",
                "time_created",
                "time_updated",
                "data",
            }
            if (
                not required_message <= message_columns
                or not required_part <= part_columns
            ):
                raise UnsupportedProviderFormat(
                    f"Unsupported OpenCode database schema: {path}"
                )
            ensure_provider_sqlite_fields(
                connection, [("message", "data"), ("part", "data")]
            )
            read_messages = _read_current_messages
        else:
            projection_columns = _columns(connection, "session_message")
            required_projection = {
                "id",
                "session_id",
                "type",
                "seq",
                "time_created",
                "time_updated",
                "data",
            }
            if not required_projection <= projection_columns:
                raise UnsupportedProviderFormat(
                    f"Unsupported OpenCode database schema: {path}"
                )
            ensure_provider_sqlite_fields(connection, [("session_message", "data")])
            read_messages = _read_projection_messages

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
            digest = hashlib.sha256()
            messages, invalid = read_messages(connection, row["id"], budget, digest)
            malformed += invalid
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
                    digest=digest.hexdigest(),
                )
            )
        return conversations, malformed
    except sqlite3.DatabaseError:
        raise ValueError(f"Could not read OpenCode database: {path}") from None
    finally:
        if "connection" in locals():
            manager.__exit__(None, None, None)


def _read_current_messages(
    connection: sqlite3.Connection,
    session_id: object,
    budget: ProviderInputBudget,
    digest: Any,
) -> tuple[list[_Message], int]:
    message_rows = budget.rows(
        connection.execute(
            "SELECT id, time_created, time_updated, data "
            "FROM message WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        )
    )
    messages: list[_Message] = []
    messages_by_id: dict[str, _Message] = {}
    malformed = 0
    for sequence, row in enumerate(message_rows, 1):
        budget.json_field(row["data"])
        digest.update(str(tuple(row)).encode())
        message_id = _label(row["id"], 512)
        data = _json_object(row["data"])
        kind = _label(data.get("role"), 64) if data is not None else None
        if not message_id or data is None or kind not in {"user", "assistant"}:
            malformed += 1
            continue
        message = _Message(
            message_id,
            kind,
            sequence,
            _timestamp(row["time_created"]),
            _timestamp(row["time_updated"]),
            data,
            [],
        )
        messages.append(message)
        messages_by_id[message_id] = message

    part_rows = budget.rows(
        connection.execute(
            "SELECT id, message_id, time_created, time_updated, data "
            "FROM part WHERE session_id = ? ORDER BY message_id, id",
            (session_id,),
        )
    )
    for row in part_rows:
        budget.json_field(row["data"])
        digest.update(str(tuple(row)).encode())
        part_id = _label(row["id"], 512)
        message_id = _label(row["message_id"], 512)
        data = _json_object(row["data"])
        kind = _label(data.get("type"), 64) if data is not None else None
        message = messages_by_id.get(message_id or "")
        if not part_id or not message_id or data is None or not kind or message is None:
            malformed += 1
            continue
        message.parts.append(
            _Part(
                part_id,
                kind,
                _timestamp(row["time_created"]),
                _timestamp(row["time_updated"]),
                data,
            )
        )
    return messages, malformed


def _read_projection_messages(
    connection: sqlite3.Connection,
    session_id: object,
    budget: ProviderInputBudget,
    digest: Any,
) -> tuple[list[_Message], int]:
    rows = budget.rows(
        connection.execute(
            "SELECT id, type, seq, time_created, time_updated, data "
            "FROM session_message WHERE session_id = ? ORDER BY seq, id",
            (session_id,),
        )
    )
    messages: list[_Message] = []
    malformed = 0
    for row in rows:
        budget.json_field(row["data"])
        digest.update(str(tuple(row)).encode())
        message_id = _label(row["id"], 512)
        kind = _label(row["type"], 64)
        data = _json_object(row["data"])
        if not message_id or not kind or data is None:
            malformed += 1
            continue
        messages.append(
            _Message(
                message_id,
                kind,
                _counter(row["seq"]),
                _timestamp(row["time_created"]),
                _timestamp(row["time_updated"]),
                data,
                _projection_parts(message_id, data),
            )
        )
    return messages, malformed


def _projection_parts(message_id: str, data: dict[str, Any]) -> list[_Part]:
    content = data.get("content")
    if not isinstance(content, list):
        return []
    result: list[_Part] = []
    for sequence, value in enumerate(content, 1):
        if not isinstance(value, dict):
            continue
        kind = _label(value.get("type"), 64)
        if not kind:
            continue
        part_time = _mapping(value.get("time"))
        result.append(
            _Part(
                _label(value.get("id"), 512) or f"{message_id}:part:{sequence}",
                kind,
                _timestamp(part_time.get("created") or part_time.get("start")),
                _timestamp(part_time.get("completed") or part_time.get("end")),
                value,
            )
        )
    return result


def _json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str | bytes | bytearray):
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rank(value: _Conversation) -> tuple[int, datetime, str]:
    return (
        sum(1 + len(message.parts) for message in value.messages),
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
    reported = _counter(usage.get("total"))
    total = max(attributed, reported)
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
    model = _mapping(value)
    provider = _label(model.get("providerID"), 127)
    identifier = _label(model.get("id") or model.get("modelID"), 127)
    if provider and identifier:
        return f"{provider}/{identifier}"
    return identifier


def _message_model(data: dict[str, Any]) -> str | None:
    return _model(data.get("model")) or _model(
        {
            "providerID": data.get("providerID"),
            "modelID": data.get("modelID"),
        }
    )


def _finish_turn(turn: dict[str, Any], fallback: datetime | None) -> None:
    turn["ended_at"] = turn["ended_at"] or _iso(fallback)
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
