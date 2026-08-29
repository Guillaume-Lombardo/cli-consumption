from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, create_mock_engine, event, insert, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import make_url
from sqlalchemy.schema import CreateSchema, DropSchema
from typer.testing import CliRunner

import cli_consumption.dashboard as dashboard_module
from cli_consumption.cli import app
from cli_consumption.dashboard import (
    DashboardLimitError,
    _atomic_write_text,
    _dashboard_snapshot,
    _fsync_directory,
    _preflight_dashboard,
    generate_dashboard,
)
from cli_consumption.reporting import (
    ExportWindow,
    ReportEstimate,
    estimate_report,
    iter_report_rows,
    report_estimate_statement,
)
from cli_consumption.storage import (
    Conversation,
    create_database_engine,
    initialize_database,
)

runner = CliRunner()


def _conversation(identifier: str, started_at: str) -> dict[str, Any]:
    return {
        "id": f"provider:{identifier}",
        "provider": "provider",
        "external_id": identifier,
        "source_machine": "machine",
        "project": "project",
        "project_source": "fallback",
        "started_at": started_at,
        "ended_at": started_at,
        "duration_seconds": 0.0,
        "source": "synthetic",
        "models_json": "[]",
        "iterations": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "compactions": 0,
        "event_count": 0,
        "content_hash": identifier.ljust(64, "0")[:64],
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "visible_output_tokens": 0,
        "unattributed_tokens": 0,
        "total_tokens": 0,
    }


def _database(tmp_path: Path, *rows: dict[str, Any]):
    engine = create_database_engine(tmp_path / "usage.sqlite")
    initialize_database(engine)
    if rows:
        with engine.begin() as connection:
            connection.execute(insert(Conversation), list(rows))
    return engine


def test_report_preflight_counts_sqlite_rows_and_compiles_for_postgresql(
    tmp_path: Path,
) -> None:
    engine = _database(
        tmp_path,
        _conversation("one", "2026-08-01T00:00:00.000000+00:00"),
        _conversation("two", "2026-09-01T00:00:00.000000+00:00"),
    )
    with engine.connect() as connection:
        estimate = estimate_report(connection)
    assert estimate.records == 2
    assert estimate.scalar_bytes > 0

    mock_engine = create_mock_engine("postgresql+psycopg://", lambda *args: None)
    statement = report_estimate_statement(cast(Connection, mock_engine.connect()))
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "octet_length" in sql
    assert "UNION ALL" in sql
    assert sql.count("count(") == 10
    engine.dispose()


def test_preflight_limits_are_inclusive_at_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    estimate = ReportEstimate(records=5, scalar_bytes=1_000)
    monkeypatch.setattr(dashboard_module, "estimate_report", lambda *_: estimate)
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_RECORDS", 5)
    monkeypatch.setattr(
        dashboard_module,
        "MAX_DASHBOARD_ESTIMATED_BYTES",
        1_000,
    )

    _preflight_dashboard(engine)

    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_RECORDS", 4)
    with pytest.raises(DashboardLimitError, match="dashboard_record_limit_exceeded"):
        _preflight_dashboard(engine)
    engine.dispose()


def test_simulated_accumulated_database_is_rejected_before_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(
        tmp_path,
        *(
            _conversation(f"conversation-{index}", "2026-08-01T00:00:00.000000+00:00")
            for index in range(3)
        ),
    )
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_RECORDS", 2)
    monkeypatch.setattr(
        dashboard_module,
        "_dashboard_payload",
        lambda *_args, **_kwargs: pytest.fail("payload was materialized"),
    )

    with pytest.raises(DashboardLimitError, match="dashboard_record_limit_exceeded"):
        generate_dashboard(engine, tmp_path / "dashboard.html")
    engine.dispose()


def test_required_relationship_indexes_have_a_separate_memory_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(
        tmp_path, _conversation("one", "2026-08-01T00:00:00.000000+00:00")
    )
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_INDEX_BYTES", 1)

    with pytest.raises(DashboardLimitError, match="dashboard_index_limit_exceeded"):
        generate_dashboard(engine, tmp_path / "dashboard.html")

    assert not (tmp_path / "dashboard.html").exists()
    engine.dispose()


def test_preflight_rejects_estimated_size_before_payload_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    monkeypatch.setattr(
        dashboard_module,
        "estimate_report",
        lambda *_: ReportEstimate(records=1, scalar_bytes=10),
    )
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_ESTIMATED_BYTES", 9)
    monkeypatch.setattr(
        dashboard_module,
        "_dashboard_payload",
        lambda *_args, **_kwargs: pytest.fail("payload was materialized"),
    )

    with pytest.raises(
        DashboardLimitError, match="dashboard_estimated_size_limit_exceeded"
    ):
        generate_dashboard(engine, tmp_path / "dashboard.html")
    engine.dispose()


