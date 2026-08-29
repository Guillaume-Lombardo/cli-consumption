from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.models import empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
MAX_PROVIDER_JSON_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_JSONL_BYTES = 256 * 1024 * 1024
MAX_PROVIDER_JSONL_LINE_BYTES = 8 * 1024 * 1024
MAX_PROVIDER_CANDIDATES = 10_000
MAX_PROVIDER_READ_BYTES = 512 * 1024 * 1024
MAX_PROVIDER_SQLITE_BYTES = 512 * 1024 * 1024
MAX_PROVIDER_SQLITE_ROWS = 250_000
MAX_PROVIDER_SQLITE_FIELD_BYTES = 8 * 1024 * 1024
MAX_PROVIDER_SQLITE_FIELDS_BYTES = 256 * 1024 * 1024
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]*")
SAFE_BASIC_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")


class ProviderDataLimitError(RuntimeError):
    """A privacy-safe failure raised when provider input exceeds a hard limit."""


class ProviderInputBudget:
    """Bound untrusted discovery and SQLite work across one collection."""

    def __init__(self) -> None:
        self.candidates = 0
        self.file_bytes = 0
        self.sqlite_file_bytes = 0
        self.sqlite_rows = 0
        self.sqlite_field_bytes = 0

    def sorted_paths(self, paths: Iterable[Path]) -> list[Path]:
        result: list[Path] = []
        for path in paths:
            self.item()
            result.append(path)
        return sorted(result)

    def candidate(self, path: Path) -> Path:
        self.item()
        return path

    def item(self) -> None:
        self.candidates += 1
        if self.candidates > MAX_PROVIDER_CANDIDATES:
            raise ProviderDataLimitError("provider_candidate_limit_exceeded")

    def file_data(self, size: int) -> None:
        self.file_bytes += size
        if self.file_bytes > MAX_PROVIDER_READ_BYTES:
            raise ProviderDataLimitError("provider_read_limit_exceeded")

    def remaining_file_bytes(self) -> int:
        return max(0, MAX_PROVIDER_READ_BYTES - self.file_bytes)

    def sqlite_files(self, size: int) -> None:
        self.sqlite_file_bytes += size
        if self.sqlite_file_bytes > MAX_PROVIDER_SQLITE_BYTES:
            raise ProviderDataLimitError("provider_sqlite_file_too_large")

    def rows(self, rows: Iterable[Any]) -> Iterator[Any]:
        for row in rows:
            self.sqlite_rows += 1
            if self.sqlite_rows > MAX_PROVIDER_SQLITE_ROWS:
                raise ProviderDataLimitError("provider_sqlite_row_limit_exceeded")
            yield row

    def json_field(self, value: Any) -> Any:
        if isinstance(value, str):
            if len(value) > MAX_PROVIDER_SQLITE_FIELD_BYTES:
                raise ProviderDataLimitError("provider_sqlite_field_too_large")
            size = len(value.encode("utf-8"))
        elif isinstance(value, bytes):
            size = len(value)
        else:
            return value
        if size > MAX_PROVIDER_SQLITE_FIELD_BYTES:
            raise ProviderDataLimitError("provider_sqlite_field_too_large")
        self.sqlite_field_bytes += size
        if self.sqlite_field_bytes > MAX_PROVIDER_SQLITE_FIELDS_BYTES:
            raise ProviderDataLimitError("provider_sqlite_fields_too_large")
        return value


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_value(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def label(value: object, maximum: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= maximum and SAFE_LABEL.fullmatch(value) else None


def basic_label(value: object, maximum: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return (
        value
        if 0 < len(value) <= maximum and SAFE_BASIC_LABEL.fullmatch(value)
        else None
    )


def counter(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return min(MAX_BIGINT, max(0, int(value)))


def bounded_sum(*values: int) -> int:
    return min(MAX_BIGINT, sum(values))


def timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float) and math.isfinite(value):
            scale = 1000 if abs(value) > 10_000_000_000 else 1
            return datetime.fromtimestamp(value / scale, UTC)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        pass
    return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def tokens(
    *,
    uncached: object = 0,
    cached: object = 0,
    cache_write: object = 0,
    visible: object = 0,
    reasoning: object = 0,
    total: object = 0,
) -> dict[str, int]:
    uncached_n = counter(uncached)
    cached_n = counter(cached)
    write_n = counter(cache_write)
    visible_n = counter(visible)
    reasoning_n = counter(reasoning)
    input_n = bounded_sum(uncached_n, cached_n, write_n)
    output_n = bounded_sum(visible_n, reasoning_n)
    attributed = bounded_sum(input_n, output_n)
    total_n = max(attributed, counter(total))
    return {
        "input_tokens": input_n,
        "cached_input_tokens": cached_n,
        "cache_write_input_tokens": write_n,
        "output_tokens": output_n,
        "reasoning_output_tokens": reasoning_n,
        "total_tokens": total_n,
        "uncached_input_tokens": uncached_n,
        "visible_output_tokens": visible_n,
        "unattributed_tokens": max(0, total_n - attributed),
    }


def add_tokens(target: dict[str, Any], value: dict[str, int]) -> None:
    for field, amount in value.items():
        target[field] = bounded_sum(int(target[field]), amount)


def new_turn(
    conversation_id: str, external_id: str, started: datetime | None
) -> dict[str, Any]:
    return {
        "id": f"{conversation_id}:{external_id}",
        "conversation_id": conversation_id,
        "external_id": external_id,
        "started_at": iso(started),
        "ended_at": None,
        "status": "in-progress",
        "duration_ms": None,
        "time_to_first_token_ms": None,
        "model_calls": 0,
        "tool_calls": 0,
        **empty_tokens(),
    }


def finish_turn(
    turn: dict[str, Any], ended: datetime | None, status: str = "completed"
) -> None:
    turn["ended_at"] = turn["ended_at"] or iso(ended)
    start = timestamp(turn["started_at"])
    end = timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))
    turn["status"] = status


def project(directory: str | None, mappings: list[tuple[str, str]]) -> tuple[str, str]:
    if directory:
        normalized = directory.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            prefix = prefix.replace("\\", "/").rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return name, "mapping"
    return "outside-project", "none"


def digest_records(records: object) -> str:
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def read_json(path: Path, budget: ProviderInputBudget | None = None) -> object:
    return json.loads(read_bounded_bytes(path, budget))


def read_bounded_bytes(
    path: Path,
    budget: ProviderInputBudget | None = None,
    maximum: int = MAX_PROVIDER_JSON_BYTES,
) -> bytes:
    active_budget = budget or ProviderInputBudget()
    with _open_provider_file(path) as handle:
        payload = handle.read(min(maximum, active_budget.remaining_file_bytes()) + 1)
    active_budget.file_data(len(payload))
    if len(payload) > maximum:
        raise ProviderDataLimitError("provider_file_too_large")
    return payload


def iter_bounded_jsonl_bytes(
    path: Path,
    budget: ProviderInputBudget | None = None,
    maximum_line: int = MAX_PROVIDER_JSONL_LINE_BYTES,
    maximum_file: int = MAX_PROVIDER_JSONL_BYTES,
) -> Iterator[bytes]:
    active_budget = budget or ProviderInputBudget()
    total = 0
    with _open_provider_file(path) as handle:
        while True:
            collection_remaining = active_budget.remaining_file_bytes()
            file_remaining = max(0, maximum_file - total)
            limit = min(maximum_line, file_remaining, collection_remaining)
            line = handle.readline(limit + 1)
            if not line:
                break
            total += len(line)
            active_budget.file_data(len(line))
            if len(line) > file_remaining:
                raise ProviderDataLimitError("provider_file_too_large")
            if len(line) > maximum_line:
                raise ProviderDataLimitError("provider_line_too_large")
            yield line


def _open_provider_descriptor(path: Path) -> int:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError:
        raise
    if stat.S_ISLNK(before.st_mode):
        raise ProviderDataLimitError("provider_file_symlink_not_allowed")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ProviderDataLimitError("provider_file_symlink_not_allowed") from None
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ProviderDataLimitError("provider_file_type_not_allowed")
    try:
        after = path.stat(follow_symlinks=False)
    except OSError:
        os.close(descriptor)
        raise ProviderDataLimitError("provider_file_source_changed") from None
    if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino) or (
        after.st_dev,
        after.st_ino,
    ) != (metadata.st_dev, metadata.st_ino):
        os.close(descriptor)
        raise ProviderDataLimitError("provider_file_source_changed")
    return descriptor


