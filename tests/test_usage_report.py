from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from report_fixtures import (
    CANARY,
    CROSSCHECK_FIXTURE,
    PATH_CANARY,
    render_crosscheck_fixture,
    report_snapshots,
    seed_report_database,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from cli_consumption.dashboard import _dashboard_context, _dashboard_snapshot
from cli_consumption.reporting import ExportWindow, ReportFilters
from cli_consumption.storage import (
    IngestionRun,
    create_database_engine,
    ingest_snapshot,
)
from cli_consumption.usage_report import (
    REPORT_SCHEMA_VERSION,
    Breakdown,
    ReportView,
    UsageMetrics,
    UsageQuery,
    UsageQueryError,
    aggregate_usage,
    parse_report_window,
    report_filters,
    resolve_timezone,
)

ROW_KEYS = {
    "breakdown",
    "cache_rate",
    "calls",
    "conversations",
    "flags",
    "period",
    "session",
    "token_semantics",
    "tokens",
    "turns",
}
TOKEN_KEYS = {
    "cache_read",
    "cache_write",
    "input",
    "output",
    "reasoning",
    "total",
    "unattributed",
    "uncached_input",
    "visible_output",
}
PRIVATE_LABELS = ("alpha", "beta", "gamma", "laptop", "desktop", "gpt-x", "claude-a")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_database_engine(tmp_path / "usage.sqlite")
    seed_report_database(engine)
    yield engine
    engine.dispose()


def _rows(engine: Engine, **kwargs: Any) -> dict[str | None, UsageMetrics]:
    report = aggregate_usage(engine, UsageQuery(**kwargs))
    return {row.period: row.metrics for row in report.rows}


def test_crosscheck_fixture_matches_current_aggregation(tmp_path: Path) -> None:
    expected = CROSSCHECK_FIXTURE.read_text(encoding="utf-8")
    assert render_crosscheck_fixture(tmp_path) == expected, (
        "Regenerate with: uv run python tests/report_fixtures.py"
    )
    for case in json.loads(expected)["cases"]:
        serialized = json.dumps(case["dataset"])
        assert CANARY not in serialized
        assert PATH_CANARY not in serialized


def test_daily_report_attributes_semantics_without_inventing_zeroes(
    engine: Engine,
) -> None:
    report = aggregate_usage(engine, UsageQuery())
    rows = {row.period: row.metrics for row in report.rows}

    assert list(rows) == [
        "2026-08-03",
        "2026-08-04",
        "2026-08-05",
        "2026-08-10",
        "2026-08-12",
        "2026-08-20",
        "2026-08-21",
        None,
    ]
    first = rows["2026-08-03"]
    assert first.tokens is not None
    assert (first.tokens.input, first.tokens.cache_read, first.tokens.total) == (
        5_500,
        4_000,
        6_000,
    )
    assert first.cache_rate == pytest.approx(4_000 / 5_500)
    # The in-progress turn's call counts as a call but not as tokens.
    second = rows["2026-08-04"]
    assert (second.calls, second.turns, second.conversations) == (2, 1, 0)
    assert second.tokens is not None
    assert second.tokens.total == 3_505
    assert rows["2026-08-12"].flags == ("conversation-aggregate",)
    assert rows["2026-08-20"].flags == ("context-snapshot",)
    unavailable = rows["2026-08-21"]
    assert unavailable.tokens is None
    assert unavailable.cache_rate is None
    assert unavailable.flags == ("tokens-unavailable",)
    assert (unavailable.calls, unavailable.turns) == (1, 1)
    undated = rows[None]
    assert (undated.conversations, undated.turns, undated.calls) == (1, 1, 0)

    totals = report.totals
    assert totals.tokens is not None
    assert totals.tokens.total == 92_505
    assert (totals.conversations, totals.turns, totals.calls) == (6, 8, 8)
    assert totals.flags == (
        "conversation-aggregate",
        "context-snapshot",
        "partial-tokens",
    )
    for field in ("conversations", "turns", "calls"):
        assert sum(getattr(row, field) for row in rows.values()) == getattr(
            totals, field
        )


def test_timezone_moves_period_boundaries_and_plain_dates(engine: Engine) -> None:
    utc = _rows(engine)
    paris = _rows(engine, timezone="Europe/Paris")

    assert "2026-08-03" in utc
    assert "2026-08-03" not in paris
    paris_day = paris["2026-08-04"]
    assert paris_day.tokens is not None
    assert paris_day.tokens.total == 6_000 + 3_505

    window = parse_report_window("2026-08-04", "2026-08-04", "Europe/Paris")
    assert window.since == datetime(2026, 8, 3, 22, tzinfo=UTC)
    assert window.until == datetime(2026, 8, 4, 22, tzinfo=UTC)
    zoned = parse_report_window("2026-08-04T06:00:00+02:00", None, "America/New_York")
    assert zoned.since == datetime(2026, 8, 4, 4, tzinfo=UTC)
    assert zoned.until is None


@pytest.mark.parametrize(
    ("view", "periods"),
    [
        (ReportView.WEEKLY, ["2026-08-03", "2026-08-10", "2026-08-17", None]),
        (ReportView.MONTHLY, ["2026-08", None]),
    ],
)
def test_weekly_and_monthly_periods(
    engine: Engine, view: ReportView, periods: list[str | None]
) -> None:
    report = aggregate_usage(engine, UsageQuery(view=view))

    assert [row.period for row in report.rows] == periods
    assert report.totals.tokens is not None
    assert report.totals.tokens.total == 92_505


def test_window_selects_only_in_window_activity(engine: Engine) -> None:
    window = parse_report_window("2026-08-04T06:00:00Z", "2026-08-11T00:00:00Z")
    report = aggregate_usage(engine, UsageQuery(window=window))

    assert [row.period for row in report.rows] == [
        "2026-08-04",
        "2026-08-05",
        "2026-08-10",
    ]
    # A conversation started before the window is attributed to its first period.
    assert report.rows[0].metrics.conversations == 1
    assert report.totals.tokens is not None
    assert report.totals.tokens.total == 3_505 + 41_000 + 24_300


def test_session_view_hides_identifiers_and_orders_sessions(engine: Engine) -> None:
    report = aggregate_usage(engine, UsageQuery(view=ReportView.SESSION))
    sessions = [row.session for row in report.rows]

    assert [session.number for session in sessions if session] == [1, 2, 3, 4, 5, 6]
    first = sessions[0]
    assert first is not None
    assert first.started_at == "2026-08-03T23:30:00+00:00"
    assert (first.provider, first.project, first.models) == (
        "codex",
        "alpha",
        ("gpt-x", "gpt-y"),
    )
    assert sessions[-1] is not None and sessions[-1].started_at is None
    assert report.rows[0].metrics.turns == 3
    assert report.totals.tokens is not None
    assert sum(
        row.metrics.tokens.total for row in report.rows if row.metrics.tokens
    ) == (report.totals.tokens.total)
    serialized = json.dumps(report.to_dict())
    assert CANARY not in serialized
    assert PATH_CANARY not in serialized


@pytest.mark.parametrize("breakdown", list(Breakdown))
def test_breakdown_tokens_and_calls_sum_to_each_period(
    engine: Engine, breakdown: Breakdown
) -> None:
    report = aggregate_usage(engine, UsageQuery(breakdown=breakdown))

    for row in report.rows:
        # Model rows exist only where a model was used.
        assert row.breakdown or (breakdown is Breakdown.MODEL and not row.metrics.calls)
        assert sum(item.metrics.calls for item in row.breakdown) == row.metrics.calls
        if row.metrics.tokens is not None:
            assert sum(
                item.metrics.tokens.total
                for item in row.breakdown
                if item.metrics.tokens is not None
            ) == (row.metrics.tokens.total)
        if breakdown is not Breakdown.MODEL:
            assert (
                sum(item.metrics.turns for item in row.breakdown) == row.metrics.turns
            )
            assert (
                sum(item.metrics.conversations for item in row.breakdown)
                == row.metrics.conversations
            )


def test_model_breakdown_matches_model_filters(engine: Engine) -> None:
    breakdown = aggregate_usage(engine, UsageQuery(breakdown=Breakdown.MODEL))
    by_model: dict[str, list[int]] = {}
    for row in breakdown.rows:
        for item in row.breakdown:
            totals = by_model.setdefault(item.label, [0, 0, 0, 0])
            totals[0] += item.metrics.tokens.total if item.metrics.tokens else 0
            totals[1] += item.metrics.calls
            totals[2] += item.metrics.turns
            totals[3] += item.metrics.conversations

    for model, (tokens, calls, turns, conversations) in by_model.items():
        filtered = aggregate_usage(
            engine, UsageQuery(filters=ReportFilters(models=(model,)))
        ).totals
        assert (filtered.tokens.total if filtered.tokens else 0) == tokens, model
        assert (filtered.calls, filtered.turns) == (calls, turns), model
        assert filtered.conversations <= conversations, model


def test_filters_resolve_aliases_and_reject_unknown_providers() -> None:
    filters = report_filters(
        providers=["claude-code", "claude", "codex"], models=["a", "a"]
    )

    assert filters.providers == ("claude", "codex")
    assert filters.models == ("a",)
    with pytest.raises(UsageQueryError) as error:
        report_filters(providers=[f"{CANARY}-provider"])
    assert error.value.code == "unknown_provider"
    assert CANARY not in str(error.value)


@pytest.mark.parametrize(
    "name", ["", "../../etc/passwd", "/etc/localtime", "Mars/Base", "x" * 80]
)
def test_invalid_timezones_are_rejected_without_echo(name: str) -> None:
    with pytest.raises(UsageQueryError) as error:
        resolve_timezone(name)

    assert error.value.code == "invalid_timezone"
    assert str(error.value) == "invalid_timezone"


@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-02-30", None),
        ("yesterday", None),
        ("2026-08-04T10:00:00", None),
        ("2026-08-05", "2026-08-04"),
    ],
)
def test_invalid_windows_use_a_fixed_code(since: str, until: str | None) -> None:
    with pytest.raises(UsageQueryError) as error:
        parse_report_window(since, until)

    assert error.value.code == "invalid_window"


