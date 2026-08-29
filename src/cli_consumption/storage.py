from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import (
    BigInteger,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    create_engine,
    delete,
    event,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import NullPool

from cli_consumption.models import Snapshot, SnapshotPayload, SnapshotValidationError
from cli_consumption.schema import upgrade_database
from cli_consumption.timestamps import canonical_timestamp

DEFAULT_DATABASE = "sqlite:///cli-consumption.sqlite"


class MissingOptionalDependencyError(RuntimeError):
    """Raised when a selected database backend is not installed."""


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(512), index=True)
    source_machine: Mapped[str] = mapped_column(String(255), index=True)
    project: Mapped[str] = mapped_column(String(512), index=True)
    project_source: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[str | None] = mapped_column(String(64), index=True)
    ended_at: Mapped[str | None] = mapped_column(String(64), index=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(255))
    models_json: Mapped[str] = mapped_column(Text)
    iterations: Mapped[int] = mapped_column(Integer)
    model_calls: Mapped[int] = mapped_column(Integer)
    tool_calls: Mapped[int] = mapped_column(Integer)
    compactions: Mapped[int] = mapped_column(Integer)
    event_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_write_input_tokens: Mapped[int] = mapped_column(BigInteger)
    uncached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    reasoning_output_tokens: Mapped[int] = mapped_column(BigInteger)
    visible_output_tokens: Mapped[int] = mapped_column(BigInteger)
    unattributed_tokens: Mapped[int] = mapped_column(BigInteger)
    total_tokens: Mapped[int] = mapped_column(BigInteger)


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(512))
    started_at: Mapped[str | None] = mapped_column(String(64), index=True)
    ended_at: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(BigInteger)
    model_calls: Mapped[int] = mapped_column(Integer)
    tool_calls: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_write_input_tokens: Mapped[int] = mapped_column(BigInteger)
    uncached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    reasoning_output_tokens: Mapped[int] = mapped_column(BigInteger)
    visible_output_tokens: Mapped[int] = mapped_column(BigInteger)
    unattributed_tokens: Mapped[int] = mapped_column(BigInteger)
    total_tokens: Mapped[int] = mapped_column(BigInteger)


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(1024), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(255), index=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    cache_write_input_tokens: Mapped[int] = mapped_column(BigInteger)
    uncached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    reasoning_output_tokens: Mapped[int] = mapped_column(BigInteger)
    visible_output_tokens: Mapped[int] = mapped_column(BigInteger)
    unattributed_tokens: Mapped[int] = mapped_column(BigInteger)
    total_tokens: Mapped[int] = mapped_column(BigInteger)


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(1024), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[str | None] = mapped_column(String(64), index=True)
    tool_name: Mapped[str] = mapped_column(String(512), index=True)
    outer_tool_name: Mapped[str] = mapped_column(String(512))


class WorkItem(Base):
    """A content-free provider activity interval within a conversation."""

    __tablename__ = "work_items"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(1024), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    tool_name: Mapped[str | None] = mapped_column(String(512), index=True)
    started_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    completed_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), index=True)


class ContextSample(Base):
    """Context-window pressure reported for one provider model-usage event."""

    __tablename__ = "context_samples"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(1024), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[str | None] = mapped_column(String(64), index=True)
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    context_window_tokens: Mapped[int] = mapped_column(BigInteger)


class TurnSetting(Base):
    """Last effective provider configuration observed for a turn."""

    __tablename__ = "turn_settings"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str] = mapped_column(String(1024), index=True)
    model: Mapped[str | None] = mapped_column(String(255), index=True)
    effort: Mapped[str | None] = mapped_column(String(64), index=True)
    collaboration_mode: Mapped[str | None] = mapped_column(String(64), index=True)
    service_tier: Mapped[str | None] = mapped_column(String(64), index=True)
    context_window_tokens: Mapped[int | None] = mapped_column(BigInteger)


class CompactionEvent(Base):
    """A timestamped context compaction without replacement content or window IDs."""

    __tablename__ = "compaction_events"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(1024), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[str | None] = mapped_column(String(64), index=True)


