from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    add_tokens,
    digest_records,
    finish_turn,
    iso,
    label,
    new_turn,
    timestamp,
    tokens,
)
from cli_consumption.models import Snapshot, empty_tokens


class PlandexAdapter:
    """Read metadata from a copied self-hosted Plandex server data directory."""

    name = "plandex"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        del project_mappings
        selected: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            directories = sorted((home / "orgs").glob("*/plans/*/conversation"))
            if not directories:
                raise ValueError(
                    f"Missing Plandex server conversations under: {home / 'orgs'}"
                )
            for directory in directories:
                messages, invalid = _read_messages(directory)
                malformed += invalid
                if not messages:
                    continue
                plan_id = label(messages[0].get("planId")) or label(
                    directory.parent.name
                )
                if not plan_id:
                    malformed += 1
                    continue
                candidate = (machine, messages)
                if plan_id in selected:
                    duplicates += 1
                    if _rank(candidate) <= _rank(selected[plan_id]):
                        continue
                selected[plan_id] = candidate
        snapshot = Snapshot(
            provider=self.name,
            malformed_records=malformed,
            duplicate_conversations=duplicates,
        )
        for plan_id, (machine, messages) in sorted(selected.items()):
            _normalize(snapshot, machine, plan_id, messages)
        return snapshot


def _read_messages(directory: Path) -> tuple[list[dict[str, Any]], int]:
    messages: list[dict[str, Any]] = []
    malformed = 0
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        if (
            not isinstance(value, dict)
            or not label(value.get("id"))
            or label(value.get("role"), 32) not in {"user", "assistant"}
        ):
            malformed += 1
            continue
        messages.append(value)
    messages.sort(
        key=lambda item: (
            timestamp(item.get("createdAt")) or timestamp(0),
            int(item.get("num", 0)) if isinstance(item.get("num"), int) else 0,
        )
    )
    return messages, malformed


def _rank(value: tuple[str, list[dict[str, Any]]]) -> tuple[int, str]:
    return len(value[1]), digest_records(value[1])


def _normalize(
    snapshot: Snapshot, machine: str, plan_id: str, messages: list[dict[str, Any]]
) -> None:
    conversation_id = f"plandex:{plan_id}"
    turns: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    totals = empty_tokens()
    model_sequence = 0
    for message in messages:
        created = timestamp(message.get("createdAt"))
        role = message.get("role")
        if role == "user":
            if active:
                finish_turn(active, created)
            active = new_turn(conversation_id, str(message["id"]), created)
            turns.append(active)
        else:
            usage = tokens(total=message.get("tokens"))
            add_tokens(totals, usage)
            model_sequence += 1
            if active:
                active["model_calls"] += 1
                add_tokens(active, usage)
                finish_turn(
                    active,
                    created,
                    "aborted" if message.get("stopped") else "completed",
                )
            snapshot.model_calls.append(
                {
                    "id": f"{conversation_id}:model:{model_sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": active["id"] if active else None,
                    "sequence": model_sequence,
                    "timestamp": iso(created),
                    "model": "unknown",
                    **usage,
                }
            )
    if active and active["status"] == "in-progress":
        finish_turn(active, timestamp(messages[-1].get("createdAt")), "in-progress")
    snapshot.turns.extend(turns)
    started = timestamp(messages[0].get("createdAt"))
    ended = timestamp(messages[-1].get("createdAt"))
    snapshot.conversations.append(
        {
            "id": conversation_id,
            "provider": "plandex",
            "external_id": plan_id,
            "source_machine": machine,
            "project": "outside-project",
            "project_source": "none",
            "started_at": iso(started),
            "ended_at": iso(ended),
            "duration_seconds": max(0.0, (ended - started).total_seconds())
            if started and ended
            else None,
            "source": "server-files-v1",
            "models": ["unknown"] if model_sequence else [],
            "iterations": len(turns),
            "model_calls": model_sequence,
            "tool_calls": 0,
            "compactions": 0,
            "event_count": len(messages),
            "content_hash": digest_records(messages),
            **totals,
        }
    )