def _open_provider_file(path: Path) -> Any:
    return os.fdopen(_open_provider_descriptor(path), "rb")


@dataclass(slots=True)
class _HeldSQLiteFile:
    path: Path
    descriptor: int
    device: int
    inode: int
    charged_size: int


def _hold_sqlite_file(path: Path) -> _HeldSQLiteFile:
    descriptor = _open_provider_descriptor(path)
    metadata = os.fstat(descriptor)
    return _HeldSQLiteFile(
        path, descriptor, metadata.st_dev, metadata.st_ino, metadata.st_size
    )


def _sqlite_paths(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    )


@contextmanager
def open_provider_sqlite(
    path: Path, budget: ProviderInputBudget
) -> Iterator[sqlite3.Connection]:
    held: dict[Path, _HeldSQLiteFile] = {}
    connection: sqlite3.Connection | None = None
    try:
        for candidate in _sqlite_paths(path):
            if candidate != path and not candidate.exists():
                continue
            item = _hold_sqlite_file(candidate)
            held[candidate] = item
            budget.sqlite_files(item.charged_size)
        connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        _verify_sqlite_files(path, held, budget)
        yield connection
    except sqlite3.DataError:
        raise ProviderDataLimitError("provider_sqlite_field_too_large") from None
    finally:
        try:
            if held:
                _verify_sqlite_files(path, held, budget)
        finally:
            if connection is not None:
                connection.close()
            for item in held.values():
                os.close(item.descriptor)


