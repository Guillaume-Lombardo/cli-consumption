from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, cast, exists, func, or_, select
from sqlalchemy.engine import Connection
from sqlalchemy.sql import Select

from cli_consumption.storage import TABLES, Conversation

DATE_VALUE = re.compile(r"\d{4}-\d{2}-\d{2}")
CONVERSATION_CHILDREN = frozenset(
    {
        "turns",
        "model_calls",
        "tool_calls",
        "work_items",
        "context_samples",
        "turn_settings",
        "compaction_events",
    }
)


@dataclass(frozen=True, slots=True)
class ExportWindow:
    since: datetime | None = None
    until: datetime | None = None

    def __post_init__(self) -> None:
        for value in (self.since, self.until):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("export bounds must include a timezone")
        if (
            self.since is not None
            and self.until is not None
            and self.since >= self.until
        ):
            raise ValueError("export start must be earlier than export end")

    @property
    def bounded(self) -> bool:
        return self.since is not None or self.until is not None

    def metadata(self, *, day_precision: bool = False) -> dict[str, str | None]:
        since, until = self.since, self.until
        if day_precision:
            since = _day_floor(since)
            until = _day_ceiling(until)
        return {
            "since": _utc_iso(since),
            "until": _utc_iso(until),
        }


def parse_export_window(
    since: str | None = None, until: str | None = None
) -> ExportWindow:
    return ExportWindow(
        since=_parse_bound(since, end=False),
        until=_parse_bound(until, end=True),
    )


def iter_report_rows(
    connection: Connection,
    table_name: str,
    window: ExportWindow | None = None,
    *,
    batch_size: int = 1_000,
) -> Iterator[dict[str, Any]]:
    statement = report_statement(connection, table_name, window)
    result = connection.execution_options(
        stream_results=True, yield_per=batch_size
    ).execute(statement)
    for row in result.mappings().yield_per(batch_size):
        yield dict(row)


def report_statement(
    connection: Connection,
    table_name: str,
    window: ExportWindow | None = None,
) -> Select[Any]:
    model = TABLES.get(table_name)
    if model is None:
        raise ValueError(f"Unknown table: {table_name}")
    table = model.__table__
    selected = _selected_conversations(connection, window or ExportWindow())
    statement = select(table)
    active_window = window or ExportWindow()
    if active_window.bounded:
        if table_name == "conversations":
            statement = statement.where(table.c.id.in_(selected))
        elif table_name in CONVERSATION_CHILDREN:
            statement = statement.where(table.c.conversation_id.in_(selected))
        elif table_name == "subagents":
            statement = statement.where(_subagent_belongs_to_selection(table, selected))
        elif table_name == "ingestion_runs":
            statement = statement.where(
                _timestamp_conditions(connection, table.c.ingested_at, active_window)
            )
    return statement.order_by(*table.primary_key)


def _selected_conversations(
    connection: Connection, window: ExportWindow
) -> Select[Any]:
    table = Conversation.__table__
    statement = select(table.c.id)
    if not window.bounded:
        return statement
    started = _timestamp_expression(connection, table.c.started_at)
    ended = _timestamp_expression(connection, table.c.ended_at)
    first_activity = func.coalesce(started, ended)
    last_activity = func.coalesce(ended, started)
    conditions = [first_activity.is_not(None), last_activity.is_not(None)]
    if window.since is not None:
        conditions.append(last_activity >= _bound_expression(connection, window.since))
    if window.until is not None:
        conditions.append(first_activity < _bound_expression(connection, window.until))
    return statement.where(and_(*conditions))


def _subagent_belongs_to_selection(table: Any, selected: Select[Any]) -> Any:
    conversation = Conversation.__table__.alias("selected_conversation")
    selected_ids = selected.subquery("selected_conversation_ids")
    selected_conversation = conversation.join(
        selected_ids, conversation.c.id == selected_ids.c.id
    )
    common = and_(
        conversation.c.provider == table.c.provider,
        conversation.c.source_machine == table.c.source_machine,
    )
    parent = exists(
        select(1)
        .select_from(selected_conversation)
        .where(common, conversation.c.external_id == table.c.parent_thread_id)
    )
    child = exists(
        select(1)
        .select_from(selected_conversation)
        .where(common, conversation.c.external_id == table.c.child_thread_id)
    )
    return or_(parent, child)


def _timestamp_conditions(
    connection: Connection, column: Any, window: ExportWindow
) -> Any:
    value = _timestamp_expression(connection, column)
    conditions = [value.is_not(None)]
    if window.since is not None:
        conditions.append(value >= _bound_expression(connection, window.since))
    if window.until is not None:
        conditions.append(value < _bound_expression(connection, window.until))
    return and_(*conditions)


def _timestamp_expression(connection: Connection, column: Any) -> Any:
    if connection.dialect.name == "sqlite":
        return func.datetime(column)
    return cast(column, DateTime(timezone=True))


def _bound_expression(connection: Connection, value: datetime) -> Any:
    instant = value.astimezone(UTC)
    if connection.dialect.name == "sqlite":
        return func.datetime(instant)
    return instant


def _parse_bound(value: str | None, *, end: bool) -> datetime | None:
    if value is None:
        return None
    if DATE_VALUE.fullmatch(value):
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("invalid export date") from error
        instant = datetime.combine(parsed_date, time.min, UTC)
        return instant + timedelta(days=1) if end else instant
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid export timestamp") from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("export timestamps must include a timezone")
    return instant.astimezone(UTC)


def _utc_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _day_floor(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _day_ceiling(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    instant = value.astimezone(UTC)
    floor = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return floor if instant == floor else floor + timedelta(days=1)
