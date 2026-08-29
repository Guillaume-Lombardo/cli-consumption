from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli_consumption.adapters._shared import (
    add_tokens,
    digest_records,
    finish_turn,
    iso,
    iter_bounded_jsonl_bytes,
    label,
    mapping,
    new_turn,
    timestamp,
    tokens,
)
from cli_consumption.models import Snapshot, empty_tokens


class KimiAdapter:
    """Read metadata from Kimi Code CLI wire event logs."""

    name = "kimi"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        del project_mappings
        selected: dict[str, tuple[str, list[dict[str, Any]], int]] = {}
        duplicates = malformed = 0
        for machine, home in sources:
            files = sorted((home / "sessions").glob("*/*/wire.jsonl"))
            if not files:
                raise ValueError(
                    f"Missing Kimi Code CLI sessions under: {home / 'sessions'}"
                )
            for path in files:
                records, invalid = _read_wire(path)
                malformed += invalid
                external_id = path.parent.name
                if not label(external_id):
                    malformed += 1
                    continue
                candidate = (machine, records, path.stat().st_mtime_ns)
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
        for external_id, (machine, records, _) in sorted(selected.items()):
            _normalize(snapshot, machine, external_id, records)
        return snapshot


def _read_wire(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    try:
        for raw in iter_bounded_jsonl_bytes(path):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(value, dict):
                malformed += 1
                continue
            message = mapping(value.get("message"))
            if not label(message.get("type"), 64) or not isinstance(
                message.get("payload"), dict
            ):
                # Wire metadata is not an event record.
                if value.get("type") != "metadata" or not isinstance(
                    value.get("protocol_version"), str
                ):
                    malformed += 1
                continue
            records.append(value)
    except OSError:
        raise ValueError(f"Could not read Kimi Code CLI wire log: {path}") from None
    return records, malformed


def _rank(value: tuple[str, list[dict[str, Any]], int]) -> tuple[int, int, str]:
    return len(value[1]), value[2], digest_records(value[1])


def _normalize(
    snapshot: Snapshot, machine: str, external_id: str, records: list[dict[str, Any]]
) -> None:
    conversation_id = f"kimi:{external_id}"
    turns: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    models: set[str] = set()
    totals = empty_tokens()
    started = ended = None
    model_sequence = tool_sequence = compaction_sequence = 0

    for index, record in enumerate(records, 1):
        event_time = timestamp(record.get("timestamp"))
        started = started or event_time
        ended = event_time or ended
        message = mapping(record.get("message"))
        kind = message.get("type")
        payload = mapping(message.get("payload"))
        if kind == "TurnBegin":
            if active:
                finish_turn(active, event_time)
            active = new_turn(conversation_id, f"turn-{index}", event_time)
            turns.append(active)
        elif kind == "TurnEnd" and active:
            finish_turn(active, event_time)
        elif kind == "StepInterrupted" and active:
            finish_turn(active, event_time, "aborted")
        elif kind == "StatusUpdate":
            usage = mapping(payload.get("token_usage"))
            if usage:
                value = tokens(
                    uncached=usage.get("input_other"),
                    cached=usage.get("input_cache_read"),
                    cache_write=usage.get("input_cache_creation"),
                    visible=usage.get("output"),
                    total=usage.get("total"),
                )
                model_sequence += 1
                model = "unknown"
                models.add(model)
                add_tokens(totals, value)
                if active:
                    active["model_calls"] += 1
                    add_tokens(active, value)
                snapshot.model_calls.append(
                    {
                        "id": f"{conversation_id}:model:{model_sequence}",
                        "conversation_id": conversation_id,
                        "turn_id": active["id"] if active else None,
                        "sequence": model_sequence,
                        "timestamp": iso(event_time),
                        "model": model,
                        **value,
                    }
                )
            context_tokens = payload.get("context_tokens")
            max_context = payload.get("max_context_tokens")
            if (
                isinstance(context_tokens, int)
                and isinstance(max_context, int)
                and max_context > 0
            ):
                snapshot.context_samples.append(
                    {
                        "id": f"{conversation_id}:context:{index}",
                        "conversation_id": conversation_id,
                        "turn_id": active["id"] if active else None,
                        "timestamp": iso(event_time),
                        "input_tokens": max(0, context_tokens),
                        "context_window_tokens": max_context,
                    }
                )
        elif kind in {"ToolCall", "ToolCallRequest"}:
            function = mapping(payload.get("function"))
            name = label(function.get("name") or payload.get("name"))
            if name:
                tool_sequence += 1
                if active:
                    active["tool_calls"] += 1
                snapshot.tool_calls.append(
                    {
                        "id": f"{conversation_id}:tool:{tool_sequence}",
                        "conversation_id": conversation_id,
                        "turn_id": active["id"] if active else None,
                        "sequence": tool_sequence,
                        "timestamp": iso(event_time),
                        "tool_name": name,
                        "outer_tool_name": name,
                    }
                )
        elif kind == "CompactionEnd":
            compaction_sequence += 1
            snapshot.compaction_events.append(
                {
                    "id": f"{conversation_id}:compaction:{compaction_sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": active["id"] if active else None,
                    "sequence": compaction_sequence,
                    "timestamp": iso(event_time),
                }
            )
    if active and active["status"] == "in-progress":
        finish_turn(active, ended, "in-progress")
    snapshot.turns.extend(turns)
    snapshot.conversations.append(
        {
            "id": conversation_id,
            "provider": "kimi",
            "external_id": external_id,
            "source_machine": machine,
            "project": "outside-project",
            "project_source": "none",
            "started_at": iso(started),
            "ended_at": iso(ended),
            "duration_seconds": max(0.0, (ended - started).total_seconds())
            if started and ended
            else None,
            "source": "wire-jsonl-v1",
            "models": sorted(models),
            "iterations": len(turns),
            "model_calls": model_sequence,
            "tool_calls": tool_sequence,
            "compactions": compaction_sequence,
            "event_count": len(records),
            "content_hash": digest_records(records),
            **totals,
        }
    )
