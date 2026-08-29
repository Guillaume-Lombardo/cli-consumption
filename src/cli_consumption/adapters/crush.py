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
    read_json,
)
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]*")
ABORT_REASONS = {"canceled", "content_filter", "error"}


@dataclass(slots=True)
class _Message:
    external_id: str
    role: str
    created_at: datetime | None
    updated_at: datetime | None
    finished_at: datetime | None
    model: str | None
    provider: str | None
    tool_names: list[str]
    finish_reason: str | None
    is_summary: bool


@dataclass(slots=True)
class _Conversation:
    machine: str
    external_id: str
    directory: str | None
    created_at: datetime | None
    updated_at: datetime | None
    prompt_tokens: int
    completion_tokens: int
    messages: list[_Message]
    digest: str


class CrushAdapter:
    """Read metadata from Crush's per-project SQLite stores."""

    name = "crush"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected: dict[str, _Conversation] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            databases, invalid = _discover_databases(home)
            malformed += invalid
            if not databases:
                raise ValueError(f"No readable Crush databases found from: {home}")
            for database, directory in databases:
                conversations, invalid = _read_database(database, machine, directory)
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
        conversation_id = f"crush:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        calls: list[tuple[str | None, datetime | None, str]] = []
        tools: list[tuple[str | None, datetime | None, str]] = []
        compactions: list[tuple[str | None, datetime | None]] = []
        timestamps = [source.created_at, source.updated_at]

        for message in source.messages:
            timestamp = message.created_at
            completed_at = message.finished_at or message.updated_at
            timestamps.extend((timestamp, completed_at))

            if message.is_summary:
                compactions.append((active, timestamp))
                continue

            if message.role == "user":
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

            if message.role != "assistant":
                continue

            model = _model(message.provider, message.model) or "unknown"
            calls.append((active, timestamp, model))
            turn = turns.get(active or "")
            if turn:
                turn["model_calls"] += 1
                turn_models[active or ""].add(model)
                turn["ended_at"] = _iso(completed_at or timestamp)
                if message.finish_reason in ABORT_REASONS:
                    turn["status"] = "aborted"
                elif message.finish_reason or message.finished_at is not None:
                    turn["status"] = "completed"

            for name in message.tool_names:
                tools.append((active, timestamp, name))

        ended_at = max(
            (value for value in timestamps if value is not None), default=None
        )
        if active is not None:
            _finish_turn(turns[active], ended_at)

        usage = _usage(source.prompt_tokens, source.completion_tokens)
        if not calls and usage["total_tokens"]:
            calls.append((None, source.updated_at, "unknown"))

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (turn_key, timestamp, model) in enumerate(calls, 1):
            models.add(model)
            tokens = usage if sequence == len(calls) else empty_tokens()
            _add_tokens(totals, tokens)
            turn = turns.get(turn_key or "")
            if turn:
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
                "source": "local-sqlite-v0.91",
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


def _discover_databases(home: Path) -> tuple[list[tuple[Path, str | None]], int]:
    direct = home / "crush.db"
    if direct.is_file():
        directory = str(home.parent) if home.name == ".crush" else None
        return [(direct, directory)], 0

    nested = home / ".crush" / "crush.db"
    if nested.is_file():
        return [(nested, str(home))], 0

    registry = home / "projects.json"
    if not registry.is_file():
        return [], 0
    try:
        value = read_json(registry)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"Could not read Crush project registry: {registry}") from None

    entries = _registry_entries(value)
    discovered: dict[Path, str | None] = {}
    malformed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            malformed += 1
            continue
        project = entry.get("path")
        data_dir = entry.get("data_dir")
        if not isinstance(project, str) or not isinstance(data_dir, str):
            malformed += 1
            continue
        base = Path(project).expanduser()
        data = Path(data_dir).expanduser()
        if not data.is_absolute():
            data = base / data
        database = (data / "crush.db").resolve()
        if database in discovered:
            if discovered[database] != project:
                discovered[database] = None
            continue
        if not database.is_file():
            malformed += 1
            continue
        discovered[database] = project
    return list(discovered.items()), malformed


