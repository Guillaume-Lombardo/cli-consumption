from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from cli_consumption.reporting import ExportWindow, iter_report_rows
from cli_consumption.storage import TABLES, initialize_database

FORMULA_PREFIXES = ("=", "+", "-", "@")
CONTROL_PREFIXES = ("\t", "\r", "\n")


def export_csv(
    engine: Engine,
    output: Path,
    *,
    window: ExportWindow | None = None,
) -> list[Path]:
    initialize_database(engine)
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with engine.connect() as connection:
        for table_name in TABLES:
            path = output / f"{table_name}.csv"
            fieldnames = list(TABLES[table_name].__table__.columns.keys())
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in iter_report_rows(connection, table_name, window):
                    writer.writerow(
                        {key: _serialize_cell(value) for key, value in row.items()}
                    )
            written.append(path)
    return written


def _serialize_cell(value: Any) -> Any:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value

    candidate = value
    while candidate and candidate[0].isspace() and candidate[0] not in CONTROL_PREFIXES:
        candidate = candidate[1:]
    if candidate.startswith(FORMULA_PREFIXES + CONTROL_PREFIXES):
        return f"'{value}"
    return value
