from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderInputBudget,
    add_tokens,
    check_provider_sqlite_file,
    digest_records,
    finish_turn,
    iso,
    label,
    mapping,
    new_turn,
    project,
    read_json,
    timestamp,
    tokens,
)
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.models import Snapshot, empty_tokens


class ClineAdapter:
    """Read metadata from Cline CLI's SQLite index and message artifacts."""

    name = "cline"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected: dict[str, tuple[str, dict[str, Any], list[dict[str, Any]]]] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            database = home / "sessions" / "sessions.db"
            if not database.is_file():
                raise ValueError(f"Missing Cline CLI database: {database}")
            rows, invalid = _read_database(database, home)
            malformed += invalid
            for row, messages in rows:
                key = str(row["session_id"])
                candidate = (machine, row, messages)
                if key in selected:
                    duplicates += 1
                    if _rank(candidate) <= _rank(selected[key]):
                        continue
                selected[key] = candidate

        snapshot = Snapshot(
            provider=self.name,
            malformed_records=malformed,
            duplicate_conversations=duplicates,
        )
        for machine, row, messages in sorted(
            selected.values(), key=lambda item: str(item[1]["session_id"])
        ):
            _normalize(snapshot, machine, row, messages, project_mappings or [])
        return snapshot


def _read_database(
    path: Path, home: Path
) -> tuple[list[tuple[dict[str, Any], list[dict[str, Any]]]], int]:
    connection: sqlite3.Connection | None = None
    malformed = 0
    try:
        check_provider_sqlite_file(path)
        budget = ProviderInputBudget()
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        columns = {
            str(row["name"])
            for row in connection.execute('PRAGMA table_info("sessions")')
        }
        required = {
            "session_id",
            "started_at",
            "status",
            "provider",
            "model",
            "cwd",
            "metadata_json",
        }
        if not required <= columns:
            raise UnsupportedProviderFormat(
                f"Unsupported Cline CLI database schema: {path}"
            )
        optional = [
            name
            for name in ("ended_at", "updated_at", "workspace_root", "messages_path")
            if name in columns
        ]
        rows = budget.rows(
            connection.execute(
                "SELECT "
                + ", ".join(sorted(required | set(optional)))
                + " FROM sessions ORDER BY session_id"
            )
        )
        result: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for raw in rows:
            row = dict(raw)
            if "metadata_json" in row:
                budget.json_field(row["metadata_json"])
            if not label(row.get("session_id")):
                malformed += 1
                continue
            messages_path = row.get("messages_path")
            candidates: list[Path] = []
            if isinstance(messages_path, str) and messages_path:
                configured = Path(messages_path)
                try:
                    if configured.resolve().is_relative_to(
                        (home / "sessions").resolve()
                    ):
                        candidates.append(configured)
                except OSError:
                    pass
            session_id = str(row["session_id"])
            candidates.append(
                home / "sessions" / session_id / f"{session_id}.messages.json"
            )
            messages: list[dict[str, Any]] = []
            artifact = next(
                (candidate for candidate in candidates if candidate.is_file()), None
            )
            if artifact:
                try:
                    value = read_json(artifact)
                    raw_messages = mapping(value).get("messages")
                    if isinstance(raw_messages, list):
                        messages = [
                            item for item in raw_messages if isinstance(item, dict)
                        ]
                        malformed += len(raw_messages) - len(messages)
                    else:
                        malformed += 1
                except (OSError, ValueError, UnicodeDecodeError):
                    malformed += 1
            result.append((row, messages))
        return result, malformed
    except sqlite3.DatabaseError:
        raise ValueError(f"Could not read Cline CLI database: {path}") from None
    finally:
        if connection is not None:
            connection.close()


