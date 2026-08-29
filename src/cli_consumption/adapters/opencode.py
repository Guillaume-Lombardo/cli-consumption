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
    ProviderInputBudget,
    check_provider_sqlite_file,
)
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")


@dataclass(slots=True)
class _Message:
    external_id: str
    kind: str
    sequence: int
    created_at: datetime | None
    updated_at: datetime | None
    data: dict[str, Any]


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
        selected: dict[str, _Conversation] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            database = home / "opencode.db"
            if not database.is_file():
                raise ValueError(f"Missing OpenCode database: {database}")
            conversations, invalid = _read_database(database, machine)
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
                continue

            if message.kind == "model-switched":
                effective_model = _model(message.data.get("model"))
                continue

            if message.kind == "compaction":
                compactions.append((active, timestamp))
                continue

            if message.kind != "assistant":
                continue

            model = _model(message.data.get("model")) or effective_model or "unknown"
            effective_model = model if model != "unknown" else effective_model
            tokens = _usage(message.data.get("tokens"))
            calls.append((active, timestamp, model, tokens))
            turn = turns.get(active or "")
            if turn:
                turn["model_calls"] += 1
                turn_models[active or ""].add(model)
                _add_tokens(turn, tokens)
                turn["ended_at"] = _iso(completed_at or timestamp) or turn["ended_at"]
                if isinstance(message.data.get("error"), dict):
                    turn["status"] = "aborted"
                elif (
                    completed_at is not None or _label(message.data.get("finish"), 255)
                ) and turn["status"] != "aborted":
                    turn["status"] = "completed"

            content = message.data.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool":
                    continue
                name = _label(part.get("name"), 512)
                if not name:
                    continue
                part_time = _mapping(part.get("time"))
                tools.append(
                    (active, _timestamp(part_time.get("created")) or timestamp, name)
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


def _read_database(path: Path, machine: str) -> tuple[list[_Conversation], int]:
    try:
        check_provider_sqlite_file(path)
        budget = ProviderInputBudget()
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        session_columns = _columns(connection, "session")
        message_columns = _columns(connection, "session_message")
        required_session = {"id", "time_created", "time_updated"}
        required_message = {
            "id",
            "session_id",
            "type",
            "seq",
            "time_created",
            "time_updated",
            "data",
        }
        if (
            not required_session <= session_columns
            or not required_message <= message_columns
        ):
            raise UnsupportedProviderFormat(
                f"Unsupported OpenCode database schema: {path}"
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
                    "SELECT id, type, seq, time_created, time_updated, data "
                    "FROM session_message WHERE session_id = ? ORDER BY seq, id",
                    (row["id"],),
                )
            )
            digest = hashlib.sha256()
            messages: list[_Message] = []
            for message_row in message_rows:
                budget.json_field(message_row["data"])
                digest.update(str(tuple(message_row)).encode())
                message_id = _label(message_row["id"], 512)
                kind = _label(message_row["type"], 64)
                try:
                    data = json.loads(message_row["data"])
                except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                    malformed += 1
                    continue
                if not message_id or not kind or not isinstance(data, dict):
                    malformed += 1
                    continue
                sequence = _counter(message_row["seq"])
                messages.append(
                    _Message(
                        message_id,
                        kind,
                        sequence,
                        _timestamp(message_row["time_created"]),
                        _timestamp(message_row["time_updated"]),
                        data,
                    )
                )
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
            connection.close()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _rank(value: _Conversation) -> tuple[int, datetime, str]:
    return (
        len(value.messages),
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
