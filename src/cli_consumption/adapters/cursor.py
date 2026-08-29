from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderDataLimitError,
    ProviderInputBudget,
    iter_bounded_jsonl_bytes,
    open_provider_sqlite,
)
from cli_consumption.models import Snapshot, empty_tokens

SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]*")
MAX_META_HEX_BYTES = 2_000_000


@dataclass(slots=True)
class _Record:
    line: int
    role: str
    visible_user: bool
    tool_names: list[str]


@dataclass(slots=True)
class _Meta:
    external_id: str
    created_at: datetime | None
    last_used_model: str | None
    workspace_hash: str
    modified_at: datetime | None


@dataclass(slots=True)
class _Conversation:
    machine: str
    external_id: str
    project_slug: str | None
    records: list[_Record]
    meta: _Meta | None
    event_count: int
    digest: str
    modified_at: datetime | None


class CursorAdapter:
    """Read Cursor CLI metadata while discarding transcript content."""

    name = "cursor"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Conversation] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            projects = home / "projects"
            chats = home / "chats"
            if not projects.is_dir() and not chats.is_dir():
                raise ValueError(
                    f"Missing Cursor CLI projects or chats directory under: {home}"
                )

            metas, invalid = _read_metas(chats, budget)
            malformed += invalid
            seen: set[str] = set()
            if projects.is_dir():
                pattern = "*/agent-transcripts/*/*.jsonl"
                for path in budget.sorted_paths(projects.glob(pattern)):
                    candidate, invalid = _read_transcript(path, machine, metas, budget)
                    malformed += invalid
                    if candidate is None:
                        continue
                    seen.add(candidate.external_id)
                    duplicates += _select(selected, candidate)

            for external_id, meta in metas.items():
                if external_id in seen:
                    continue
                digest = _digest_meta(meta)
                candidate = _Conversation(
                    machine=machine,
                    external_id=external_id,
                    project_slug=None,
                    records=[],
                    meta=meta,
                    event_count=0,
                    digest=digest,
                    modified_at=meta.modified_at,
                )
                duplicates += _select(selected, candidate)

        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=malformed,
        )
        for conversation in sorted(
            selected.values(), key=lambda value: value.external_id
        ):
            self._normalize(snapshot, conversation, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Conversation,
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"cursor:{source.external_id}"
        turns: dict[str, dict[str, Any]] = {}
        active: str | None = None
        calls: list[str | None] = []
        tools: list[tuple[str | None, str]] = []

        for record in source.records:
            if record.role == "user" and record.visible_user:
                if active is not None:
                    _finish_turn(turns[active])
                active = f"turn-{record.line}"
                turns[active] = {
                    "id": f"{conversation_id}:{active}",
                    "conversation_id": conversation_id,
                    "external_id": active,
                    "started_at": None,
                    "ended_at": None,
                    "status": "in-progress",
                    "duration_ms": None,
                    "time_to_first_token_ms": None,
                    "model_calls": 0,
                    "tool_calls": 0,
                    **empty_tokens(),
                }
                continue

            if record.role != "assistant":
                continue
            calls.append(active)
            turn = turns.get(active or "")
            if turn:
                turn["model_calls"] += 1
            tools.extend((active, name) for name in record.tool_names)

        if active is not None:
            _finish_turn(turns[active])

        for sequence, turn_key in enumerate(calls, 1):
            turn = turns.get(turn_key or "")
            snapshot.model_calls.append(
                {
                    "id": f"{conversation_id}:model:{sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"] if turn else None,
                    "sequence": sequence,
                    "timestamp": None,
                    "model": "unknown",
                    **empty_tokens(),
                }
            )

        for sequence, (turn_key, name) in enumerate(tools, 1):
            turn = turns.get(turn_key or "")
            if turn:
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

        snapshot.turns.extend(turns.values())
        started_at = source.meta.created_at if source.meta else None
        ended_at = source.modified_at
        project, project_source = _project(source, mappings)
        last_model = source.meta.last_used_model if source.meta else None
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
                "source": (
                    "local-jsonl-composer-2" if source.records else "local-sqlite-meta"
                ),
                "models": [last_model] if last_model else [],
                "iterations": len(turns),
                "model_calls": len(calls),
                "tool_calls": len(tools),
                "compactions": 0,
                "event_count": source.event_count,
                "content_hash": source.digest,
                **empty_tokens(),
            }
        )


def _select(selected: dict[str, _Conversation], candidate: _Conversation) -> int:
    previous = selected.get(candidate.external_id)
    if previous is None:
        selected[candidate.external_id] = candidate
        return 0
    if _rank(candidate) > _rank(previous):
        selected[candidate.external_id] = candidate
    return 1


def _rank(value: _Conversation) -> tuple[int, int, str]:
    return value.event_count, int(value.meta is not None), value.digest