def _verify_sqlite_files(
    path: Path, held: dict[Path, _HeldSQLiteFile], budget: ProviderInputBudget
) -> None:
    for candidate in _sqlite_paths(path):
        item = held.get(candidate)
        if item is None:
            if candidate != path and candidate.exists():
                item = _hold_sqlite_file(candidate)
                held[candidate] = item
                budget.sqlite_files(item.charged_size)
            continue
        metadata = os.fstat(item.descriptor)
        try:
            current = candidate.stat(follow_symlinks=False)
        except OSError:
            raise ProviderDataLimitError("provider_sqlite_source_changed") from None
        if (current.st_dev, current.st_ino) != (item.device, item.inode):
            raise ProviderDataLimitError("provider_sqlite_source_changed")
        if metadata.st_size > item.charged_size:
            budget.sqlite_files(metadata.st_size - item.charged_size)
            item.charged_size = metadata.st_size


def ensure_provider_sqlite_fields(
    connection: sqlite3.Connection, fields: Iterable[tuple[str, str]]
) -> None:
    identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    for table, field in fields:
        if not identifier.fullmatch(table) or not identifier.fullmatch(field):
            raise ValueError("invalid_sqlite_field_check")
        oversized = connection.execute(
            f'SELECT 1 FROM "{table}" '
            f'WHERE length(CAST("{field}" AS BLOB)) > ? LIMIT 1',
            (MAX_PROVIDER_SQLITE_FIELD_BYTES,),
        ).fetchone()
        if oversized is not None:
            raise ProviderDataLimitError("provider_sqlite_field_too_large")


def sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
