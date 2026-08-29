from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert

from cli_consumption.exporting import _serialize_cell, export_csv
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