def test_json_contract_is_versioned_and_deterministic(engine: Engine) -> None:
    query = UsageQuery(view=ReportView.SESSION, breakdown=Breakdown.MODEL)
    first = aggregate_usage(engine, query).to_dict()
    second = aggregate_usage(engine, query).to_dict()

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert set(first) == {
        "breakdown",
        "filters",
        "notice",
        "rows",
        "schema",
        "schema_version",
        "share_safe",
        "timezone",
        "totals",
        "view",
        "window",
    }
    assert first["schema"] == "cli-consumption/usage-report"
    assert first["schema_version"] == REPORT_SCHEMA_VERSION == 1
    assert "not billing data" in first["notice"]
    assert set(first["totals"]) == ROW_KEYS - {"breakdown", "period", "session"}
    for row in first["rows"]:
        assert set(row) == ROW_KEYS
        assert set(row["session"]) == {
            "machine",
            "models",
            "number",
            "project",
            "provider",
            "started_at",
        }
        if row["tokens"] is not None:
            assert set(row["tokens"]) == TOKEN_KEYS
        for item in row["breakdown"]:
            assert set(item) == (ROW_KEYS - {"breakdown", "period", "session"}) | {
                "label"
            }
    unavailable = [row for row in first["rows"] if row["tokens"] is None]
    assert unavailable
    assert all(row["cache_rate"] is None for row in unavailable)


