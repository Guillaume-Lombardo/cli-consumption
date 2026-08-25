from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import NullPool

from cli_consumption.models import Snapshot

DEFAULT_DATABASE = "sqlite:///cli-consumption.sqlite"


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
    ended_at: Mapped[str | None] = mapped_column(String(64))
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
    agent_nickname: Mapped[str] = mapped_column(String(255))
    agent_role: Mapped[str] = mapped_column(String(255))
    tokens_used: Mapped[int | None] = mapped_column(BigInteger)


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
    "subagents": Subagent,
    "ingestion_runs": IngestionRun,
}


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
    Base.metadata.create_all(engine)


def ingest_snapshot(engine: Engine, snapshot: Snapshot) -> IngestionResult:
    validate_snapshot(snapshot)
    initialize_database(engine)
    run_id = str(uuid.uuid4())
    written = 0
    skipped = 0
    turns_by_conversation = _group(snapshot.turns)
    calls_by_conversation = _group(snapshot.model_calls)
    tools_by_conversation = _group(snapshot.tool_calls)
    with Session(engine) as session, session.begin():
        for subagent in snapshot.subagents:
            session.merge(Subagent(**subagent))
        for record in snapshot.conversations:
            conversation_id = str(record["id"])
            existing = session.get(Conversation, conversation_id)
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
            session.execute(delete(Turn).where(Turn.conversation_id == conversation_id))
            session.merge(_conversation_from_record(record))
            session.flush()
            for turn in turns_by_conversation.get(conversation_id, []):
                session.add(Turn(**turn))
            for call in calls_by_conversation.get(conversation_id, []):
                session.add(ModelCall(**call))
            for tool in tools_by_conversation.get(conversation_id, []):
                session.add(ToolCall(**tool))
            written += 1
        session.add(
            IngestionRun(
                id=run_id,
                provider=snapshot.provider,
                ingested_at=datetime.now(UTC).isoformat(),
                conversations_received=len(snapshot.conversations),
                conversations_written=written,
                conversations_skipped=skipped,
                malformed_records=snapshot.malformed_records,
                duplicate_conversations=snapshot.duplicate_conversations,
            )
        )
    return IngestionResult(run_id, len(snapshot.conversations), written, skipped)


def read_table(engine: Engine, table_name: str) -> list[dict[str, Any]]:
    initialize_database(engine)
    model = TABLES.get(table_name)
    if model is None:
        raise ValueError(f"Unknown table: {table_name}")
    with Session(engine) as session:
        rows = session.execute(select(model)).scalars().all()
        return [
            {
                column.name: getattr(row, column.name)
                for column in model.__table__.columns
            }
            for row in rows
        ]


def validate_snapshot(snapshot: Snapshot) -> None:
    """Reject missing or unexpected transport fields before opening a transaction."""
    groups: tuple[tuple[str, list[dict[str, Any]], set[str]], ...] = (
        (
            "conversation",
            snapshot.conversations,
            (set(Conversation.__table__.columns.keys()) - {"models_json"}) | {"models"},
        ),
        ("turn", snapshot.turns, set(Turn.__table__.columns.keys())),
        ("model call", snapshot.model_calls, set(ModelCall.__table__.columns.keys())),
        ("tool call", snapshot.tool_calls, set(ToolCall.__table__.columns.keys())),
        ("subagent", snapshot.subagents, set(Subagent.__table__.columns.keys())),
    )
    for record_type, records, expected in groups:
        for record in records:
            actual = set(record)
            if actual != expected:
                missing = sorted(expected - actual)
                unexpected = sorted(actual - expected)
                raise ValueError(
                    f"Invalid {record_type} fields; missing={missing}, "
                    f"unexpected={unexpected}"
                )


def _group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row["conversation_id"]), []).append(row)
    return result


def _conversation_from_record(record: dict[str, Any]) -> Conversation:
    values = dict(record)
    values["models_json"] = json.dumps(values.pop("models"), separators=(",", ":"))
    return Conversation(**values)
