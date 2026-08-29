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

from cli_consumption.adapters._shared import reject_provider_file_symlink
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.models import Snapshot, empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")


@dataclass(slots=True)
class _Message:
    external_id: str
    role: str
    created_at: datetime | None
    starts_turn: bool
    has_error: bool
    tools: list[str]


@dataclass(slots=True)
class _Usage:
    external_id: str
    created_at: datetime | None
    model: str | None
    tokens: dict[str, int]
    is_compaction: bool


@dataclass(slots=True)
class _Conversation:
    machine: str
    external_id: str
    directory: str | None
    created_at: datetime | None
    updated_at: datetime | None
    provider: str | None
    configured_model: str | None
    messages: list[_Message]
    usage: list[_Usage]
    event_count: int
    digest: str


class GooseAdapter:
    """Read metadata from Goose's local SQLite v16 session store."""

    name = "goose"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected: dict[str, _Conversation] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            database = home / "sessions.db"
            if not database.is_file():
                raise ValueError(f"Missing Goose database: {database}")
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
        conversation_id = f"goose:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        assistant_seen: set[str] = set()
        errors: set[str] = set()
        tools: list[tuple[str | None, datetime | None, str]] = []

        for message in source.messages:
            if message.starts_turn:
                if active is not None:
                    _finish_turn(
                        turns[active], message.created_at, active in assistant_seen
                    )
                active = message.external_id
                turns[active] = {
                    "id": f"{conversation_id}:{active}",
                    "conversation_id": conversation_id,
                    "external_id": active,
                    "started_at": _iso(message.created_at),
                    "ended_at": None,
                    "status": "in-progress",
                    "duration_ms": None,
                    "time_to_first_token_ms": None,
                    "model_calls": 0,
                    "tool_calls": 0,
                    **empty_tokens(),
                }
                turn_models[active] = set()
            if active is not None and message.role == "assistant":
                assistant_seen.add(active)
                turns[active]["ended_at"] = _iso(message.created_at)
                if message.has_error:
                    errors.add(active)
            for name in message.tools:
                tools.append((active, message.created_at, name))

        if active is not None:
            _finish_turn(turns[active], source.updated_at, active in assistant_seen)
        for key in errors:
            turns[key]["status"] = "aborted"

        calls: list[tuple[str | None, _Usage, str]] = []
        compactions: list[tuple[str | None, _Usage]] = []
        turn_order = sorted(
            turns,
            key=lambda key: (
                _timestamp(turns[key]["started_at"]) or datetime.min.replace(tzinfo=UTC)
            ),
        )
        for usage in source.usage:
            turn_key = _turn_at(usage.created_at, turn_order, turns)
            model = _model(source.provider, usage.model or source.configured_model)
            calls.append((turn_key, usage, model or "unknown"))
            if usage.is_compaction:
                compactions.append((turn_key, usage))

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, (turn_key, usage, model) in enumerate(calls, 1):
            models.add(model)
            _add_tokens(totals, usage.tokens)
            turn = turns.get(turn_key or "")
            if turn:
                turn["model_calls"] += 1
                turn_models[turn_key or ""].add(model)
                _add_tokens(turn, usage.tokens)
            snapshot.model_calls.append(
                {
                    "id": f"{conversation_id}:model:{usage.external_id}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": _iso(usage.created_at),
                    "model": model,
                    **usage.tokens,
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

        for sequence, (turn_key, usage) in enumerate(compactions, 1):
            turn = turns.get(turn_key or "")
            snapshot.compaction_events.append(
                {
                    "id": f"{conversation_id}:compaction:{usage.external_id}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": _iso(usage.created_at),
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

        ended_at = source.updated_at or max(
            (
                item.created_at
                for item in [*source.messages, *source.usage]
                if item.created_at is not None
            ),
            default=None,
        )
        started_at = source.created_at or min(
            (
                item.created_at
                for item in [*source.messages, *source.usage]
                if item.created_at is not None
            ),
            default=None,
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
                "source": "local-sqlite-v16",
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


def _read_database(path: Path, machine: str) -> tuple[list[_Conversation], int]:
    try:
        reject_provider_file_symlink(path)
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        session_columns = _columns(connection, "sessions")
        message_columns = _columns(connection, "messages")
        usage_columns = _columns(connection, "usage_ledger")
        if (
            not {
                "id",
                "working_dir",
                "created_at",
                "updated_at",
                "provider_name",
                "model_config_json",
            }
            <= session_columns
            or not {
                "id",
                "message_id",
                "session_id",
                "role",
                "content_json",
                "created_timestamp",
                "metadata_json",
            }
            <= message_columns
            or not {
                "id",
                "session_id",
                "created_timestamp",
                "model",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "is_compaction",
            }
            <= usage_columns
        ):
            raise UnsupportedProviderFormat(
                f"Unsupported Goose database schema: {path}"
            )

        rows = connection.execute(
            "SELECT id, working_dir, created_at, updated_at, provider_name, "
            "model_config_json FROM sessions ORDER BY id"
        ).fetchall()
        conversations: list[_Conversation] = []
        malformed = 0
        for row in rows:
            external_id = _label(row["id"], 500)
            if not external_id:
                malformed += 1
                continue
            message_rows = connection.execute(
                "SELECT id, message_id, role, content_json, created_timestamp, "
                "metadata_json FROM messages WHERE session_id = ? "
                "ORDER BY created_timestamp, id",
                (row["id"],),
            ).fetchall()
            usage_rows = connection.execute(
                "SELECT id, created_timestamp, model, input_tokens, output_tokens, "
                "total_tokens, cache_read_tokens, cache_write_tokens, is_compaction "
                "FROM usage_ledger WHERE session_id = ? ORDER BY created_timestamp, id",
                (row["id"],),
            ).fetchall()
            digest = hashlib.sha256()
            messages: list[_Message] = []
            for message_row in message_rows:
                digest.update(str(tuple(message_row)).encode())
                message, invalid = _message(message_row)
                malformed += invalid
                if message is not None:
                    messages.append(message)

            usage: list[_Usage] = []
            for usage_row in usage_rows:
                digest.update(str(tuple(usage_row)).encode())
                external_usage_id = _label(str(usage_row["id"]), 512)
                if not external_usage_id:
                    malformed += 1
                    continue
                usage.append(
                    _Usage(
                        external_id=external_usage_id,
                        created_at=_timestamp(usage_row["created_timestamp"]),
                        model=_label(usage_row["model"], 255),
                        tokens=_usage(usage_row),
                        is_compaction=usage_row["is_compaction"] in (1, True),
                    )
                )

            configured_model, invalid = _configured_model(row["model_config_json"])
            malformed += invalid
            conversations.append(
                _Conversation(
                    machine=machine,
                    external_id=external_id,
                    directory=(
                        row["working_dir"]
                        if isinstance(row["working_dir"], str)
                        else None
                    ),
                    created_at=_timestamp(row["created_at"]),
                    updated_at=_timestamp(row["updated_at"]),
                    provider=_label(row["provider_name"], 127),
                    configured_model=configured_model,
                    messages=messages,
                    usage=usage,
                    event_count=len(messages) + len(usage),
                    digest=digest.hexdigest(),
                )
            )
        return conversations, malformed
    except sqlite3.DatabaseError:
        raise ValueError(f"Could not read Goose database: {path}") from None
    finally:
        if "connection" in locals():
            connection.close()


def _message(row: sqlite3.Row) -> tuple[_Message | None, int]:
    external_id = _label(row["message_id"], 512) or _label(str(row["id"]), 512)
    role = _label(row["role"], 32)
    try:
        content = json.loads(row["content_json"])
        metadata = json.loads(row["metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None, 1
    if (
        not external_id
        or role not in {"user", "assistant"}
        or not isinstance(content, list)
        or not isinstance(metadata, dict)
    ):
        return None, 1

    tools: list[str] = []
    has_prompt = False
    has_error = False
    invalid = 0
    for block in content:
        if not isinstance(block, dict):
            invalid += 1
            continue
        kind = block.get("type")
        if kind in {"text", "image"}:
            has_prompt = True
        elif kind in {"toolRequest", "frontendToolRequest"}:
            tool_call = block.get("toolCall")
            value = tool_call.get("value") if isinstance(tool_call, dict) else None
            name = value.get("name") if isinstance(value, dict) else None
            label = _label(name, 512)
            if label:
                tools.append(label)
            else:
                invalid += 1
        elif kind == "error":
            has_error = True

    user_visible = metadata.get("userVisible", True) is not False
    return (
        _Message(
            external_id=external_id,
            role=role,
            created_at=_timestamp(row["created_timestamp"]),
            starts_turn=role == "user" and user_visible and has_prompt,
            has_error=has_error,
            tools=tools,
        ),
        invalid,
    )


def _configured_model(value: object) -> tuple[str | None, int]:
    if value is None:
        return None, 0
    if not isinstance(value, str | bytes | bytearray):
        return None, 1
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None, 1
    if not isinstance(data, dict):
        return None, 1
    model = _label(data.get("model_name"), 255)
    return model, int(data.get("model_name") is not None and model is None)


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


def _usage(row: sqlite3.Row) -> dict[str, int]:
    input_tokens = _counter(row["input_tokens"])
    cached = min(input_tokens, _counter(row["cache_read_tokens"]))
    cache_write = min(
        max(0, input_tokens - cached), _counter(row["cache_write_tokens"])
    )
    output_tokens = _counter(row["output_tokens"])
    attributed = _sum(input_tokens, output_tokens)
    total = max(attributed, _counter(row["total_tokens"]))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "total_tokens": total,
        "uncached_input_tokens": max(0, input_tokens - cached - cache_write),
        "visible_output_tokens": output_tokens,
        "unattributed_tokens": max(0, total - attributed),
    }


def _model(provider: str | None, model: str | None) -> str | None:
    if not model:
        return None
    if provider and not model.startswith(provider + "/"):
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


def _turn_at(
    timestamp: datetime | None,
    order: list[str],
    turns: dict[str, dict[str, Any]],
) -> str | None:
    if not order:
        return None
    if timestamp is None:
        return order[-1]
    selected = None
    for key in order:
        started_at = _timestamp(turns[key]["started_at"])
        if started_at is not None and started_at <= timestamp:
            selected = key
        elif started_at is not None:
            break
    return selected


def _finish_turn(
    turn: dict[str, Any], fallback: datetime | None, completed: bool
) -> None:
    turn["ended_at"] = turn["ended_at"] or _iso(fallback)
    if completed:
        turn["status"] = "completed"
    start, end = _timestamp(turn["started_at"]), _timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float) and math.isfinite(value):
            divisor = 1000 if abs(value) >= 10_000_000_000 else 1
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
