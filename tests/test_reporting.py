from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from cli_consumption.dashboard import _dashboard_payload
from cli_consumption.exporting import export_csv
from cli_consumption.reporting import (
    ExportWindow,
    parse_export_window,
    report_statement,
)
from cli_consumption.storage import (
    Conversation,
    IngestionRun,
    Subagent,
    Turn,
    create_database_engine,
    initialize_database,
    read_table,
)


def _conversation(
    identifier: str, started_at: str | None, ended_at: str | None
) -> Conversation:
    return Conversation(
        id=f"codex:{identifier}",
        provider="codex",
        external_id=identifier,
        source_machine="machine",
        project="project",
        project_source="none",
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=None,
        source="rollout",
        models_json="[]",
        iterations=1,
        model_calls=0,
        tool_calls=0,
        compactions=0,
        event_count=1,
        content_hash="0" * 64,
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_input_tokens=0,
        uncached_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        visible_output_tokens=0,
        unattributed_tokens=0,
        total_tokens=0,
    )


def _turn(identifier: str) -> Turn:
    return Turn(
        id=f"codex:{identifier}:turn",
        conversation_id=f"codex:{identifier}",
        external_id="turn",
        started_at=None,
        ended_at=None,
        status="completed",
        duration_ms=None,
        time_to_first_token_ms=None,
        model_calls=0,
        tool_calls=0,
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_input_tokens=0,
        uncached_input_tokens=0,
        output_tokens=0,
        reasoning_output_tokens=0,
        visible_output_tokens=0,
        unattributed_tokens=0,
        total_tokens=0,
    )


def _run(identifier: str, ingested_at: str) -> IngestionRun:
    return IngestionRun(
        id=identifier,
        provider="codex",
        ingested_at=ingested_at,
        conversations_received=0,
        conversations_written=0,
        conversations_skipped=0,
        malformed_records=0,
        duplicate_conversations=0,
    )


def test_export_window_parses_dates_offsets_and_boundaries() -> None:
    window = parse_export_window("2026-06-01", "2026-06-30")
    assert window == ExportWindow(
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert parse_export_window("2026-06-01T02:00:00+02:00").since == datetime(
        2026, 6, 1, tzinfo=UTC
    )
    precise = parse_export_window("2026-06-01T10:00:00Z", "2026-06-02T10:00:00Z")
    assert precise.metadata(day_precision=True) == {
        "since": "2026-06-01T00:00:00+00:00",
        "until": "2026-06-03T00:00:00+00:00",
    }
    with pytest.raises(ValueError, match="timezone"):
        parse_export_window("2026-06-01T00:00:00")
    with pytest.raises(ValueError, match="earlier"):
        parse_export_window("2026-06-02", "2026-06-01")


def test_window_selects_overlaps_complete_graph_subagents_and_runs(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "reporting.sqlite")
    initialize_database(engine)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                _conversation(
                    "overlap",
                    "2026-05-31T22:00:00Z",
                    "2026-06-01T01:00:00+00:00",
                ),
                _conversation(
                    "inside",
                    "2026-06-15T00:00:00+00:00",
                    "2026-06-15T01:00:00+00:00",
                ),
                _conversation(
                    "after",
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-01T01:00:00+00:00",
                ),
                _conversation("unknown", None, None),
                _turn("overlap"),
                _turn("inside"),
                _turn("after"),
                Subagent(
                    id="selected-edge",
                    provider="codex",
                    source_machine="machine",
                    parent_thread_id="overlap",
                    child_thread_id="after",
                    status="completed",
                    created_at_ms=None,
                    updated_at_ms=None,
                    agent_role="worker",
                    tokens_used=0,
                ),
                Subagent(
                    id="excluded-edge",
                    provider="codex",
                    source_machine="machine",
                    parent_thread_id="after",
                    child_thread_id="unknown",
                    status="completed",
                    created_at_ms=None,
                    updated_at_ms=None,
                    agent_role="worker",
                    tokens_used=0,
                ),
                _run("inside-run", "2026-06-20T00:00:00+00:00"),
                _run("after-run", "2026-07-01T00:00:00+00:00"),
            ]
        )

    window = parse_export_window("2026-06-01", "2026-06-30")
    output = tmp_path / "csv"
    export_csv(engine, output, window=window)
    with (output / "conversations.csv").open(encoding="utf-8") as handle:
        assert [row["id"] for row in csv.DictReader(handle)] == [
            "codex:inside",
            "codex:overlap",
        ]
    with (output / "turns.csv").open(encoding="utf-8") as handle:
        assert [row["conversation_id"] for row in csv.DictReader(handle)] == [
            "codex:inside",
            "codex:overlap",
        ]
    with (output / "subagents.csv").open(encoding="utf-8") as handle:
        assert [row["id"] for row in csv.DictReader(handle)] == ["selected-edge"]
    with (output / "ingestion_runs.csv").open(encoding="utf-8") as handle:
        assert [row["id"] for row in csv.DictReader(handle)] == ["inside-run"]

    payload = _dashboard_payload(engine, window=window)
    assert payload["meta"]["exportWindow"] == window.metadata()
    assert len(payload["conversations"]) == 2
    assert len(payload["turns"]) == 2
    assert len(payload["subagents"]) == 1
    engine.dispose()


def test_unbounded_reads_and_csv_rows_have_stable_primary_key_order(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "ordered.sqlite")
    initialize_database(engine)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                _run("z-run", "2026-01-01T00:00:00+00:00"),
                _run("a-run", "2026-01-01T00:00:00+00:00"),
            ]
        )
    assert [row["id"] for row in read_table(engine, "ingestion_runs")] == [
        "a-run",
        "z-run",
    ]
    first = tmp_path / "first"
    second = tmp_path / "second"
    export_csv(engine, first)
    export_csv(engine, second)
    assert (first / "ingestion_runs.csv").read_bytes() == (
        second / "ingestion_runs.csv"
    ).read_bytes()
    engine.dispose()


def test_window_queries_compile_for_postgresql() -> None:
    connection = cast(
        Connection,
        create_mock_engine("postgresql+psycopg://", lambda *_: None).connect(),
    )
    window = parse_export_window("2026-01-01", "2026-01-31")
    for table_name in ("conversations", "turns", "subagents", "ingestion_runs"):
        statement = report_statement(connection, table_name, window)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "ORDER BY" in sql
        assert "TIMESTAMP WITH TIME ZONE" in sql