def _rank(
    value: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> tuple[int, str, str]:
    _, row, messages = value
    return (
        len(messages),
        str(row.get("updated_at") or row.get("ended_at") or ""),
        digest_records(messages),
    )


def _usage(value: object) -> dict[str, int]:
    metrics = mapping(value)
    cached = metrics.get("cacheReadTokens", 0)
    cache_write = metrics.get("cacheWriteTokens", 0)
    reported_input = metrics.get("inputTokens", 0)
    uncached = (
        max(0, int(reported_input or 0) - int(cached or 0) - int(cache_write or 0))
        if all(
            isinstance(v, int) and not isinstance(v, bool)
            for v in (reported_input, cached, cache_write)
        )
        else 0
    )
    return tokens(
        uncached=uncached,
        cached=cached,
        cache_write=cache_write,
        visible=metrics.get("outputTokens", 0),
    )


def _normalize(
    snapshot: Snapshot,
    machine: str,
    row: dict[str, Any],
    messages: list[dict[str, Any]],
    mappings: list[tuple[str, str]],
) -> None:
    external_id = str(row["session_id"])
    conversation_id = f"cline:{external_id}"
    turns: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    model_calls: list[tuple[dict[str, Any] | None, Any, str, dict[str, int]]] = []
    tool_calls: list[tuple[dict[str, Any] | None, Any, str]] = []
    configured_model = label(row.get("model"), 255) or "unknown"

    for index, message in enumerate(messages, 1):
        role = label(message.get("role"), 32)
        message_time = timestamp(message.get("ts"))
        if role == "user" and _is_visible_user(message.get("content")):
            if active:
                finish_turn(active, message_time)
            active = new_turn(
                conversation_id,
                label(message.get("id")) or f"turn-{index}",
                message_time,
            )
            turns.append(active)
        elif role == "assistant":
            usage = _usage(message.get("metrics"))
            call_model = (
                label(mapping(message.get("modelInfo")).get("id"), 255)
                or configured_model
            )
            model_calls.append((active, message_time, call_model, usage))
            if active:
                active["model_calls"] += 1
                add_tokens(active, usage)
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                part_map = mapping(part)
                if part_map.get("type") == "tool_use":
                    name = label(part_map.get("name"))
                    if name:
                        tool_calls.append((active, message_time, name))
                        if active:
                            active["tool_calls"] += 1
    if active:
        finish_turn(active, timestamp(row.get("ended_at")))

    totals = empty_tokens()
    observed_models: set[str] = set()
    for sequence, (turn, call_time, call_model, usage) in enumerate(model_calls, 1):
        observed_models.add(call_model)
        add_tokens(totals, usage)
        snapshot.model_calls.append(
            {
                "id": f"{conversation_id}:model:{sequence}",
                "conversation_id": conversation_id,
                "turn_id": turn["id"] if turn else None,
                "sequence": sequence,
                "timestamp": iso(call_time),
                "model": call_model,
                **usage,
            }
        )
    for sequence, (turn, call_time, name) in enumerate(tool_calls, 1):
        snapshot.tool_calls.append(
            {
                "id": f"{conversation_id}:tool:{sequence}",
                "conversation_id": conversation_id,
                "turn_id": turn["id"] if turn else None,
                "sequence": sequence,
                "timestamp": iso(call_time),
                "tool_name": name,
                "outer_tool_name": name,
            }
        )
    snapshot.turns.extend(turns)
    directory = row.get("workspace_root") or row.get("cwd")
    project_name, project_source = project(
        directory if isinstance(directory, str) else None, mappings
    )
    started, ended = timestamp(row.get("started_at")), timestamp(row.get("ended_at"))
    snapshot.conversations.append(
        {
            "id": conversation_id,
            "provider": "cline",
            "external_id": external_id,
            "source_machine": machine,
            "project": project_name,
            "project_source": project_source,
            "started_at": iso(started),
            "ended_at": iso(ended),
            "duration_seconds": max(0.0, (ended - started).total_seconds())
            if started and ended
            else None,
            "source": "local-sqlite-v1",
            "models": sorted(observed_models or {configured_model}),
            "iterations": len(turns),
            "model_calls": len(model_calls),
            "tool_calls": len(tool_calls),
            "compactions": 0,
            "event_count": len(messages),
            "content_hash": digest_records([row, messages]),
            **totals,
        }
    )


def _is_visible_user(content: object) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    return any(
        mapping(part).get("type") == "text"
        and isinstance(mapping(part).get("text"), str)
        and bool(mapping(part)["text"].strip())
        for part in content
    )
