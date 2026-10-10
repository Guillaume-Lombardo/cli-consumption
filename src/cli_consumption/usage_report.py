"""Provider-neutral usage aggregation shared by terminal and programmatic reports.

This module reads the normalized database and returns period, session, and
breakdown aggregates. It never renders text and never reads provider files, so the
terminal report, a status line, a read-only MCP server, or budget checks can reuse
it unchanged.

Token selection mirrors the dashboard calculation contract
(``packages/analytics``): a model call from a provider with ``additive`` token
semantics counts only when it has a timestamp inside the window and belongs to no
turn or to a closed (completed or aborted) turn inside the window. Calls from
``conversation-aggregate`` and ``context-snapshot`` providers count once their
conversation is selected; they have no honest per-call time attribution, so period
views attribute them to the conversation's period and flag the row. Providers with
``unavailable`` token semantics contribute activity counts but never token values.
Token counters are local usage metadata, not billing data.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, exists, false, not_, or_, select, true
from sqlalchemy.engine import Connection, Engine

from cli_consumption.adapters.registry import ADAPTER_SPECS, resolve_adapter_spec
from cli_consumption.dashboard import share_safe_labels
from cli_consumption.reporting import (
    DATE_VALUE,
    ExportWindow,
    ReportFilters,
    _conversation_model_membership,
    parse_export_window,
    report_statement,
)
from cli_consumption.storage import ModelCall, ToolCall, Turn, initialize_database
from cli_consumption.timestamps import canonical_timestamp

REPORT_SCHEMA = "cli-consumption/usage-report"
REPORT_SCHEMA_VERSION = 1
BILLING_NOTICE = "Token counters are local usage metadata, not billing data."
UNAVAILABLE = "unavailable"
UNTIMED_SEMANTICS = ("conversation-aggregate", "context-snapshot")
CLOSED_TURN_STATUSES = frozenset({"completed", "aborted"})
_TOKEN_COLUMNS = (
    ("input", "input_tokens"),
    ("cache_read", "cached_input_tokens"),
    ("cache_write", "cache_write_input_tokens"),
    ("uncached_input", "uncached_input_tokens"),
    ("output", "output_tokens"),
    ("reasoning", "reasoning_output_tokens"),
    ("visible_output", "visible_output_tokens"),
    ("unattributed", "unattributed_tokens"),
    ("total", "total_tokens"),
)


class ReportView(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    SESSION = "session"


class Breakdown(StrEnum):
    MODEL = "model"
    PROVIDER = "provider"
    PROJECT = "project"
    MACHINE = "machine"


class UsageQueryError(ValueError):
    """A report query parameter is invalid; the code never echoes input values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class UsageQuery:
    view: ReportView = ReportView.DAILY
    window: ExportWindow = field(default_factory=ExportWindow)
    filters: ReportFilters = field(default_factory=ReportFilters)
    timezone: str = "UTC"
    breakdown: Breakdown | None = None
    share_safe: bool = False


@dataclass(frozen=True, slots=True)
class TokenCounts:
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    uncached_input: int = 0
    output: int = 0
    reasoning: int = 0
    visible_output: int = 0
    unattributed: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name, _ in _TOKEN_COLUMNS}


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    conversations: int
    turns: int
    calls: int
    tokens: TokenCounts | None
    token_semantics: tuple[str, ...]

    @property
    def cache_rate(self) -> float | None:
        """Cache reads divided by all input tokens, or None when undefined."""
        if self.tokens is None or self.tokens.input <= 0:
            return None
        return self.tokens.cache_read / self.tokens.input

    @property
    def flags(self) -> tuple[str, ...]:
        """Stable presentation flags derived from the contributing semantics."""
        flags = [value for value in UNTIMED_SEMANTICS if value in self.token_semantics]
        if self.tokens is None:
            flags.append("tokens-unavailable")
        elif UNAVAILABLE in self.token_semantics:
            flags.append("partial-tokens")
        return tuple(flags)

    def to_dict(self) -> dict[str, Any]:
        rate = self.cache_rate
        return {
            "conversations": self.conversations,
            "turns": self.turns,
            "calls": self.calls,
            "tokens": None if self.tokens is None else self.tokens.to_dict(),
            "cache_rate": None if rate is None else round(rate, 6),
            "token_semantics": list(self.token_semantics),
            "flags": list(self.flags),
        }


