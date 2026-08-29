from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, cast, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from cli_consumption.schema import upgrade_database
from cli_consumption.storage import Conversation, IngestionRun, Subagent


@dataclass(frozen=True, slots=True)
class RetentionResult:
    cutoff: datetime
    conversations: int
    subagents: int
    ingestion_runs: int
    applied: bool


def retain_before(
    engine: Engine, cutoff: datetime, *, apply: bool = False
) -> RetentionResult:
    """Preview or delete normalized metadata older than an aware UTC cutoff."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("Retention cutoff must include a timezone")
    normalized = cutoff.astimezone(UTC)
    cutoff_ms = int(normalized.timestamp() * 1000)
    conversation_filter = _before_cutoff(
        engine,
        func.coalesce(Conversation.ended_at, Conversation.started_at),
        normalized,
    )
    subagent_filter = (
        func.coalesce(Subagent.updated_at_ms, Subagent.created_at_ms) < cutoff_ms
    )
    ingestion_filter = _before_cutoff(engine, IngestionRun.ingested_at, normalized)

    upgrade_database(engine)
    with Session(engine) as session, session.begin():
        conversations = session.scalar(
            select(func.count()).select_from(Conversation).where(conversation_filter)
        )
        subagents = session.scalar(
            select(func.count()).select_from(Subagent).where(subagent_filter)
        )
        ingestion_runs = session.scalar(
            select(func.count()).select_from(IngestionRun).where(ingestion_filter)
        )
        if apply:
            session.execute(delete(Conversation).where(conversation_filter))
            session.execute(delete(Subagent).where(subagent_filter))
            session.execute(delete(IngestionRun).where(ingestion_filter))
    return RetentionResult(
        cutoff=normalized,
        conversations=conversations or 0,
        subagents=subagents or 0,
        ingestion_runs=ingestion_runs or 0,
        applied=apply,
    )


def _before_cutoff(engine: Engine, column: Any, cutoff: datetime) -> Any:
    """Compare timestamp text by instant rather than lexical representation."""
    if engine.dialect.name == "sqlite":
        return func.datetime(column) < func.datetime(cutoff)
    return cast(column, DateTime(timezone=True)) < cutoff
