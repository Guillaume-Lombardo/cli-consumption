from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    ProviderInputBudget,
    add_tokens,
    counter,
    iso,
    iter_bounded_jsonl_bytes,
    label,
    mapping,
    project,
    read_bounded_bytes,
    timestamp,
    tokens,
)
from cli_consumption.models import Snapshot, empty_tokens


@dataclass(slots=True)
class _Candidate:
    machine: str
    external_id: str
    metadata: dict[str, Any]
    messages: list[dict[str, Any]]
    event_count: int
    digest: str


class MistralVibeAdapter:
    """Read Mistral Vibe session logs without retaining conversation content."""

    name = "mistral-vibe"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected: dict[str, _Candidate] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            sessions = home / "logs" / "session"
            if not sessions.is_dir():
                raise ValueError(
                    f"Missing Mistral Vibe session log directory: {sessions}"
                )
            for metadata_path in budget.sorted_paths(sessions.glob("*/meta.json")):
                candidate, invalid = _read_candidate(machine, metadata_path, budget)
                malformed += invalid
                if candidate is None:
                    continue
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
        for candidate in sorted(selected.values(), key=lambda item: item.external_id):
            self._normalize(snapshot, candidate, project_mappings or [])
        return snapshot

    def _normalize(
        self,
        snapshot: Snapshot,
        source: _Candidate,
        mappings: list[tuple[str, str]],
    ) -> None:
        conversation_id = f"mistral-vibe:{source.external_id}"
        started_at = timestamp(source.metadata.get("start_time"))
        ended_at = timestamp(source.metadata.get("end_time"))
        active_turn: dict[str, Any] | None = None
        turn_number = 0
        tool_number = 0
        compaction_number = 0

        for message in source.messages:
            role = message.get("role")
            if role == "user" and message.get("injected") is not True:
                if active_turn is not None:
                    active_turn["status"] = "completed"
                turn_number += 1
                active_turn = _turn(conversation_id, message, turn_number)
                snapshot.turns.append(active_turn)
                snapshot.turn_settings.append(
                    {
                        "id": f"{conversation_id}:settings:{turn_number}",
                        "conversation_id": conversation_id,
                        "turn_id": active_turn["id"],
                        "model": None,
                        "effort": None,
                        "collaboration_mode": None,
                        "service_tier": None,
                        "context_window_tokens": None,
                    }
                )

            if message.get("context_boundary") == "compaction":
                compaction_number += 1
                snapshot.compaction_events.append(
                    {
                        "id": f"{conversation_id}:compaction:{compaction_number}",
                        "conversation_id": conversation_id,
                        "turn_id": active_turn["id"] if active_turn else None,
                        "sequence": compaction_number,
                        "timestamp": None,
                    }
                )

            if role != "assistant":
                continue
            if active_turn is not None:
                active_turn["status"] = "completed"
            raw_tool_calls = message.get("tool_calls")
            if not isinstance(raw_tool_calls, list):
                continue
            for raw_call in raw_tool_calls:
                function = mapping(mapping(raw_call).get("function"))
                tool_name = label(function.get("name"))
                if tool_name is None:
                    snapshot.malformed_records += 1
                    continue
                tool_number += 1
                if active_turn is not None:
                    active_turn["tool_calls"] += 1
                snapshot.tool_calls.append(
                    {
                        "id": f"{conversation_id}:tool:{tool_number}",
                        "conversation_id": conversation_id,
                        "turn_id": active_turn["id"] if active_turn else None,
                        "sequence": tool_number,
                        "timestamp": None,
                        "tool_name": tool_name,
                        "outer_tool_name": tool_name,
                    }
                )

        stats = mapping(source.metadata.get("stats"))
        prompt_tokens = counter(stats.get("session_prompt_tokens"))
        cached_tokens = min(prompt_tokens, counter(stats.get("session_cached_tokens")))
        completion_tokens = counter(stats.get("session_completion_tokens"))
        usage = tokens(
            uncached=prompt_tokens - cached_tokens,
            cached=cached_tokens,
            visible=completion_tokens,
        )
        config = mapping(source.metadata.get("config"))
        model = label(config.get("active_model"), 255) or "unknown"
        model_calls = 0
        totals = empty_tokens()
        if usage["total_tokens"] > 0 or counter(stats.get("steps")) > 0:
            model_calls = 1
            add_tokens(totals, usage)
            snapshot.model_calls.append(
                {
                    "id": f"{conversation_id}:model:aggregate",
                    "conversation_id": conversation_id,
                    "turn_id": None,
                    "sequence": 1,
                    "timestamp": iso(ended_at),
                    "model": model,
                    **usage,
                }
            )

        directory = mapping(source.metadata.get("environment")).get("working_directory")
        project_name, project_source = project(
            directory if isinstance(directory, str) else None, mappings
        )
        snapshot.conversations.append(
            {
                "id": conversation_id,
                "provider": self.name,
                "external_id": source.external_id,
                "source_machine": source.machine,
                "project": project_name,
                "project_source": project_source,
                "started_at": iso(started_at),
                "ended_at": iso(ended_at),
                "duration_seconds": _duration(started_at, ended_at),
                "source": "local-session-json",
                "models": [model] if model_calls else [],
                "iterations": turn_number,
                "model_calls": model_calls,
                "tool_calls": tool_number,
                "compactions": compaction_number,
                "event_count": source.event_count,
                "content_hash": source.digest,
                **totals,
            }
        )


def _read_candidate(
    machine: str, metadata_path: Path, budget: ProviderInputBudget
) -> tuple[_Candidate | None, int]:
    digest = hashlib.sha256()
    try:
        metadata_bytes = read_bounded_bytes(metadata_path, budget)
        digest.update(metadata_bytes)
        metadata = json.loads(metadata_bytes)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, 1
    if not isinstance(metadata, dict):
        return None, 1
    external_id = label(metadata.get("session_id"), 512)
    if external_id is None:
        return None, 1

    messages: list[dict[str, Any]] = []
    malformed = 0
    messages_path = metadata_path.parent / "messages.jsonl"
    try:
        budget.candidate(messages_path)
        for line in iter_bounded_jsonl_bytes(messages_path, budget):
            digest.update(line)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(message, dict) or not isinstance(
                message.get("role"), str
            ):
                malformed += 1
                continue
            messages.append(message)
    except OSError:
        malformed += 1

    return (
        _Candidate(
            machine=machine,
            external_id=external_id,
            metadata=metadata,
            messages=messages,
            event_count=len(messages),
            digest=digest.hexdigest(),
        ),
        malformed,
    )


def _turn(
    conversation_id: str, message: dict[str, Any], sequence: int
) -> dict[str, Any]:
    external_id = label(message.get("message_id"), 512) or f"turn-{sequence}"
    return {
        "id": f"{conversation_id}:{external_id}",
        "conversation_id": conversation_id,
        "external_id": external_id,
        "started_at": None,
        "ended_at": None,
        "status": "in-progress",
        "duration_ms": None,
        "time_to_first_token_ms": None,
        "model_calls": 0,
        "tool_calls": 0,
        **empty_tokens(),
    }


def _duration(started_at: datetime | None, ended_at: datetime | None) -> float | None:
    if started_at is None or ended_at is None:
        return None
    return max(0.0, (ended_at - started_at).total_seconds())


def _rank(candidate: _Candidate) -> tuple[int, str, str]:
    ended_at = timestamp(candidate.metadata.get("end_time"))
    return candidate.event_count, iso(ended_at) or "", candidate.digest