@dataclass(frozen=True, slots=True)
class SessionLabel:
    number: int
    started_at: str | None
    provider: str
    project: str
    machine: str
    models: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "started_at": self.started_at,
            "provider": self.provider,
            "project": self.project,
            "machine": self.machine,
            "models": list(self.models),
        }


@dataclass(frozen=True, slots=True)
class BreakdownRow:
    label: str
    metrics: UsageMetrics


@dataclass(frozen=True, slots=True)
class UsageRow:
    period: str | None
    session: SessionLabel | None
    metrics: UsageMetrics
    breakdown: tuple[BreakdownRow, ...] = ()


@dataclass(frozen=True, slots=True)
class UsageReport:
    view: ReportView
    timezone: str
    window: ExportWindow
    filters: dict[str, tuple[str, ...]]
    breakdown: Breakdown | None
    share_safe: bool
    rows: tuple[UsageRow, ...]
    totals: UsageMetrics

    def to_dict(self) -> dict[str, Any]:
        """Return the deterministic, versioned report contract."""
        rows = []
        for row in self.rows:
            payload = row.metrics.to_dict()
            payload["period"] = row.period
            payload["session"] = None if row.session is None else row.session.to_dict()
            payload["breakdown"] = [
                {"label": item.label, **item.metrics.to_dict()}
                for item in row.breakdown
            ]
            rows.append(payload)
        return {
            "schema": REPORT_SCHEMA,
            "schema_version": REPORT_SCHEMA_VERSION,
            "view": self.view.value,
            "timezone": self.timezone,
            "window": self.window.metadata(day_precision=self.share_safe),
            "filters": {key: list(values) for key, values in self.filters.items()},
            "breakdown": None if self.breakdown is None else self.breakdown.value,
            "share_safe": self.share_safe,
            "notice": BILLING_NOTICE,
            "rows": rows,
            "totals": self.totals.to_dict(),
        }


