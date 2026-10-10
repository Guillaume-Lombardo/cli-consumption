from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import Any

from cli_consumption import models
from cli_consumption.adapters._incremental import (
    INCREMENTAL_CANDIDATES_PER_BATCH,
    MAX_INCREMENTAL_LISTING,
    bounded_sorted_paths,
    charge_candidates,
    iter_collection_batches,
)
from cli_consumption.adapters._shared import (
    MAX_BIGINT as MAX_BIGINT,
)
from cli_consumption.adapters._shared import (
    ProviderDataLimitError,
    ProviderInputBudget,
    iter_bounded_jsonl_bytes,
    read_bounded_bytes,
)
from cli_consumption.adapters._shared import (
    add_tokens as _add_tokens,
)
from cli_consumption.adapters._shared import (
    basic_label as _label,
)
from cli_consumption.adapters._shared import (
    counter as _counter,
)
from cli_consumption.adapters.base import CollectionBatch
from cli_consumption.models import Snapshot, empty_tokens


class ClaudeAdapter:
    """Read Claude Code transcript metadata while discarding conversation content."""

    name = "claude"

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Snapshot:
        budget = ProviderInputBudget()
        transcripts: list[_Transcript] = []
        for machine, home in sources:
            projects = home / "projects"
            if not projects.is_dir():
                raise ValueError(f"Missing Claude Code projects directory: {projects}")
            paths = chain(
                projects.glob("*/*.jsonl"),
                projects.glob("*/*/subagents/**/agent-*.jsonl"),
            )
            transcripts.extend(
                _classify(machine, projects, path)
                for path in budget.sorted_paths(paths)
            )
        selected, duplicates, malformed = self._select(
            transcripts, project_mappings or [], budget
        )
        return self._snapshot(selected, duplicates, malformed)

    def collect_incrementally(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]] | None = None,
    ) -> Iterator[CollectionBatch]:
        """Yield bounded batches whose results equal one collection.

        A first pass reads each transcript only until it knows the identities that
        duplicate selection and replay filtering use. Every transcript sharing one
        of those identities, across all sources and project directories, lands in
        the same indivisible batch group, so each group is normalized exactly as a
        single collection would normalize it. Relationships use merge semantics
        because one batch never sees the whole provider/source-machine graph.
        """
        mappings = project_mappings or []
        for _, home in sources:
            if not (home / "projects").is_dir():
                raise ValueError("Missing Claude Code projects directory")
        yielded = False
        for batch in iter_collection_batches(
            _transcript_groups(sources),
            lambda items: self._collect_batch(items, mappings),
            candidates_per_batch=INCREMENTAL_CANDIDATES_PER_BATCH,
            subagent_merge=True,
        ):
            yielded = True
            yield batch
        if not yielded:
            yield CollectionBatch(
                Snapshot(provider=self.name), frozenset(), subagent_merge=True
            )

    def _collect_batch(
        self, transcripts: list[_Transcript], mappings: list[tuple[str, str]]
    ) -> Snapshot:
        budget = ProviderInputBudget()
        selected, duplicates, malformed = self._select(
            list(charge_candidates(transcripts, budget)), mappings, budget
        )
        return self._snapshot(selected, duplicates, malformed)

    def _snapshot(
        self, selected: dict[str, _Selection], duplicates: int, malformed: int
    ) -> Snapshot:
        snapshot = Snapshot(
            provider=self.name,
            duplicate_conversations=duplicates,
            malformed_records=malformed,
        )
        for _, records, edge in selected.values():
            for name in _RECORD_COLLECTIONS:
                getattr(snapshot, name).extend(getattr(records, name))
            if edge is not None:
                snapshot.subagents.append(edge)
        return snapshot

    def _select(
        self,
        transcripts: list[_Transcript],
        mappings: list[tuple[str, str]],
        budget: ProviderInputBudget,
    ) -> tuple[dict[str, _Selection], int, int]:
        # Each file is read once. Only the normalized, content-free records of the
        # most complete copy of each session or subagent are retained between files.
        selected: dict[str, _Selection] = {}
        retained_records = 0
        retained_message_ids = 0
        duplicates = malformed = 0
        sessions = [item for item in transcripts if not item.agent]
        agents = [item for item in transcripts if item.agent]

        # Sidechain transcripts can replay parent responses. Only the response
        # identifiers of possible parent sessions are kept, and
        # only until agent transcripts have been read.
        parents = {item.parent for item in agents if item.parent is not None}
        has_legacy_agents = any(item.parent is None for item in agents)
        parent_messages: dict[str, set[str]] = {}

        def choose(key: str, rank: tuple[int, str]) -> bool:
            nonlocal duplicates, retained_records
            previous = selected.get(key)
            if previous is None:
                return True
            duplicates += 1
            if rank <= previous[0]:
                return False
            retained_records -= sum(
                len(getattr(previous[1], name)) for name in _RECORD_COLLECTIONS
            ) + int(previous[2] is not None)
            for name in _RECORD_COLLECTIONS:
                getattr(previous[1], name).clear()
            del selected[key]
            return True

        for item in sessions:
            events, content_hash, session_id, invalid = _load_events(item.path, budget)
            malformed += invalid
            rank = (len(events), content_hash)
            if not choose(session_id, rank):
                del events
                continue
            if session_id in parents or has_legacy_agents:
                retained_message_ids -= len(parent_messages.pop(session_id, set()))
                identifiers = _message_ids(
                    events, models.MAX_SNAPSHOT_RECORDS - retained_message_ids
                )
                parent_messages[session_id] = identifiers
                retained_message_ids += len(identifiers)
                del identifiers
            records = Snapshot(
                provider=self.name,
                _record_limit=models.MAX_SNAPSHOT_RECORDS - retained_records,
            )
            self._read(records, item.machine, events, rank, session_id, mappings)
            del events
            retained_records += sum(
                len(getattr(records, name)) for name in _RECORD_COLLECTIONS
            )
            selected[session_id] = (rank, records, None)

        for item in agents:
            events, content_hash, session_id, invalid = _load_events(item.path, budget)
            malformed += invalid
            if item.parent is None and not any(
                _label(event.get("sessionId"), 512) for event in events
            ):
                malformed += 1
                del events
                continue
            session_id = item.parent or session_id
            agent_id = _agent_id(events, item.path)
            external_id = _label(f"{session_id}:agent:{agent_id}", 512)
            if agent_id is None or external_id is None:
                malformed += 1
                del events
                continue
            rank = (len(events), content_hash)
            if not choose(external_id, rank):
                del events
                continue
            replayed = parent_messages.get(session_id, set())
            records = Snapshot(
                provider=self.name,
                _record_limit=models.MAX_SNAPSHOT_RECORDS - retained_records - 1,
            )
            self._read(
                records,
                item.machine,
                [event for event in events if not _replays(event, replayed)],
                rank,
                external_id,
                mappings,
                sidechain=True,
            )
            del events
            edge = _subagent_edge(
                self.name,
                item.machine,
                session_id,
                external_id,
                records,
                _agent_role(item.path, budget),
            )
            retained_records += 1 + sum(
                len(getattr(records, name)) for name in _RECORD_COLLECTIONS
            )
            selected[external_id] = (rank, records, edge)
        return selected, duplicates, malformed

    def _read(
        self,
        snapshot: Snapshot,
        machine: str,
        events: list[dict[str, Any]],
        selection: tuple[int, str],
        session_id: str,
        mappings: list[tuple[str, str]],
        *,
        sidechain: bool = False,
    ) -> None:
        event_count, digest = selection
        conversation_id = f"claude:{session_id}"
        timestamps = [
            value
            for event in events
            if (value := _timestamp(event.get("timestamp"))) is not None
        ]
        started_at, ended_at = (
            min(timestamps, default=None),
            max(timestamps, default=None),
        )
        turns: dict[str, dict[str, Any]] = {}
        turn_models: dict[str, set[str]] = {}
        active: str | None = None
        # request/message ID -> (rank, event index, turn, timestamp, model, tokens)
        calls: dict[
            str,
            tuple[
                tuple[bool, int, int],
                int,
                str | None,
                datetime | None,
                str,
                dict[str, int],
            ],
        ] = {}
        tools: dict[str, tuple[str | None, datetime | None, str]] = {}
        compactions = 0

        for index, event in enumerate(events, 1):
            timestamp = _timestamp(event.get("timestamp"))
            if _starts_turn(event, sidechain=sidechain):
                if active is not None:
                    _finish_turn(turns[active], timestamp)
                external_id = _label(event.get("uuid"), 512) or f"turn-{len(turns) + 1}"
                if external_id in turns:
                    external_id = f"turn-{len(turns) + 1}"
                active = external_id
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

            if event.get("type") == "system" and event.get("subtype") in {
                "compact",
                "compact_boundary",
            }:
                compactions += 1
                snapshot.compaction_events.append(
                    {
                        "id": f"{conversation_id}:compaction:{compactions}",
                        "conversation_id": conversation_id,
                        "turn_id": turns[active]["id"] if active else None,
                        "sequence": compactions,
                        "timestamp": _iso(timestamp),
                    }
                )

            if event.get("type") != "assistant" or not isinstance(
                (message := event.get("message")), dict
            ):
                continue
            if active:
                turn = turns[active]
                turn["ended_at"] = _iso(timestamp) or turn["ended_at"]
                turn["status"] = (
                    "aborted" if event.get("isApiErrorMessage") is True else "completed"
                )
            model = _label(message.get("model"), 255) or "unknown"
            call_key = (
                _label(event.get("requestId"), 512)
                or _label(message.get("id"), 512)
                or _label(event.get("uuid"), 512)
                or f"event-{index}"
            )
            if isinstance((usage := message.get("usage")), dict):
                tokens = _usage(usage)
                rank = (
                    message.get("stop_reason") is not None,
                    sum(tokens.values()),
                    index,
                )
                previous = calls.get(call_key)
                if previous is None or rank > previous[0]:
                    calls[call_key] = (rank, index, active, timestamp, model, tokens)
            if not isinstance((content := message.get("content")), list):
                continue
            for block_index, block in enumerate(content, 1):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = _label(block.get("name"), 512)
                if name:
                    key = _label(block.get("id"), 512) or (
                        f"{call_key}:{block_index}:{name}"
                    )
                    tools.setdefault(key, (active, timestamp, name))

        if active:
            _finish_turn(turns[active], ended_at)

        totals = empty_tokens()
        models: set[str] = set()
        for sequence, call in enumerate(
            sorted(calls.values(), key=lambda row: row[1]), 1
        ):
            _, _, turn_key, timestamp, model, tokens = call
            models.add(model)
            turn = turns.get(turn_key or "")
            if turn:
                turn["model_calls"] += 1
                turn_models[turn_key or ""].add(model)
                _add_tokens(turn, tokens)
            _add_tokens(totals, tokens)
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

        for sequence, (turn_key, timestamp, name) in enumerate(tools.values(), 1):
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

        for key, turn in turns.items():
            snapshot.turns.append(turn)
            observed_models = turn_models[key]
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:settings:{key}",
                    "conversation_id": conversation_id,
                    "turn_id": turn["id"],
                    "model": next(iter(observed_models))
                    if len(observed_models) == 1
                    else None,
                    "effort": None,
                    "collaboration_mode": None,
                    "service_tier": None,
                    "context_window_tokens": None,
                }
            )

        project, project_source = _project(events, mappings)
        snapshot.conversations.append(
            {
                "id": conversation_id,
                "provider": self.name,
                "external_id": session_id,
                "source_machine": machine,
                "project": project,
                "project_source": project_source,
                "started_at": _iso(started_at),
                "ended_at": _iso(ended_at),
                "duration_seconds": (
                    (ended_at - started_at).total_seconds()
                    if started_at and ended_at
                    else None
                ),
                "source": "local-jsonl",
                "models": sorted(models),
                "iterations": len(turns),
                "model_calls": len(calls),
                "tool_calls": len(tools),
                "compactions": compactions,
                "event_count": event_count,
                "content_hash": digest,
                **totals,
            }
        )