def _registry_entries(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    projects = value.get("projects")
    if isinstance(projects, list):
        return projects
    if all(isinstance(entry, dict) for entry in value.values()):
        return list(value.values())
    return []


def _read_database(
    path: Path, machine: str, directory: str | None
) -> tuple[list[_Conversation], int]:
    try:
        check_provider_sqlite_file(path)
        budget = ProviderInputBudget()
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        session_columns = _columns(connection, "sessions")
        message_columns = _columns(connection, "messages")
        required_session = {
            "id",
            "parent_session_id",
            "prompt_tokens",
            "completion_tokens",
            "created_at",
            "updated_at",
        }
        required_message = {
            "id",
            "session_id",
            "role",
            "parts",
            "model",
            "created_at",
            "updated_at",
        }
        if (
            not required_session <= session_columns
            or not required_message <= message_columns
        ):
            raise UnsupportedProviderFormat(
                f"Unsupported Crush database schema: {path}"
            )

        finished_at = "finished_at" if "finished_at" in message_columns else "NULL"
        provider = "provider" if "provider" in message_columns else "NULL"
        summary = (
            "is_summary_message" if "is_summary_message" in message_columns else "0"
        )
        rows = budget.rows(
            connection.execute(
                "SELECT id, prompt_tokens, completion_tokens, created_at, updated_at "
                "FROM sessions WHERE parent_session_id IS NULL ORDER BY id"
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
                    f"SELECT id, role, parts, model, {provider} AS provider, "
                    f"created_at, updated_at, {finished_at} AS finished_at, "
                    f"{summary} AS is_summary_message FROM messages "
                    "WHERE session_id = ? ORDER BY created_at, rowid",
                    (row["id"],),
                )
            )
            digest = hashlib.sha256()
            digest.update(str(tuple(row)).encode())
            messages: list[_Message] = []
            for message_row in message_rows:
                budget.json_field(message_row["parts"])
                digest.update(str(tuple(message_row)).encode())
                message, invalid = _parse_message(message_row)
                malformed += invalid
                if message is not None:
                    messages.append(message)
            conversations.append(
                _Conversation(
                    machine=machine,
                    external_id=external_id,
                    directory=directory,
                    created_at=_timestamp(row["created_at"]),
                    updated_at=_timestamp(row["updated_at"]),
                    prompt_tokens=_counter(row["prompt_tokens"]),
                    completion_tokens=_counter(row["completion_tokens"]),
                    messages=messages,
                    digest=digest.hexdigest(),
                )
            )
        return conversations, malformed
    except sqlite3.DatabaseError:
        raise ValueError(f"Could not read Crush database: {path}") from None
    finally:
        if "connection" in locals():
            connection.close()


def _parse_message(row: sqlite3.Row) -> tuple[_Message | None, int]:
    external_id = _label(row["id"], 500)
    role = _label(row["role"], 32)
    if not external_id or role not in {"assistant", "system", "tool", "user"}:
        return None, 1
    try:
        parts = json.loads(row["parts"])
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None, 1
    if not isinstance(parts, list):
        return None, 1

    tool_names: list[str] = []
    finish_reason: str | None = None
    malformed = 0
    for part in parts:
        if not isinstance(part, dict):
            malformed += 1
            continue
        kind = part.get("type")
        data = part.get("data")
        if kind == "tool_call":
            if not isinstance(data, dict):
                malformed += 1
                continue
            name = _label(data.get("name"), 512)
            if name:
                tool_names.append(name)
            else:
                malformed += 1
        elif kind == "finish":
            if not isinstance(data, dict):
                malformed += 1
                continue
            finish_reason = _label(data.get("reason"), 64)

    return (
        _Message(
            external_id=external_id,
            role=role,
            created_at=_timestamp(row["created_at"]),
            updated_at=_timestamp(row["updated_at"]),
            finished_at=_timestamp(row["finished_at"]),
            model=_label(row["model"], 127),
            provider=_label(row["provider"], 127),
            tool_names=tool_names,
            finish_reason=finish_reason,
            is_summary=bool(_counter(row["is_summary_message"])),
        ),
        malformed,
    )


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


def _usage(prompt: int, completion: int) -> dict[str, int]:
    total = _sum(prompt, completion)
    return {
        "input_tokens": prompt,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": completion,
        "reasoning_output_tokens": 0,
        "total_tokens": total,
        "uncached_input_tokens": prompt,
        "visible_output_tokens": completion,
        "unattributed_tokens": 0,
    }


def _model(provider: str | None, model: str | None) -> str | None:
    if provider and model and not model.startswith(provider + "/"):
        return f"{provider}/{model}"
    return model


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


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float) and math.isfinite(value):
            divisor = 1000 if abs(value) >= 100_000_000_000 else 1
            return datetime.fromtimestamp(value / divisor, UTC)
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