def test_share_safe_reuses_dashboard_pseudonyms(engine: Engine) -> None:
    window = ExportWindow()
    filters = ReportFilters()
    with _dashboard_snapshot(engine) as connection:
        context = _dashboard_context(
            connection, share_safe=True, window=window, filters=filters
        )
    report = aggregate_usage(
        engine,
        UsageQuery(view=ReportView.SESSION, breakdown=Breakdown.MODEL, share_safe=True),
    )
    payload = report.to_dict()
    serialized = json.dumps(payload)

    sessions = [row["session"] for row in payload["rows"]]
    assert {session["project"] for session in sessions} <= set(
        context.projects.values()
    )
    assert {session["machine"] for session in sessions} <= set(
        context.machines.values()
    )
    labels = {item["label"] for row in payload["rows"] for item in row["breakdown"]}
    assert labels <= set(context.models.values())
    assert all(
        len(session["started_at"]) == 10
        for session in sessions
        if session["started_at"]
    )
    for private in (*PRIVATE_LABELS, CANARY, PATH_CANARY):
        assert private not in serialized


def test_share_safe_filters_and_labels_are_pseudonymized(engine: Engine) -> None:
    report = aggregate_usage(
        engine,
        UsageQuery(
            breakdown=Breakdown.PROJECT,
            filters=ReportFilters(projects=("beta",), machines=("laptop",)),
            share_safe=True,
            window=parse_report_window("2026-08-01T10:15:00Z", None),
        ),
    ).to_dict()

    assert report["filters"]["projects"] == ["project-1"]
    assert report["filters"]["machines"] == ["machine-1"]
    assert report["window"]["since"] == "2026-08-01T00:00:00.000000+00:00"
    serialized = json.dumps(report)
    assert "beta" not in serialized
    assert "laptop" not in serialized


def test_report_is_idempotent_after_repeated_ingestion(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        seed_report_database(engine)
        before = aggregate_usage(engine, UsageQuery(breakdown=Breakdown.PROVIDER))
        seed_report_database(engine)
        after = aggregate_usage(engine, UsageQuery(breakdown=Breakdown.PROVIDER))
    finally:
        engine.dispose()

    assert after.to_dict() == before.to_dict()


def test_report_reads_without_collecting_or_writing(engine: Engine) -> None:
    with engine.connect() as connection:
        runs = connection.scalar(select(func.count()).select_from(IngestionRun))

    aggregate_usage(engine, UsageQuery())

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(IngestionRun)) == runs