def test_encoded_html_limit_preserves_existing_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(tmp_path)
    output = tmp_path / "dashboard.html"
    output.write_text("old dashboard", encoding="utf-8")
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_HTML_BYTES", 1)

    with pytest.raises(DashboardLimitError, match="dashboard_html_limit_exceeded"):
        generate_dashboard(engine, output)

    assert output.read_text(encoding="utf-8") == "old dashboard"
    engine.dispose()


def test_production_path_streams_many_rows_without_materialized_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(
        tmp_path,
        *(
            _conversation(f"narrow-{index}", "2026-08-01T00:00:00.000000+00:00")
            for index in range(200)
        ),
    )
    monkeypatch.setattr(
        dashboard_module,
        "_dashboard_payload",
        lambda *_args, **_kwargs: pytest.fail("global payload was materialized"),
    )

    output = tmp_path / "streamed.html"
    generate_dashboard(engine, output)

    assert output.stat().st_size > 0
    assert '"conversations":[' in output.read_text(encoding="utf-8")
    engine.dispose()


def test_streaming_json_uses_ascii_escapes_and_script_safe_unicode(
    tmp_path: Path,
) -> None:
    row = _conversation("unicode", "2026-08-01T00:00:00.000000+00:00")
    row["project"] = "café <&>"
    engine = _database(tmp_path, row)

    output = tmp_path / "unicode.html"
    generate_dashboard(engine, output)
    html = output.read_text(encoding="utf-8")

    assert "café <&>" not in html
    assert "caf\\u00e9 \\u003c\\u0026\\u003e" in html
    engine.dispose()


def test_sqlite_snapshot_begins_explicitly_before_preflight(tmp_path: Path) -> None:
    engine = _database(
        tmp_path, _conversation("one", "2026-08-01T00:00:00.000000+00:00")
    )
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    generate_dashboard(engine, tmp_path / "dashboard.html")

    begin_index = next(
        index
        for index, statement in enumerate(statements)
        if statement == "BEGIN DEFERRED"
    )
    preflight_index = next(
        index for index, statement in enumerate(statements) if "UNION ALL" in statement
    )
    assert begin_index < preflight_index
    engine.dispose()


