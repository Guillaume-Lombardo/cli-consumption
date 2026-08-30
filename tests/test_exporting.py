from __future__ import annotations

import csv
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert

import cli_consumption.exporting as exporting_module
from cli_consumption.exporting import _atomic_write_csv, _serialize_cell, export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    initialize_database,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("+cmd", "'+cmd"),
        ("-cmd", "'-cmd"),
        ("@SUM(A1:A2)", "'@SUM(A1:A2)"),
        (
            '  =HYPERLINK("https://example.test")',
            '\'  =HYPERLINK("https://example.test")',
        ),
        ("\u2003+cmd", "'\u2003+cmd"),
        ("\tvalue", "'\tvalue"),
        ("\rvalue", "'\rvalue"),
        ("\nvalue", "'\nvalue"),
        (" \tvalue", "' \tvalue"),
    ],
)
def test_serialize_neutralizes_spreadsheet_formula_prefixes(
    value: str, expected: str
) -> None:
    assert _serialize_cell(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "safe", "  safe", "'=already-text", -1, 1, 1.5, False],
)
def test_serialize_preserves_safe_and_non_string_values(value: Any) -> None:
    assert _serialize_cell(value) == value


def test_serialize_preserves_none_as_an_empty_csv_cell() -> None:
    assert _serialize_cell(None) == ""


def test_export_csv_neutralizes_values_already_present_in_a_database(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "legacy.sqlite")
    initialize_database(engine)
    canary = "CSV_FORMULA_CANARY_DO_NOT_EXECUTE"
    malicious_provider = f'  =HYPERLINK("https://example.test/{canary}")'
    ingestion_runs = TABLES["ingestion_runs"]
    with engine.begin() as connection:
        connection.execute(
            insert(ingestion_runs).values(
                id="legacy-run",
                provider=malicious_provider,
                ingested_at="2026-08-29T00:00:00+00:00",
                conversations_received=0,
                conversations_written=0,
                conversations_skipped=0,
                malformed_records=0,
                duplicate_conversations=0,
            )
        )

    output = tmp_path / "csv"
    paths = export_csv(engine, output)

    assert len(paths) == len(TABLES)
    with (output / "ingestion_runs.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["provider"] == f"'{malicious_provider}"
    assert rows[0]["conversations_received"] == "0"
    engine.dispose()


def test_csv_write_failure_preserves_existing_file_and_cleans_owned_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "usage.sqlite")
    initialize_database(engine)
    output = tmp_path / "csv"
    output.mkdir()
    existing = output / "conversations.csv"
    existing.write_text("existing csv\n", encoding="utf-8")
    stale = output / ".conversations.csv.stale.tmp"
    stale.write_text("unrelated temporary\n", encoding="utf-8")

    def fail_write(handle, *_args) -> None:
        handle.write("partial csv\n")
        raise OSError("synthetic write failure")

    monkeypatch.setattr(exporting_module, "_write_csv", fail_write)

    with pytest.raises(OSError, match="synthetic write failure"):
        export_csv(engine, output)

    assert existing.read_text(encoding="utf-8") == "existing csv\n"
    assert list(output.glob(".conversations.csv.*.tmp")) == [stale]
    assert stale.read_text(encoding="utf-8") == "unrelated temporary\n"
    engine.dispose()


def test_each_csv_fsyncs_then_atomically_replaces_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "usage.sqlite")
    initialize_database(engine)
    output = tmp_path / "csv"
    output.mkdir()
    (output / "conversations.csv").write_text("old csv\n", encoding="utf-8")
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

    monkeypatch.setattr(exporting_module.os, "fsync", tracking_fsync)
    monkeypatch.setattr(exporting_module.os, "replace", tracking_replace)

    paths = export_csv(engine, output)

    assert len(paths) == len(TABLES)
    assert events == ["file-fsync", "replace", "directory-fsync"] * len(TABLES)
    assert (
        (output / "conversations.csv")
        .read_text(encoding="utf-8")
        .startswith("id,provider,")
    )
    assert list(output.glob(".*.csv.*.tmp")) == []
    engine.dispose()


def test_atomic_csv_replacement_preserves_existing_file_mode(tmp_path: Path) -> None:
    output = tmp_path / "conversations.csv"
    output.write_text("old csv\n", encoding="utf-8")
    output.chmod(0o640)

    def write_csv(handle) -> None:
        handle.write("new csv\n")

    _atomic_write_csv(output, write_csv)

    assert output.read_text(encoding="utf-8") == "new csv\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o640


def test_later_sql_failure_preserves_that_table_but_not_the_whole_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "usage.sqlite")
    initialize_database(engine)
    output = tmp_path / "csv"
    output.mkdir()
    conversations = output / "conversations.csv"
    turns = output / "turns.csv"
    conversations.write_text("old conversations\n", encoding="utf-8")
    turns.write_text("old turns\n", encoding="utf-8")
    real_iter_report_rows = exporting_module.iter_report_rows

    def fail_turns(connection, table_name, window):
        if table_name == "turns":
            raise RuntimeError("synthetic SQL failure")
        yield from real_iter_report_rows(connection, table_name, window)

    monkeypatch.setattr(exporting_module, "iter_report_rows", fail_turns)

    with pytest.raises(RuntimeError, match="synthetic SQL failure"):
        export_csv(engine, output)

    assert conversations.read_text(encoding="utf-8") != "old conversations\n"
    assert turns.read_text(encoding="utf-8") == "old turns\n"
    assert list(output.glob(".*.csv.*.tmp")) == []
    engine.dispose()


def test_replace_failure_preserves_existing_csv_and_removes_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "conversations.csv"
    output.write_text("old csv\n", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("synthetic replace failure")

    def write_csv(handle) -> None:
        handle.write("new csv\n")

    monkeypatch.setattr(exporting_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        _atomic_write_csv(output, write_csv)

    assert output.read_text(encoding="utf-8") == "old csv\n"
    assert list(tmp_path.glob(".conversations.csv.*.tmp")) == []