_RECORD_COLLECTIONS = (
    "conversations",
    "turns",
    "model_calls",
    "tool_calls",
    "turn_settings",
    "compaction_events",
)


_Selection = tuple[tuple[int, str], Snapshot, dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class _Transcript:
    machine: str
    path: Path
    agent: bool
    parent: str | None


def _classify(machine: str, projects: Path, path: Path) -> _Transcript:
    parts = path.relative_to(projects).parts
    agent = (len(parts) >= 4 and parts[2] == "subagents") or path.name.startswith(
        "agent-"
    )
    return _Transcript(
        machine=machine,
        path=path,
        agent=agent,
        parent=_nested_agent_session(parts) if agent else None,
    )


def _transcript_groups(
    sources: list[tuple[str, Path]],
) -> Iterator[list[_Transcript]]:
    """Group transcripts by every identity shared during one collection.

    In a single collection a session copy interacts with other transcripts only
    through its selection key (its session ID), and a subagent copy only through its
    selection key and the session ID it is filtered against. Joining those keys
    into connected components across all sources and project directories yields
    groups with no interaction between them, so normalizing each group alone is
    exactly the single-collection result. Members keep the single-collection order:
    source order, then sorted paths.
    """
    members: list[tuple[int, Path, _Transcript, str | None]] = []
    roots: dict[str, str] = {}

    def find(key: str) -> str:
        roots.setdefault(key, key)
        while roots[key] != key:
            roots[key] = roots[roots[key]]
            key = roots[key]
        return key

    for index, (machine, home) in enumerate(sources):
        projects = home / "projects"
        paths = bounded_sorted_paths(
            chain(
                projects.glob("*/*.jsonl"),
                projects.glob("*/*/subagents/**/agent-*.jsonl"),
            )
        )
        if len(members) + len(paths) > MAX_INCREMENTAL_LISTING:
            raise ProviderDataLimitError("provider_incremental_listing_limit_exceeded")
        for path in paths:
            item = _classify(machine, projects, path)
            key, parent = _identity(item)
            if key is not None and parent is not None:
                roots[find(key)] = find(parent)
            members.append((index, path, item, key))

    groups: dict[str, list[tuple[int, Path, _Transcript]]] = {}
    isolated: list[list[tuple[int, Path, _Transcript]]] = []
    for index, path, item, key in members:
        if key is None:
            isolated.append([(index, path, item)])
        else:
            groups.setdefault(find(key), []).append((index, path, item))
    ordered = sorted(
        [*groups.values(), *isolated], key=lambda group: (group[0][0], group[0][1])
    )
    for group in ordered:
        yield [item for _, _, item in group]


def _identity(item: _Transcript) -> tuple[str | None, str | None]:
    """Return the selection key and, for a subagent, its parent session ID.

    Reads only until the labels used by ``_load_events`` and ``_agent_id`` are
    found; the whole file is read only when one is absent. ``None`` marks a
    transcript that a single collection skips as malformed.
    """
    session_label, agent_label, digest = _scan_identity(
        item.path,
        session=not item.agent or item.parent is None,
        agent=item.agent,
    )
    if not item.agent:
        key = (
            session_label
            or _label(item.path.stem, 512)
            or f"session-{(digest or '')[:24]}"
        )
        return key, None
    parent = item.parent or session_label
    stem = item.path.stem
    agent_id = agent_label or (
        _label(stem.removeprefix("agent-"), 255) if stem.startswith("agent-") else None
    )
    if parent is None or agent_id is None:
        return None, None
    external_id = _label(f"{parent}:agent:{agent_id}", 512)
    return (external_id, parent) if external_id is not None else (None, None)


def _scan_identity(
    path: Path, *, session: bool, agent: bool
) -> tuple[str | None, str | None, str | None]:
    budget = ProviderInputBudget()
    digest = hashlib.sha256()
    session_label = agent_label = None
    lines = iter_bounded_jsonl_bytes(path, budget)
    try:
        for line in lines:
            digest.update(line)
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if session and session_label is None:
                session_label = _label(event.get("sessionId"), 512)
            if agent and agent_label is None:
                agent_label = _label(event.get("agentId"), 255)
            if (not session or session_label is not None) and (
                not agent or agent_label is not None
            ):
                return session_label, agent_label, None
    finally:
        # Stopping early must still release the no-follow descriptor.
        close = getattr(lines, "close", None)
        if close is not None:
            close()
    return session_label, agent_label, digest.hexdigest()


MAX_AGENT_METADATA_BYTES = 64 * 1024
_AGENT_ROLE_ALIASES = {
    "explore": "research",
    "general-purpose": "worker",
    "plan": "planning",
}
_TURN_STATUS_TO_SUBAGENT_STATUS = {
    "aborted": "aborted",
    "completed": "completed",
    "in-progress": "in-progress",
}


def _nested_agent_session(parts: tuple[str, ...]) -> str | None:
    # parts is relative to projects/: <project>/<session>/subagents/.../agent-*.jsonl
    if len(parts) < 4 or parts[2] != "subagents":
        return None
    return _label(parts[1], 512)


def _agent_id(events: list[dict[str, Any]], path: Path) -> str | None:
    for event in events:
        if (agent_id := _label(event.get("agentId"), 255)) is not None:
            return agent_id
    stem = path.stem
    return (
        _label(stem.removeprefix("agent-"), 255) if stem.startswith("agent-") else None
    )


def _message_ids(events: list[dict[str, Any]], capacity: int) -> set[str]:
    identifiers: set[str] = set()
    for event in events:
        if event.get("type") != "assistant" or not isinstance(
            (message := event.get("message")), dict
        ):
            continue
        identifier = _label(message.get("id"), 512)
        if identifier is None or identifier in identifiers:
            continue
        if len(identifiers) >= capacity:
            raise ProviderDataLimitError("provider_record_limit_exceeded")
        identifiers.add(identifier)
    return identifiers


def _replays(event: dict[str, Any], parent_messages: set[str]) -> bool:
    message = event.get("message")
    return (
        event.get("type") == "assistant"
        and isinstance(message, dict)
        and _label(message.get("id"), 512) in parent_messages
    )


def _agent_role(path: Path, budget: ProviderInputBudget) -> str:
    metadata = path.with_name(f"{path.stem}.meta.json")
    if not budget.candidate(metadata).is_file():
        return "unspecified"
    try:
        value = json.loads(
            read_bounded_bytes(metadata, budget, MAX_AGENT_METADATA_BYTES)
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "unspecified"
    except ProviderDataLimitError as error:
        # Optional metadata never blocks collection; aggregate limits still apply.
        if str(error) != "provider_file_too_large":
            raise
        return "unspecified"
    agent_type = value.get("agentType") if isinstance(value, dict) else None
    if not isinstance(agent_type, str) or not agent_type.strip():
        return "unspecified"
    return _AGENT_ROLE_ALIASES.get(agent_type.strip().casefold(), "other")


def _subagent_edge(
    provider: str,
    machine: str,
    parent_id: str,
    child_id: str,
    records: Snapshot,
    role: str,
) -> dict[str, Any]:
    conversation = records.conversations[0]
    status = (
        _TURN_STATUS_TO_SUBAGENT_STATUS.get(records.turns[-1]["status"], "unknown")
        if records.turns
        else "unknown"
    )
    return {
        "id": f"claude:{machine}:{child_id}",
        "provider": provider,
        "source_machine": machine,
        "parent_thread_id": parent_id,
        "child_thread_id": child_id,
        "status": status,
        "created_at_ms": _epoch_ms(conversation["started_at"]),
        "updated_at_ms": _epoch_ms(conversation["ended_at"]),
        "agent_role": role,
        "tokens_used": conversation["total_tokens"],
    }


def _epoch_ms(value: object) -> int | None:
    timestamp = _timestamp(value)
    return int(timestamp.timestamp() * 1000) if timestamp is not None else None


def _load_events(
    path: Path, budget: ProviderInputBudget
) -> tuple[list[dict[str, Any]], str, str, int]:
    digest = hashlib.sha256()
    events: list[dict[str, Any]] = []
    malformed = 0
    session_id: str | None = None
    for line in iter_bounded_jsonl_bytes(path, budget):
        digest.update(line)
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        events.append(event)
        session_id = session_id or _label(event.get("sessionId"), 512)
    content_hash = digest.hexdigest()
    session_id = session_id or _label(path.stem, 512) or f"session-{content_hash[:24]}"
    return events, content_hash, session_id, malformed


def _starts_turn(event: dict[str, Any], *, sidechain: bool = False) -> bool:
    if (
        event.get("type") != "user"
        or event.get("isMeta") is True
        or (event.get("isSidechain") is True and not sidechain)
        or event.get("toolUseResult") is not None
        or not isinstance((message := event.get("message")), dict)
    ):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    types = {str(item.get("type")) for item in content if isinstance(item, dict)}
    return bool(types - {"tool_result"})


def _usage(value: dict[str, Any]) -> dict[str, int]:
    uncached = _counter(value.get("input_tokens"))
    cached = _counter(value.get("cache_read_input_tokens"))
    cache_write = _counter(value.get("cache_creation_input_tokens"))
    output = _counter(value.get("output_tokens"))
    input_tokens = min(MAX_BIGINT, uncached + cached + cache_write)
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
        "reasoning_output_tokens": 0,
        "total_tokens": min(MAX_BIGINT, input_tokens + output),
        "uncached_input_tokens": uncached,
        "visible_output_tokens": output,
        "unattributed_tokens": 0,
    }


def _project(
    events: list[dict[str, Any]], mappings: list[tuple[str, str]]
) -> tuple[str, str]:
    for event in events:
        if not isinstance((cwd := event.get("cwd")), str):
            continue
        cwd = cwd.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            prefix = prefix.replace("\\", "/").rstrip("/")
            if cwd == prefix or cwd.startswith(prefix + "/"):
                return name, "mapping"
    return "outside-project", "none"


def _finish_turn(turn: dict[str, Any], fallback: datetime | None) -> None:
    turn["ended_at"] = turn["ended_at"] or _iso(fallback)
    start, end = _timestamp(turn["started_at"]), _timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