def test_sqlite_concurrent_commit_does_not_change_dashboard_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(
        tmp_path, _conversation("original", "2026-08-01T00:00:00.000000+00:00")
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    preflight_complete = threading.Event()
    writer_complete = threading.Event()
    real_estimate = dashboard_module.estimate_report

    def estimate_then_wait(connection, window):
        estimate = real_estimate(connection, window)
        preflight_complete.set()
        assert writer_complete.wait(timeout=5)
        return estimate

    def insert_concurrently() -> None:
        assert preflight_complete.wait(timeout=5)
        with engine.begin() as connection:
            connection.execute(
                insert(Conversation),
                _conversation("concurrent", "2026-09-01T00:00:00.000000+00:00"),
            )
        writer_complete.set()

    monkeypatch.setattr(dashboard_module, "estimate_report", estimate_then_wait)
    writer = threading.Thread(target=insert_concurrently)
    writer.start()
    output = tmp_path / "dashboard.html"
    generate_dashboard(engine, output)
    writer.join(timeout=5)

    html = output.read_text(encoding="utf-8")
    assert "2026-08-01" in html
    assert "2026-09-01" not in html
    engine.dispose()


def test_time_window_can_reduce_a_report_below_the_record_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(
        tmp_path,
        _conversation("inside", "2026-08-10T00:00:00.000000+00:00"),
        _conversation("outside", "2026-07-10T00:00:00.000000+00:00"),
    )
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_RECORDS", 1)

    with pytest.raises(DashboardLimitError):
        generate_dashboard(engine, tmp_path / "all.html")

    generate_dashboard(
        engine,
        tmp_path / "bounded.html",
        window=ExportWindow(
            since=dashboard_module.datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
            until=dashboard_module.datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
        ),
    )
    html = (tmp_path / "bounded.html").read_text(encoding="utf-8")
    assert "2026-08-10" in html
    assert "2026-07-10" not in html
    engine.dispose()


def test_atomic_replace_preserves_old_dashboard_and_removes_own_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "dashboard.html"
    output.write_text("old dashboard", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(dashboard_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        _atomic_write_text(output, "new dashboard")

    assert output.read_text(encoding="utf-8") == "old dashboard"
    assert list(tmp_path.glob(".dashboard.html.*.tmp")) == []


def test_atomic_write_fsyncs_file_then_replaces_then_fsyncs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracking_fsync(descriptor: int) -> None:
        events.append(
            "directory-fsync"
            if stat.S_ISDIR(os.fstat(descriptor).st_mode)
            else "file-fsync"
        )
        real_fsync(descriptor)

    def tracking_replace(source: Path, target: Path) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(dashboard_module.os, "fsync", tracking_fsync)
    monkeypatch.setattr(dashboard_module.os, "replace", tracking_replace)

    _atomic_write_text(tmp_path / "dashboard.html", "dashboard")

    assert events == ["file-fsync", "replace", "directory-fsync"]


def test_directory_fsync_has_explicit_unsupported_platform_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(dashboard_module.os, "O_DIRECTORY", raising=False)
    monkeypatch.setattr(
        dashboard_module.os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("directory was opened"),
    )

    _fsync_directory(tmp_path)


def test_stale_temp_does_not_block_or_get_deleted(tmp_path: Path) -> None:
    output = tmp_path / "dashboard.html"
    stale = tmp_path / ".dashboard.html.stale.tmp"
    stale.write_text("unrelated stale file", encoding="utf-8")

    _atomic_write_text(output, "new dashboard")

    assert output.read_text(encoding="utf-8") == "new dashboard"
    assert stale.read_text(encoding="utf-8") == "unrelated stale file"


def test_cli_limit_error_is_deterministic_and_does_not_leak_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "SECRET_DASHBOARD_LIMIT_CANARY"
    row = _conversation("canary", "2026-08-10T00:00:00.000000+00:00")
    row["project"] = canary
    engine = _database(tmp_path, row)
    engine.dispose()
    output = tmp_path / "private-output"
    output.mkdir()
    dashboard = output / "dashboard.html"
    dashboard.write_text("old dashboard", encoding="utf-8")
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_RECORDS", 0)
    command = [
        "export",
        "--database",
        str(tmp_path / "usage.sqlite"),
        "--output",
        str(output),
        "--json",
    ]

    first = runner.invoke(app, command)
    second = runner.invoke(app, command)

    assert first.exit_code == 2
    assert first.output == second.output
    assert json.loads(first.stdout) == {
        "error": {
            "code": "dashboard_limit_exceeded",
            "hint": "narrow the export with --since and/or --until",
        }
    }
    assert first.stderr == ""
    assert canary not in first.output
    assert str(output) not in first.output
    assert dashboard.read_text(encoding="utf-8") == "old dashboard"

    text_command = [item for item in command if item != "--json"]
    text_result = runner.invoke(app, text_command, color=True)
    assert text_result.exit_code == 2
    assert text_result.stdout == ""
    assert text_result.stderr == (
        "Dashboard exceeds safe generation limits; narrow the export with "
        "--since and/or --until.\n"
    )


def test_csv_and_dashboard_export_is_not_directory_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _database(
        tmp_path, _conversation("one", "2026-08-10T00:00:00.000000+00:00")
    )
    engine.dispose()
    output = tmp_path / "reports"
    output.mkdir()
    dashboard = output / "dashboard.html"
    dashboard.write_text("old dashboard", encoding="utf-8")
    monkeypatch.setattr(dashboard_module, "MAX_DASHBOARD_RECORDS", 0)

    result = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(tmp_path / "usage.sqlite"),
            "--output",
            str(output),
            "--csv",
        ],
    )

    assert result.exit_code == 2
    assert (output / "conversations.csv").is_file()
    assert dashboard.read_text(encoding="utf-8") == "old dashboard"


def test_postgresql_runtime_preflight_uses_one_repeatable_snapshot(
    tmp_path: Path,
) -> None:
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if database_url is None:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    schema_name = f"dashboard_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    scoped_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        scoped_url = make_url(database_url).update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        )
        scoped_engine = create_engine(scoped_url)
        initialize_database(scoped_engine)
        with scoped_engine.begin() as connection:
            connection.execute(
                insert(Conversation),
                _conversation("original", "2026-08-01T00:00:00.000000+00:00"),
            )

        with _dashboard_snapshot(scoped_engine) as connection:
            assert connection.scalar(text("SHOW transaction_isolation")) == (
                "repeatable read"
            )
            assert estimate_report(connection).records == 1
            with scoped_engine.begin() as writer:
                writer.execute(
                    insert(Conversation),
                    _conversation("concurrent", "2026-09-01T00:00:00.000000+00:00"),
                )
            assert len(list(iter_report_rows(connection, "conversations"))) == 1

        generate_dashboard(scoped_engine, tmp_path / "postgres-dashboard.html")
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