def resolve_timezone(name: str) -> tzinfo:
    """Resolve an IANA timezone name without echoing invalid input."""
    if name.upper() == "UTC":
        return UTC
    if not name or len(name) > 64 or name.startswith(("/", ".")) or ".." in name:
        raise UsageQueryError("invalid_timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise UsageQueryError("invalid_timezone") from None


def parse_report_window(
    since: str | None, until: str | None, timezone: str = "UTC"
) -> ExportWindow:
    """Parse half-open report bounds; plain dates use the report timezone."""
    zone = resolve_timezone(timezone)

    def bound(value: str | None, *, end: bool) -> datetime | None:
        if value is None or not DATE_VALUE.fullmatch(value):
            return None
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise UsageQueryError("invalid_window") from None
        if end:
            day += timedelta(days=1)
        return datetime.combine(day, time.min, zone).astimezone(UTC)

    try:
        timestamps = parse_export_window(
            None if since is None or DATE_VALUE.fullmatch(since) else since,
            None if until is None or DATE_VALUE.fullmatch(until) else until,
        )
        window = ExportWindow(
            since=bound(since, end=False) or timestamps.since,
            until=bound(until, end=True) or timestamps.until,
        )
        # Every later presentation must stay representable: local rendering and
        # share-safe day rounding both shift the bounds near the calendar limits.
        window.metadata(day_precision=True)
        for value in (window.since, window.until):
            if value is not None:
                value.astimezone(zone)
                # The dashboard compares millisecond instants. Millisecond bounds
                # make its truncated comparison equal to the half-open microsecond
                # comparison used here; finer bounds would diverge.
                if value.microsecond % 1_000:
                    raise UsageQueryError("invalid_window")
    except UsageQueryError:
        raise
    except (ValueError, OverflowError):
        raise UsageQueryError("invalid_window") from None
    return window


def report_filters(
    *,
    providers: Iterable[str] = (),
    machines: Iterable[str] = (),
    projects: Iterable[str] = (),
    models: Iterable[str] = (),
) -> ReportFilters:
    """Build deduplicated filters, resolving provider aliases to canonical names."""
    canonical: list[str] = []
    for value in providers:
        spec = resolve_adapter_spec(value)
        if spec is None:
            raise UsageQueryError("unknown_provider")
        canonical.append(spec.name)
    return ReportFilters(
        providers=tuple(dict.fromkeys(canonical)),
        machines=tuple(dict.fromkeys(machines)),
        projects=tuple(dict.fromkeys(projects)),
        models=tuple(dict.fromkeys(models)),
    )


def aggregate_usage(engine: Engine, query: UsageQuery) -> UsageReport:
    """Aggregate one report from a single coherent database snapshot."""
    initialize_database(engine)
    with _read_snapshot(engine) as connection:
        return aggregate_usage_on(connection, query)


def aggregate_usage_on(connection: Connection, query: UsageQuery) -> UsageReport:
    """Aggregate one report on a caller-provided connection and transaction."""
    return _Aggregation(connection, query).run()


@contextmanager
def _read_snapshot(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                yield connection
            return
        connection.exec_driver_sql("BEGIN DEFERRED")
        try:
            yield connection
        finally:
            connection.rollback()


class _Accumulator:
    __slots__ = ("calls", "conversations", "semantics", "tokens", "turns")

    def __init__(self) -> None:
        self.conversations = 0
        self.turns = 0
        self.calls = 0
        self.tokens = [0] * len(_TOKEN_COLUMNS)
        self.semantics: set[str] = set()

    def metrics(self) -> UsageMetrics:
        semantics = tuple(sorted(self.semantics))
        measured = not semantics or any(value != UNAVAILABLE for value in semantics)
        tokens = (
            TokenCounts(
                **{
                    name: value
                    for (name, _), value in zip(
                        _TOKEN_COLUMNS, self.tokens, strict=True
                    )
                }
            )
            if measured
            else None
        )
        return UsageMetrics(
            conversations=self.conversations,
            turns=self.turns,
            calls=self.calls,
            tokens=tokens,
            token_semantics=semantics,
        )


@dataclass(slots=True)
class _Session:
    order: tuple[str, str, str, str, str]
    started_at: str | None
    provider: str
    project: str
    machine: str
    models: tuple[str, ...]


class _Aggregation:
    def __init__(self, connection: Connection, query: UsageQuery) -> None:
        self.connection = connection
        self.query = query
        self.zone = resolve_timezone(query.timezone)
        self.semantics = {spec.name: spec.token_semantics for spec in ADAPTER_SPECS}
        self.untimed_providers = tuple(
            sorted(
                name
                for name, semantics in self.semantics.items()
                if semantics in UNTIMED_SEMANTICS
            )
        )
        window = query.window
        if query.share_safe:
            # The share-safe dashboard rounds timestamps and its window to UTC days
            # before analytical selection; day bounds make both selections equal.
            self.zone = UTC
            bounds = window.metadata(day_precision=True)
            self.since, self.until = bounds["since"], bounds["until"]
        else:
            self.since = (
                None if window.since is None else canonical_timestamp(window.since)
            )
            self.until = (
                None if window.until is None else canonical_timestamp(window.until)
            )
        # Whether the dashboard has an analytical range; set by ``run``.
        self.has_range = window.bounded
        self.selected = (
            report_statement(connection, "conversations", window, filters=query.filters)
            .order_by(None)
            .subquery("report_conversations")
        )
        self.periods: dict[str | None, _Accumulator] = {}
        self.details: dict[tuple[str | None, str], _Accumulator] = {}
        self.totals = _Accumulator()
        self.sessions: dict[str, _Session] = {}
        self.projects: dict[str, str] = {}
        self.machines: dict[str, str] = {}
        self.models: dict[str, str] = {}

    # SQL selection mirroring the dashboard calculation contract.

    def _bounded(self, column: Any) -> list[Any]:
        conditions = []
        if self.since is not None:
            conditions.append(column >= self.since)
        if self.until is not None:
            conditions.append(column < self.until)
        return conditions

    def _turn_in_window(self, column: Any) -> Any:
        if self.since is not None:
            return and_(column.is_not(None), *self._bounded(column))
        if self.until is not None:
            return or_(column.is_(None), column < self.until)
        return true()

    def _untimed(self) -> Any:
        if not self.untimed_providers:
            return false()
        return self.selected.c.provider.in_(self.untimed_providers)

    def _untimed_admitted(self) -> Any:
        """Untimed counters need a dated conversation once a range exists."""
        if not self.has_range:
            return self._untimed()
        return and_(
            self._untimed(),
            or_(
                self.selected.c.started_at.is_not(None),
                self.selected.c.ended_at.is_not(None),
            ),
        )

    def _has_dates(self) -> bool:
        """Return whether the dashboard dataset would contain any timestamp."""
        checks = (
            ("conversations", ("started_at", "ended_at")),
            ("turns", ("started_at", "ended_at")),
            ("model_calls", ("timestamp",)),
            ("tool_calls", ("timestamp",)),
            ("context_samples", ("timestamp",)),
            ("compaction_events", ("timestamp",)),
            ("work_items", ("started_at_ms",)),
            ("subagents", ("created_at_ms",)),
            ("ingestion_runs", ("ingested_at",)),
        )
        for table_name, columns in checks:
            rows = (
                report_statement(
                    self.connection,
                    table_name,
                    self.query.window,
                    filters=self.query.filters,
                )
                .order_by(None)
                .subquery(f"dated_{table_name}")
            )
            dated = or_(*(rows.c[column].is_not(None) for column in columns))
            if self.connection.scalar(
                select(exists(select(1).select_from(rows).where(dated)))
            ):
                return True
        return False

    def _model_condition(self, call: Any) -> Any:
        models = self.query.filters.models
        if not models:
            return true()
        return and_(
            call.c.model.in_(models),
            _conversation_model_membership(
                self.connection, self.selected.c.models_json, models
            ),
        )

    def _candidate_call(self, call: Any) -> Any:
        """Calls matching the model filter and the window (dashboard step one)."""
        timed = and_(
            not_(self._untimed()),
            call.c.timestamp.is_not(None),
            *self._bounded(call.c.timestamp),
        )
        return and_(self._model_condition(call), or_(self._untimed_admitted(), timed))

    def _selected_call(self, call: Any, turn: Any) -> Any:
        """Candidate calls whose turn, when present, is inside the window."""
        turn_ok = or_(turn.c.id.is_(None), self._turn_in_window(turn.c.started_at))
        return and_(self._candidate_call(call), or_(self._untimed(), turn_ok))

    # Aggregation passes.

    def run(self) -> UsageReport:
        query = self.query
        self.has_range = query.window.bounded or self._has_dates()
        if query.share_safe:
            labels = share_safe_labels(
                self.connection, window=query.window, filters=query.filters
            )
            self.projects, self.machines, self.models = (
                labels.projects,
                labels.machines,
                labels.models,
            )
        self._aggregate_conversations()
        self._aggregate_turns()
        self._aggregate_calls()
        return UsageReport(
            view=query.view,
            timezone=(
                "UTC"
                if query.share_safe or query.timezone.upper() == "UTC"
                else query.timezone
            ),
            window=query.window,
            filters=self._display_filters(),
            breakdown=query.breakdown,
            share_safe=query.share_safe,
            rows=self._rows(),
            totals=self.totals.metrics(),
        )

    def _aggregate_conversations(self) -> None:
        selected = self.selected
        call = ModelCall.__table__.alias("conversation_call")
        turn = Turn.__table__.alias("conversation_call_turn")
        selected_call = (
            select(1)
            .select_from(call.outerjoin(turn, turn.c.id == call.c.turn_id))
            .where(
                call.c.conversation_id == selected.c.id, self._selected_call(call, turn)
            )
        )
        conditions: list[Any] = []
        if self.query.filters.models:
            conditions.append(exists(selected_call))
        elif self.has_range and not self.query.window.bounded:
            # Without a window, a dateless conversation counts only when active;
            # without any timestamp at all, the dashboard has no range and counts it.
            any_turn = Turn.__table__.alias("conversation_turn")
            any_tool = ToolCall.__table__.alias("conversation_tool")
            conditions.append(
                or_(
                    selected.c.started_at.is_not(None),
                    selected.c.ended_at.is_not(None),
                    exists(
                        select(1).where(any_turn.c.conversation_id == selected.c.id)
                    ),
                    exists(
                        select(1).where(any_tool.c.conversation_id == selected.c.id)
                    ),
                    exists(selected_call),
                )
            )
        statement = select(
            selected.c.id,
            selected.c.provider,
            selected.c.project,
            selected.c.source_machine,
            selected.c.started_at,
            selected.c.ended_at,
            selected.c.models_json,
        ).where(*conditions)
        for row in self._stream(statement):
            conversation_id = str(row["id"])
            provider = str(row["provider"])
            period = self._conversation_period(row["started_at"], row["ended_at"])
            if self.query.view is ReportView.SESSION:
                self._register_session(conversation_id, row)
                period = conversation_id
            semantics = self._semantics(provider)
            for accumulator in (self.totals, self._period(period)):
                accumulator.conversations += 1
                accumulator.semantics.add(semantics)
            if self.query.breakdown not in (None, Breakdown.MODEL):
                detail = self._detail(period, self._dimension_label(row))
                detail.conversations += 1
                detail.semantics.add(semantics)
        if self.query.breakdown is Breakdown.MODEL:
            self._aggregate_conversation_models()

    def _aggregate_conversation_models(self) -> None:
        selected = self.selected
        call = ModelCall.__table__.alias("model_call")
        turn = Turn.__table__.alias("model_call_turn")
        pairs = (
            select(call.c.conversation_id, call.c.model)
            .select_from(
                call.join(selected, selected.c.id == call.c.conversation_id).outerjoin(
                    turn, turn.c.id == call.c.turn_id
                )
            )
            .where(self._selected_call(call, turn))
            .distinct()
            .subquery("conversation_models")
        )
        statement = select(
            selected.c.id,
            selected.c.provider,
            selected.c.started_at,
            selected.c.ended_at,
            pairs.c.model,
        ).select_from(selected.join(pairs, pairs.c.conversation_id == selected.c.id))
        for row in self._stream(statement):
            period = (
                str(row["id"])
                if self.query.view is ReportView.SESSION
                else self._conversation_period(row["started_at"], row["ended_at"])
            )
            detail = self._detail(period, self._model_label(row["model"]))
            detail.conversations += 1
            detail.semantics.add(self._semantics(str(row["provider"])))

    def _aggregate_turns(self) -> None:
        selected = self.selected
        turn = Turn.__table__.alias("report_turn")
        columns = [
            turn.c.conversation_id,
            turn.c.started_at,
            selected.c.provider,
            selected.c.project,
            selected.c.source_machine,
        ]
        source = turn.join(selected, selected.c.id == turn.c.conversation_id)
        conditions: list[Any] = [self._turn_in_window(turn.c.started_at)]
        call = ModelCall.__table__.alias("turn_call")
        candidate = and_(call.c.turn_id == turn.c.id, self._candidate_call(call))
        if self.query.filters.models:
            conditions.append(exists(select(1).where(candidate)))
        for row in self._stream(
            select(*columns).select_from(source).where(*conditions)
        ):
            period = self._turn_period(row)
            semantics = self._semantics(str(row["provider"]))
            for accumulator in (self.totals, self._period(period)):
                accumulator.turns += 1
                accumulator.semantics.add(semantics)
            if self.query.breakdown not in (None, Breakdown.MODEL):
                detail = self._detail(period, self._dimension_label(row))
                detail.turns += 1
                detail.semantics.add(semantics)
        if self.query.breakdown is Breakdown.MODEL:
            pairs = (
                select(call.c.turn_id, call.c.model)
                .select_from(
                    call.join(selected, selected.c.id == call.c.conversation_id)
                )
                .where(call.c.turn_id.is_not(None), self._candidate_call(call))
                .distinct()
                .subquery("turn_models")
            )
            statement = (
                select(*columns, pairs.c.model)
                .select_from(source.join(pairs, pairs.c.turn_id == turn.c.id))
                .where(*conditions)
            )
            for row in self._stream(statement):
                detail = self._detail(
                    self._turn_period(row), self._model_label(row["model"])
                )
                detail.turns += 1
                detail.semantics.add(self._semantics(str(row["provider"])))

    def _aggregate_calls(self) -> None:
        selected = self.selected
        call = ModelCall.__table__.alias("report_call")
        turn = Turn.__table__.alias("report_call_turn")
        token_columns = [call.c[column] for _, column in _TOKEN_COLUMNS]
        statement = (
            select(
                call.c.conversation_id,
                call.c.timestamp,
                call.c.model,
                *token_columns,
                selected.c.provider,
                selected.c.project,
                selected.c.source_machine,
                selected.c.started_at,
                selected.c.ended_at,
                turn.c.id.label("turn_key"),
                turn.c.status.label("turn_status"),
            )
            .select_from(
                call.join(selected, selected.c.id == call.c.conversation_id).outerjoin(
                    turn, turn.c.id == call.c.turn_id
                )
            )
            .where(self._selected_call(call, turn))
        )
        breakdown = self.query.breakdown
        for row in self._stream(statement):
            semantics = self._semantics(str(row["provider"]))
            untimed = semantics in UNTIMED_SEMANTICS
            if self.query.view is ReportView.SESSION:
                period: str | None = str(row["conversation_id"])
            elif untimed:
                period = self._conversation_period(row["started_at"], row["ended_at"])
            else:
                period = self._period_key(row["timestamp"])
            counted = semantics != UNAVAILABLE and (
                untimed
                or row["turn_key"] is None
                or row["turn_status"] in CLOSED_TURN_STATUSES
            )
            targets = [self.totals, self._period(period)]
            if breakdown is Breakdown.MODEL:
                targets.append(self._detail(period, self._model_label(row["model"])))
            elif breakdown is not None:
                targets.append(self._detail(period, self._dimension_label(row)))
            for accumulator in targets:
                accumulator.calls += 1
                accumulator.semantics.add(semantics)
                if counted:
                    tokens = accumulator.tokens
                    for index, (_, column) in enumerate(_TOKEN_COLUMNS):
                        tokens[index] += int(row[column])

    # Helpers.

    def _stream(self, statement: Any) -> Iterator[Any]:
        result = self.connection.execution_options(
            stream_results=True, yield_per=1_000
        ).execute(statement)
        yield from result.mappings().yield_per(1_000)

    def _semantics(self, provider: str) -> str:
        return self.semantics.get(provider, UNAVAILABLE)

    def _period(self, key: str | None) -> _Accumulator:
        accumulator = self.periods.get(key)
        if accumulator is None:
            accumulator = self.periods[key] = _Accumulator()
        return accumulator

    def _detail(self, key: str | None, label: str) -> _Accumulator:
        accumulator = self.details.get((key, label))
        if accumulator is None:
            accumulator = self.details[(key, label)] = _Accumulator()
        return accumulator

    def _period_key(self, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            local = datetime.fromisoformat(value).astimezone(self.zone)
        except (ValueError, OverflowError):
            return None
        day = local.date()
        if self.query.view is ReportView.WEEKLY:
            return (day - timedelta(days=day.weekday())).isoformat()
        if self.query.view is ReportView.MONTHLY:
            return f"{day.year:04d}-{day.month:02d}"
        return day.isoformat()

    def _conversation_period(self, started_at: Any, ended_at: Any) -> str | None:
        anchor = started_at or ended_at
        if anchor is not None and self.since is not None and anchor < self.since:
            anchor = self.since
        return self._period_key(anchor)

    def _turn_period(self, row: Any) -> str | None:
        if self.query.view is ReportView.SESSION:
            return str(row["conversation_id"])
        return self._period_key(row["started_at"])

    def _label(self, mapping: dict[str, str], value: Any, prefix: str) -> str:
        text = str(value)
        if not self.query.share_safe:
            return text
        return mapping.get(text, f"{prefix}-unmapped")

    def _model_label(self, value: Any) -> str:
        return self._label(self.models, value or "unknown", "model")

    def _dimension_label(self, row: Any) -> str:
        breakdown = self.query.breakdown
        if breakdown is Breakdown.PROVIDER:
            return str(row["provider"])
        if breakdown is Breakdown.PROJECT:
            return self._label(self.projects, row["project"], "project")
        return self._label(self.machines, row["source_machine"], "machine")

    def _register_session(self, conversation_id: str, row: Any) -> None:
        started = row["started_at"] or row["ended_at"]
        models = json.loads(row["models_json"]) if row["models_json"] else []
        project = self._label(self.projects, row["project"], "project")
        machine = self._label(self.machines, row["source_machine"], "machine")
        provider = str(row["provider"])
        self.sessions[conversation_id] = _Session(
            order=(
                "0" if started else "1",
                started or "",
                provider,
                project,
                conversation_id,
            ),
            started_at=self._session_start(started),
            provider=provider,
            project=project,
            machine=machine,
            models=tuple(sorted({self._model_label(model) for model in models})),
        )

    def _session_start(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            local = datetime.fromisoformat(value).astimezone(self.zone)
        except (ValueError, OverflowError):
            return None
        if self.query.share_safe:
            return local.date().isoformat()
        return local.replace(microsecond=0).isoformat()

    def _display_filters(self) -> dict[str, tuple[str, ...]]:
        filters = self.query.filters
        return {
            "providers": filters.providers,
            "projects": tuple(
                self._label(self.projects, value, "project")
                for value in filters.projects
            ),
            "machines": tuple(
                self._label(self.machines, value, "machine")
                for value in filters.machines
            ),
            "models": tuple(self._model_label(value) for value in filters.models),
        }

    def _breakdown_rows(self, key: str | None) -> tuple[BreakdownRow, ...]:
        if self.query.breakdown is None:
            return ()
        rows = [
            BreakdownRow(label=label, metrics=accumulator.metrics())
            for (period, label), accumulator in self.details.items()
            if period == key
        ]
        rows.sort(
            key=lambda row: (
                -(row.metrics.tokens.total if row.metrics.tokens else -1),
                row.label,
            )
        )
        return tuple(rows)

    def _rows(self) -> tuple[UsageRow, ...]:
        if self.query.view is ReportView.SESSION:
            ordered = sorted(self.sessions.items(), key=lambda item: item[1].order)
            return tuple(
                UsageRow(
                    period=None,
                    session=SessionLabel(
                        number=number,
                        started_at=session.started_at,
                        provider=session.provider,
                        project=session.project,
                        machine=session.machine,
                        models=session.models,
                    ),
                    metrics=self._period(conversation_id).metrics(),
                    breakdown=self._breakdown_rows(conversation_id),
                )
                for number, (conversation_id, session) in enumerate(ordered, 1)
            )
        keys = sorted(self.periods, key=lambda key: (key is None, key or ""))
        return tuple(
            UsageRow(
                period=key,
                session=None,
                metrics=self.periods[key].metrics(),
                breakdown=self._breakdown_rows(key),
            )
            for key in keys
        )
