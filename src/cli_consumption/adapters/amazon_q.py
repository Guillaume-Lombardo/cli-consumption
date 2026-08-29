from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    digest_records,
    finish_turn,
    iso,
    label,
    mapping,
    new_turn,
    project,
    reject_provider_file_symlink,
    timestamp,
)
from cli_consumption.adapters.base import UnsupportedProviderFormat
from cli_consumption.models import Snapshot, empty_tokens


class AmazonQAdapter:
    """Read metadata from Amazon Q Developer CLI persistent conversations."""

    name = "amazon-q"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        selected: dict[str, tuple[str, str, dict[str, Any]]] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            database = home / "data.sqlite3"
            if not database.is_file():
                raise ValueError(f"Missing Amazon Q Developer CLI database: {database}")
            rows, invalid = _read_database(database)
            malformed += invalid
            for directory, state in rows:
                external_id = label(state.get("conversation_id"))
                if not external_id:
                    malformed += 1
                    continue
                candidate = (machine, directory, state)
                if external_id in selected:
                    duplicates += 1
                    if _rank(candidate) <= _rank(selected[external_id]):
                        continue
                selected[external_id] = candidate
        snapshot = Snapshot(
            provider=self.name,
            malformed_records=malformed,
            duplicate_conversations=duplicates,
        )
        for external_id, value in sorted(selected.items()):
            _normalize(snapshot, external_id, *value, project_mappings or [])
        return snapshot


def _read_database(path: Path) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    connection: sqlite3.Connection | None = None
    malformed = 0
    result: list[tuple[str, dict[str, Any]]] = []
    try:
        reject_provider_file_symlink(path)
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        columns = {
            str(row["name"])
            for row in connection.execute('PRAGMA table_info("conversations")')
        }
        if not {"key", "value"} <= columns:
            raise UnsupportedProviderFormat(
                f"Unsupported Amazon Q Developer CLI database schema: {path}"
            )
        for row in connection.execute(
            "SELECT key, value FROM conversations ORDER BY key"
        ):
            try:
                value = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(row["key"], str) or not isinstance(value, dict):
                malformed += 1
                continue
            result.append((row["key"], value))
        return result, malformed
    except sqlite3.DatabaseError:
        raise ValueError(
            f"Could not read Amazon Q Developer CLI database: {path}"
        ) from None
    finally:
        if connection is not None:
            connection.close()


def _history(state: dict[str, Any]) -> list[dict[str, Any]]:
    value = state.get("history")
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _rank(value: tuple[str, str, dict[str, Any]]) -> tuple[int, str]:
    return len(_history(value[2])), digest_records(value[2])


def _normalize(
    snapshot: Snapshot,
    external_id: str,
    machine: str,
    directory: str,
    state: dict[str, Any],
    mappings: list[tuple[str, str]],
) -> None:
    conversation_id = f"amazon-q:{external_id}"
    turns: list[dict[str, Any]] = []
    models: set[str] = set()
    active: dict[str, Any] | None = None
    tool_sequence = model_sequence = 0
    for entry_sequence, entry in enumerate(_history(state), 1):
        user = mapping(entry.get("user"))
        assistant = mapping(entry.get("assistant"))
        metadata = mapping(entry.get("request_metadata"))
        started = timestamp(
            user.get("timestamp") or metadata.get("request_start_timestamp_ms")
        )
        ended = timestamp(metadata.get("stream_end_timestamp_ms"))
        if _is_prompt(user.get("content")):
            if active:
                finish_turn(active, started)
            active = new_turn(
                conversation_id,
                label(metadata.get("message_id")) or f"turn-{entry_sequence}",
                started,
            )
            turns.append(active)
        model = label(metadata.get("model_id"), 255)
        if model:
            models.add(model)
        if assistant:
            model_sequence += 1
            if active:
                active["model_calls"] += 1
                active["ended_at"] = iso(ended) or active["ended_at"]
            snapshot.model_calls.append(
                {
                    "id": f"{conversation_id}:model:{model_sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": active["id"] if active else None,
                    "sequence": model_sequence,
                    "timestamp": iso(started),
                    "model": model or "unknown",
                    **empty_tokens(),
                }
            )
        tools = metadata.get("tool_use_ids_and_names")
        if isinstance(tools, list):
            for item in tools:
                name = (
                    label(item[1])
                    if isinstance(item, list | tuple) and len(item) == 2
                    else None
                )
                if not name:
                    continue
                tool_sequence += 1
                if active:
                    active["tool_calls"] += 1
                snapshot.tool_calls.append(
                    {
                        "id": f"{conversation_id}:tool:{tool_sequence}",
                        "conversation_id": conversation_id,
                        "turn_id": active["id"] if active else None,
                        "sequence": tool_sequence,
                        "timestamp": iso(ended),
                        "tool_name": name,
                        "outer_tool_name": name,
                    }
                )
    if active:
        finish_turn(active, timestamp(active["ended_at"]))
    snapshot.turns.extend(turns)
    started = timestamp(turns[0]["started_at"]) if turns else None
    ended = timestamp(turns[-1]["ended_at"]) if turns else None
    project_name, project_source = project(directory, mappings)
    snapshot.conversations.append(
        {
            "id": conversation_id,
            "provider": "amazon-q",
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
            "models": sorted(models),
            "iterations": len(turns),
            "model_calls": model_sequence,
            "tool_calls": tool_sequence,
            "compactions": 0,
            "event_count": len(_history(state)),
            "content_hash": digest_records(state),
            **empty_tokens(),
        }
    )


def _is_prompt(content: object) -> bool:
    return isinstance(content, dict) and "Prompt" in content