class Subagent(Base):
    __tablename__ = "subagents"

    id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    source_machine: Mapped[str] = mapped_column(String(255), index=True)
    parent_thread_id: Mapped[str] = mapped_column(String(512), index=True)
    child_thread_id: Mapped[str] = mapped_column(String(512), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    created_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    updated_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    agent_role: Mapped[str] = mapped_column(String(255))
    tokens_used: Mapped[int | None] = mapped_column(BigInteger)


class SubagentScope(Base):
    """Internal scope state used to serialize relationship replacement."""

    __tablename__ = "subagent_scopes"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_machine: Mapped[str] = mapped_column(String(255), primary_key=True)
    lock_version: Mapped[int] = mapped_column(BigInteger)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    ingested_at: Mapped[str] = mapped_column(String(64), index=True)
    conversations_received: Mapped[int] = mapped_column(Integer)
    conversations_written: Mapped[int] = mapped_column(Integer)
    conversations_skipped: Mapped[int] = mapped_column(Integer)
    malformed_records: Mapped[int] = mapped_column(Integer)
    duplicate_conversations: Mapped[int] = mapped_column(Integer)


TABLES = {
    "conversations": Conversation,
    "turns": Turn,
    "model_calls": ModelCall,
    "tool_calls": ToolCall,
    "work_items": WorkItem,
    "context_samples": ContextSample,
    "turn_settings": TurnSetting,
    "compaction_events": CompactionEvent,
    "subagents": Subagent,
    "ingestion_runs": IngestionRun,
}

SCHEMA_TABLES = {**TABLES, "subagent_scopes": SubagentScope}


@dataclass(frozen=True, slots=True)
class IngestionResult:
    run_id: str
    received: int
    written: int
    skipped: int


def normalize_database_url(value: str | Path) -> str:
    raw = str(value)
    if "://" in raw:
        return raw
    path = Path(raw).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def create_database_engine(database: str | Path) -> Engine:
    url = normalize_database_url(database)
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith("postgresql+psycopg://"):
        try:
            import psycopg  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            raise MissingOptionalDependencyError(
                "PostgreSQL support requires optional dependencies; "
                "install cli-consumption[postgres]"
            ) from None
    engine = (
        create_engine(url, poolclass=NullPool)
        if url.startswith("sqlite:")
        else create_engine(url)
    )
    if url.startswith("sqlite:"):

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def initialize_database(engine: Engine) -> None:
    upgrade_database(engine)


def ingest_snapshot(engine: Engine, snapshot: Snapshot) -> IngestionResult:
    snapshot = validate_snapshot(snapshot)
    initialize_database(engine)
    run_id = str(uuid.uuid4())
    written = 0
    skipped = 0
    turns_by_conversation = _group(snapshot.turns)
    calls_by_conversation = _group(snapshot.model_calls)
    tools_by_conversation = _group(snapshot.tool_calls)
    work_by_conversation = _group(snapshot.work_items)
    context_by_conversation = _group(snapshot.context_samples)
    settings_by_conversation = _group(snapshot.turn_settings)
    compactions_by_conversation = _group(snapshot.compaction_events)
    subagent_scopes = {
        (snapshot.provider, str(record["source_machine"]))
        for record in (*snapshot.conversations, *snapshot.subagents)
    }
    stale_subagent_scopes: set[tuple[str, str]] = set()
    richer_subagent_scopes: set[tuple[str, str]] = set()
    with Session(engine) as session, session.begin():
        initial_subagent_scopes = {
            scope
            for scope in sorted(subagent_scopes)
            if _lock_subagent_scope(session, *scope)
        }
        for record in snapshot.conversations:
            conversation_id = str(record["id"])
            existing = session.get(Conversation, conversation_id)
            scope = (snapshot.provider, str(record["source_machine"]))
            if existing is not None and existing.event_count > int(
                record["event_count"]
            ):
                stale_subagent_scopes.add(scope)
            elif existing is not None and existing.event_count < int(
                record["event_count"]
            ):
                richer_subagent_scopes.add(scope)
            if existing is not None and (
                existing.event_count > int(record["event_count"])
                or (
                    existing.event_count == int(record["event_count"])
                    and existing.content_hash == record["content_hash"]
                )
            ):
                skipped += 1
                continue
            session.execute(
                delete(ModelCall).where(ModelCall.conversation_id == conversation_id)
            )
            session.execute(
                delete(ToolCall).where(ToolCall.conversation_id == conversation_id)
            )
            session.execute(
                delete(WorkItem).where(WorkItem.conversation_id == conversation_id)
            )
            session.execute(
                delete(ContextSample).where(
                    ContextSample.conversation_id == conversation_id
                )
            )
            session.execute(
                delete(TurnSetting).where(
                    TurnSetting.conversation_id == conversation_id
                )
            )
            session.execute(
                delete(CompactionEvent).where(
                    CompactionEvent.conversation_id == conversation_id
                )
            )
            session.execute(delete(Turn).where(Turn.conversation_id == conversation_id))
            session.merge(_conversation_from_record(record))
            session.flush()
            for turn in turns_by_conversation.get(conversation_id, []):
                session.add(Turn(**turn))
            for call in calls_by_conversation.get(conversation_id, []):
                session.add(ModelCall(**call))
            for tool in tools_by_conversation.get(conversation_id, []):
                session.add(ToolCall(**tool))
            for work_item in work_by_conversation.get(conversation_id, []):
                session.add(WorkItem(**work_item))
            for sample in context_by_conversation.get(conversation_id, []):
                session.add(ContextSample(**sample))
            for setting in settings_by_conversation.get(conversation_id, []):
                session.add(TurnSetting(**setting))
            for compaction in compactions_by_conversation.get(conversation_id, []):
                session.add(CompactionEvent(**compaction))
            written += 1
        authoritative_scopes = initial_subagent_scopes | (
            richer_subagent_scopes - stale_subagent_scopes
        )
        for provider, source_machine in authoritative_scopes:
            session.execute(
                delete(Subagent).where(
                    Subagent.provider == provider,
                    Subagent.source_machine == source_machine,
                )
            )
        for subagent in snapshot.subagents:
            scope = (snapshot.provider, str(subagent["source_machine"]))
            if scope in authoritative_scopes:
                session.add(Subagent(**subagent))
        session.add(
            IngestionRun(
                id=run_id,
                provider=snapshot.provider,
                ingested_at=canonical_timestamp(datetime.now(UTC)),
                conversations_received=len(snapshot.conversations),
                conversations_written=written,
                conversations_skipped=skipped,
                malformed_records=snapshot.malformed_records,
                duplicate_conversations=snapshot.duplicate_conversations,
            )
        )
    return IngestionResult(run_id, len(snapshot.conversations), written, skipped)


def _lock_subagent_scope(session: Session, provider: str, source_machine: str) -> bool:
    """Create or serialize one graph scope, returning whether it was newly created."""
    table = cast(Table, SubagentScope.__table__)
    values = {
        "provider": provider,
        "source_machine": source_machine,
        "lock_version": 0,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table)
    elif dialect == "postgresql":
        statement = postgresql_insert(table)
    else:  # pragma: no cover - guarded by the supported database backends
        raise RuntimeError("Unsupported database backend")
    created = session.scalar(
        statement.values(**values)
        .on_conflict_do_nothing(index_elements=["provider", "source_machine"])
        .returning(table.c.provider)
    )
    session.execute(
        update(SubagentScope)
        .where(
            SubagentScope.provider == provider,
            SubagentScope.source_machine == source_machine,
        )
        .values(lock_version=SubagentScope.lock_version + 1)
    )
    return created is not None


def read_table(engine: Engine, table_name: str) -> list[dict[str, Any]]:
    initialize_database(engine)
    model = TABLES.get(table_name)
    if model is None:
        raise ValueError(f"Unknown table: {table_name}")
    with Session(engine) as session:
        rows = (
            session.execute(select(model).order_by(*model.__table__.primary_key))
            .scalars()
            .all()
        )
        return [
            {
                column.name: getattr(row, column.name)
                for column in model.__table__.columns
            }
            for row in rows
        ]


def validate_snapshot(snapshot: Snapshot) -> Snapshot:
    """Validate values and referential integrity before opening a transaction."""
    try:
        payload = SnapshotPayload.model_validate(snapshot.to_dict())
    except Exception as error:
        raise SnapshotValidationError() from error

    from cli_consumption.adapters.registry import resolve_adapter_spec

    if resolve_adapter_spec(payload.provider) is None:
        raise SnapshotValidationError()

    groups = (
        payload.conversations,
        payload.turns,
        payload.model_calls,
        payload.tool_calls,
        payload.work_items,
        payload.context_samples,
        payload.turn_settings,
        payload.compaction_events,
        payload.subagents,
    )
    for records in groups:
        identifiers = [record.id for record in records]
        if len(identifiers) != len(set(identifiers)):
            raise SnapshotValidationError()

    conversation_ids = {record.id for record in payload.conversations}
    turns = {record.id: record.conversation_id for record in payload.turns}
    if any(record.provider != payload.provider for record in payload.conversations):
        raise SnapshotValidationError()
    if any(record.provider != payload.provider for record in payload.subagents):
        raise SnapshotValidationError()
    if any(record.conversation_id not in conversation_ids for record in payload.turns):
        raise SnapshotValidationError()

    child_groups = (
        payload.model_calls,
        payload.tool_calls,
        payload.work_items,
        payload.context_samples,
        payload.turn_settings,
        payload.compaction_events,
    )
    for records in child_groups:
        sequences: set[tuple[str, int]] = set()
        for record in records:
            if record.conversation_id not in conversation_ids:
                raise SnapshotValidationError()
            turn_id = record.turn_id
            if turn_id is not None and turns.get(turn_id) != record.conversation_id:
                raise SnapshotValidationError()
            sequence = getattr(record, "sequence", None)
            if sequence is not None:
                key = (record.conversation_id, sequence)
                if key in sequences:
                    raise SnapshotValidationError()
                sequences.add(key)

    for conversation in payload.conversations:
        if len(conversation.models) != len(set(conversation.models)):
            raise SnapshotValidationError()
        _validate_time_range(conversation.started_at, conversation.ended_at)
    for turn in payload.turns:
        _validate_time_range(turn.started_at, turn.ended_at)
    for item in payload.work_items:
        if (
            item.started_at_ms is not None
            and item.completed_at_ms is not None
            and item.completed_at_ms < item.started_at_ms
        ):
            raise SnapshotValidationError()
    return Snapshot(**payload.model_dump())


def _validate_time_range(started_at: str | None, ended_at: str | None) -> None:
    if started_at is None or ended_at is None:
        return
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    if end < start:
        raise SnapshotValidationError()


def _group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row["conversation_id"]), []).append(row)
    return result


def _conversation_from_record(record: dict[str, Any]) -> Conversation:
    values = dict(record)
    values["models_json"] = json.dumps(values.pop("models"), separators=(",", ":"))
    return Conversation(**values)