def _read_transcript(
    path: Path,
    machine: str,
    metas: dict[str, _Meta],
    budget: ProviderInputBudget,
) -> tuple[_Conversation | None, int]:
    external_id = _label(path.stem, 512)
    if external_id is None or path.parent.name != path.stem:
        return None, 1

    digest = hashlib.sha256()
    records: list[_Record] = []
    malformed = 0
    for line_number, raw_line in enumerate(iter_bounded_jsonl_bytes(path, budget), 1):
        digest.update(raw_line)
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        record, invalid = _record(value, line_number)
        malformed += invalid
        if record is not None:
            records.append(record)

    if not records:
        return None, malformed + 1
    meta = metas.get(external_id)
    if meta is not None:
        digest.update(_digest_meta(meta).encode())
    modified_at = _modified_at(path)
    project_slug = path.parents[2].name
    return (
        _Conversation(
            machine=machine,
            external_id=external_id,
            project_slug=project_slug,
            records=records,
            meta=meta,
            event_count=len(records),
            digest=digest.hexdigest(),
            modified_at=modified_at or (meta.modified_at if meta else None),
        ),
        malformed,
    )


def _record(value: object, line_number: int) -> tuple[_Record | None, int]:
    if not isinstance(value, dict):
        return None, 1
    role = value.get("role")
    if role not in {"user", "assistant"}:
        return None, 0
    message = value.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return None, 1

    visible_user = False
    tool_names: list[str] = []
    malformed = 0
    for block in message["content"]:
        if not isinstance(block, dict):
            malformed += 1
            continue
        kind = block.get("type")
        if role == "user" and kind == "text":
            text = block.get("text")
            if not isinstance(text, str):
                malformed += 1
            elif "<user_query>" in text or not text.lstrip().startswith("<"):
                visible_user = True
        if role == "assistant" and kind in {
            "tool_use",
            "tool-use",
            "tool_call",
            "tool-call",
        }:
            name = _label(block.get("name") or block.get("tool"), 512)
            if name is None:
                malformed += 1
            else:
                tool_names.append(name)
    return _Record(line_number, role, visible_user, tool_names), malformed


def _read_metas(
    chats: Path, budget: ProviderInputBudget
) -> tuple[dict[str, _Meta], int]:
    if not chats.is_dir():
        return {}, 0
    result: dict[str, _Meta] = {}
    malformed = 0
    for path in budget.sorted_paths(chats.glob("*/*/store.db")):
        meta = _read_meta(path, budget)
        if meta is None:
            malformed += 1
            continue
        previous = result.get(meta.external_id)
        if previous is None or _digest_meta(meta) > _digest_meta(previous):
            result[meta.external_id] = meta
    return result, malformed


def _read_meta(path: Path, budget: ProviderInputBudget) -> _Meta | None:
    try:
        manager = open_provider_sqlite(path, budget)
        connection = manager.__enter__()
        try:
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(meta)")
            }
            if not {"key", "value"}.issubset(columns):
                return None
            row = connection.execute(
                "SELECT value FROM meta WHERE key = ? LIMIT 1", ("0",)
            ).fetchone()
        finally:
            manager.__exit__(None, None, None)
    except sqlite3.DataError:
        raise ProviderDataLimitError("provider_sqlite_field_too_large") from None
    except (OSError, sqlite3.DatabaseError):
        return None
    if row is None or not isinstance(row[0], str):
        return None
    raw = budget.json_field(row[0])
    if len(raw) > MAX_META_HEX_BYTES * 2:
        return None
    try:
        decoded = bytes.fromhex(raw)
        value = json.loads(decoded)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    external_id = _label(value.get("agentId"), 512)
    if external_id is None:
        return None
    return _Meta(
        external_id=external_id,
        created_at=_timestamp(value.get("createdAt")),
        last_used_model=_model(value.get("lastUsedModel")),
        workspace_hash=path.parents[1].name,
        modified_at=_modified_at(path),
    )


def _digest_meta(meta: _Meta) -> str:
    payload = {
        "agent_id": meta.external_id,
        "created_at": _iso(meta.created_at),
        "last_used_model": meta.last_used_model,
        "modified_at": _iso(meta.modified_at),
        "workspace_hash": meta.workspace_hash,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _project(source: _Conversation, mappings: list[tuple[str, str]]) -> tuple[str, str]:
    workspace_hash = source.meta.workspace_hash if source.meta else None
    for name, prefix in sorted(mappings, key=lambda item: len(item[1]), reverse=True):
        normalized = prefix.replace("\\", "/").rstrip("/")
        digest = hashlib.md5(normalized.encode(), usedforsecurity=False).hexdigest()
        slug = normalized.lstrip("/").replace("/", "-")
        if workspace_hash == digest or source.project_slug == slug:
            return name, "mapping"
    return "outside-project", "none"


def _finish_turn(turn: dict[str, Any]) -> None:
    if turn["model_calls"]:
        turn["status"] = "completed"


def _modified_at(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return datetime.fromtimestamp(float(value) / 1000, UTC)
    except (OSError, OverflowError, ValueError):
        pass
    return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _model(value: object) -> str | None:
    label = _label(value, 255)
    return label if label and label != "default" else None


def _label(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= maximum and SAFE_LABEL.fullmatch(value) else None
