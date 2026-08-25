from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from cli_consumption.storage import TABLES, read_table


def export_csv(engine: Engine, output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for table_name in TABLES:
        rows = read_table(engine, table_name)
        path = output / f"{table_name}.csv"
        fieldnames = list(TABLES[table_name].__table__.columns.keys())
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(_serialize(rows))
        written.append(path)
    return written


def _serialize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: "" if value is None else value for key, value in row.items()}
        for row in rows
    ]