def test_empty_database_reports_measured_zeroes(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "empty.sqlite")
    try:
        report = aggregate_usage(engine, UsageQuery())
    finally:
        engine.dispose()

    assert report.rows == ()
    assert report.totals.tokens is not None
    assert report.totals.tokens.total == 0
    assert report.totals.cache_rate is None


def test_postgresql_report_matches_sqlite_when_configured(tmp_path: Path) -> None:
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if database_url is None:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    schema_name = f"usage_report_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    scoped_engine = None
    schema_created = False
    sqlite_engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        scoped_engine = create_engine(
            make_url(database_url).update_query_dict(
                {"options": f"-csearch_path={schema_name}"}
            )
        )
        for snapshot in report_snapshots():
            ingest_snapshot(scoped_engine, snapshot)
            ingest_snapshot(sqlite_engine, snapshot)
        queries = [
            UsageQuery(view=view, breakdown=breakdown, filters=filters)
            for view in ReportView
            for breakdown in (None, Breakdown.MODEL)
            for filters in (ReportFilters(), ReportFilters(models=("gpt-y",)))
        ]
        queries.append(
            UsageQuery(
                window=parse_report_window("2026-08-04", "2026-08-12", "Europe/Paris"),
                share_safe=True,
                breakdown=Breakdown.MACHINE,
            )
        )
        for query in queries:
            assert (
                aggregate_usage(scoped_engine, query).to_dict()
                == aggregate_usage(sqlite_engine, query).to_dict()
            )
    finally:
        sqlite_engine.dispose()
        if scoped_engine is not None:
            scoped_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()


def _seeded(tmp_path: Path, name: str) -> Engine:
    from report_fixtures import seed_crosscheck_database

    engine = create_database_engine(tmp_path / f"{name}.sqlite")
    seed_crosscheck_database(engine, name)
    return engine


def test_dateless_untimed_counters_follow_the_dashboard_range(tmp_path: Path) -> None:
    dated = _seeded(tmp_path, "dateless-untimed")
    dateless = _seeded(tmp_path, "all-dateless")
    try:
        with_range = aggregate_usage(dated, UsageQuery()).totals
        without_range = aggregate_usage(dateless, UsageQuery()).totals
    finally:
        dated.dispose()
        dateless.dispose()

    # Ingestion runs and the dated conversation give the dashboard a range, so
    # dateless conversation-aggregate and context-snapshot counters are excluded.
    assert (with_range.conversations, with_range.calls) == (1, 0)
    assert with_range.tokens is not None and with_range.tokens.total == 0
    # Without any timestamp, the dashboard has no range and keeps every counter.
    assert (without_range.conversations, without_range.calls) == (3, 2)
    assert without_range.tokens is not None and without_range.tokens.total == 400


def test_share_safe_selects_on_utc_days_like_the_dashboard(tmp_path: Path) -> None:
    engine = _seeded(tmp_path, "intraday")
    window = parse_report_window("2026-08-10T13:00:00Z", "2026-08-10T22:00:00Z")
    try:
        detailed = aggregate_usage(engine, UsageQuery(window=window))
        shared = aggregate_usage(
            engine,
            UsageQuery(window=window, share_safe=True, timezone="America/New_York"),
        )
    finally:
        engine.dispose()

    assert detailed.totals.tokens is not None
    assert shared.totals.tokens is not None
    assert (detailed.totals.calls, detailed.totals.tokens.total) == (1, 200)
    assert (shared.totals.calls, shared.totals.tokens.total) == (2, 300)
    assert shared.timezone == "UTC"
    assert [row.period for row in shared.rows] == ["2026-08-10"]


def test_timestamps_beyond_the_local_calendar_are_undated(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "edge.sqlite")
    snapshot = next(item for item in report_snapshots() if item.provider == "claude")
    edge = "9999-12-31T23:00:00+00:00"
    for record in (*snapshot.conversations, *snapshot.turns, *snapshot.model_calls):
        for field in ("started_at", "ended_at", "timestamp"):
            if record.get(field):
                record[field] = edge
    try:
        ingest_snapshot(engine, snapshot)
        daily = aggregate_usage(engine, UsageQuery(timezone="Pacific/Kiritimati"))
        sessions = aggregate_usage(
            engine,
            UsageQuery(view=ReportView.SESSION, timezone="Pacific/Kiritimati"),
        )
    finally:
        engine.dispose()

    assert [row.period for row in daily.rows] == [None]
    assert daily.totals.calls == 1
    assert sessions.rows[0].session is not None
    assert sessions.rows[0].session.started_at is None
