from __future__ import annotations

import csv
import errno
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, cast

from sqlalchemy.engine import Connection, Engine

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
            _atomic_write_csv(
                path,
                lambda handle, table_name=table_name, fieldnames=fieldnames: _write_csv(
                    handle, connection, table_name, fieldnames, window
                ),
            )
            written.append(path)
    return written


def _write_csv(
    handle: TextIO,
    connection: Connection,
    table_name: str,
    fieldnames: list[str],
    window: ExportWindow | None,
) -> None:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for row in iter_report_rows(connection, table_name, window):
        writer.writerow({key: _serialize_cell(value) for key, value in row.items()})


def _atomic_write_csv(output: Path, writer: Callable[[TextIO], None]) -> None:
    temporary_path: Path | None = None
    try:
        existing_mode = stat.S_IMODE(output.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            writer(cast(TextIO, handle))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
        _fsync_directory(output.parent)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP}:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise
    finally:
        os.close(descriptor)


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
