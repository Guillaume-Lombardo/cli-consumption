from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import quote

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from cli_consumption.models import (
    MAX_SNAPSHOT_CONVERSATIONS,
    MAX_SNAPSHOT_RECORDS,
    Snapshot,
    SnapshotValidationError,
)
from cli_consumption.reporting import (
    ExportWindow,
    iter_report_rows,
    parse_export_window,
    report_estimate_statement,
)
from cli_consumption.schema import (
    SchemaCompatibilityError,
    verify_current_database_schema,
)
from cli_consumption.storage import validate_snapshot

MAX_EXTRACTED_SCALAR_BYTES = 128 * 1024 * 1024

_CHILD_TABLE_COLLECTIONS = {
    "turns": "turns",
    "model_calls": "model_calls",
    "tool_calls": "tool_calls",
    "work_items": "work_items",
    "context_samples": "context_samples",
    "turn_settings": "turn_settings",
    "compaction_events": "compaction_events",
}
_EXTRACTED_TABLES = (
    "conversations",
    *_CHILD_TABLE_COLLECTIONS,
    "subagents",
)


class SnapshotExtractionError(RuntimeError):
    """A bounded extraction failure safe to expose without source details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SnapshotExtractionLimits:
    conversations: int = MAX_SNAPSHOT_CONVERSATIONS
    records: int = MAX_SNAPSHOT_RECORDS
    scalar_bytes: int = MAX_EXTRACTED_SCALAR_BYTES

    def __post_init__(self) -> None:
        if min(self.conversations, self.records, self.scalar_bytes) <= 0:
            raise ValueError("extraction limits must be positive")


@dataclass(slots=True)
class _Budget:
    limits: SnapshotExtractionLimits
    conversations: int = 0
    records: int = 0
    scalar_bytes: int = 0

    def charge(self, record: dict[str, Any], *, conversation: bool = False) -> None:
        self.records += 1
        self.conversations += int(conversation)
        self.scalar_bytes += len(
            json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if (
            self.conversations > self.limits.conversations
            or self.records > self.limits.records
            or self.scalar_bytes > self.limits.scalar_bytes
        ):
            raise SnapshotExtractionError("snapshot_too_large")


def extract_snapshots(
    database: str | Path,
    *,
    since: str | None = None,
    until: str | None = None,
    limits: SnapshotExtractionLimits | None = None,
) -> list[Snapshot]:
    """Reconstruct strict snapshots from a current local SQLite database."""
    try:
        window = parse_export_window(since, until)
    except ValueError:
        raise SnapshotExtractionError("invalid_window") from None

    engine: Engine | None = None
    try:
        engine = _create_read_only_sqlite_engine(database)
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN")
            try:
                verify_current_database_schema(connection)
                return _extract_snapshots(
                    connection,
                    window,
                    limits or SnapshotExtractionLimits(),
                )
            finally:
                connection.rollback()
    except SnapshotExtractionError:
        raise
    except SchemaCompatibilityError:
        raise SnapshotExtractionError("incompatible_database") from None
    except (SnapshotValidationError, TypeError, ValueError):
        raise SnapshotExtractionError("invalid_database") from None
    except (OSError, SQLAlchemyError):
        raise SnapshotExtractionError("database_unavailable") from None
    finally:
        if engine is not None:
            engine.dispose()


def _create_read_only_sqlite_engine(database: str | Path) -> Engine:
    raw = str(database)
    if "://" in raw:
        raise SnapshotExtractionError("database_unavailable")
    path = Path(raw).expanduser()
    if path.is_symlink() or not path.is_file():
        raise SnapshotExtractionError("database_unavailable")
    resolved = path.resolve(strict=True)
    sqlite_uri = _sqlite_file_uri(resolved)
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_uri}?mode=ro&uri=true",
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _enforce_read_only(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA query_only=ON")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def _sqlite_file_uri(path: PurePath) -> str:
    normalized = path.as_posix()
    if normalized.startswith("//"):
        return f"file:{quote(normalized, safe='/:')}"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return f"file://{quote(normalized, safe='/:')}"


def _extract_snapshots(
    connection: Connection,
    window: ExportWindow,
    limits: SnapshotExtractionLimits,
) -> list[Snapshot]:
    estimates = connection.execute(
        report_estimate_statement(
            connection,
            window,
            table_names=_EXTRACTED_TABLES,
        )
    ).mappings()
    estimated_records = 0
    estimated_scalar_bytes = 0
    for estimate in estimates:
        records = int(estimate["records"])
        if estimate["table_name"] == "conversations" and records > limits.conversations:
            raise SnapshotExtractionError("snapshot_too_large")
        estimated_records += records
        estimated_scalar_bytes += int(estimate["scalar_bytes"])
    if (
        estimated_records > limits.records
        or estimated_scalar_bytes > limits.scalar_bytes
    ):
        raise SnapshotExtractionError("snapshot_too_large")

    budget = _Budget(limits)
    snapshots: dict[str, Snapshot] = {}
    conversation_providers: dict[str, str] = {}

    for stored in iter_report_rows(connection, "conversations", window):
        record = dict(stored)
        try:
            models = json.loads(record.pop("models_json"))
        except (KeyError, TypeError, json.JSONDecodeError):
            raise SnapshotExtractionError("invalid_database") from None
        if not isinstance(models, list):
            raise SnapshotExtractionError("invalid_database")
        record["models"] = models
        provider = record.get("provider")
        conversation_id = record.get("id")
        if not isinstance(provider, str) or not isinstance(conversation_id, str):
            raise SnapshotExtractionError("invalid_database")
        budget.charge(record, conversation=True)
        snapshots.setdefault(
            provider, Snapshot(provider=provider)
        ).conversations.append(record)
        conversation_providers[conversation_id] = provider

    for table_name, collection_name in _CHILD_TABLE_COLLECTIONS.items():
        for record in iter_report_rows(connection, table_name, window):
            conversation_id = record.get("conversation_id")
            provider = conversation_providers.get(str(conversation_id))
            if provider is None:
                raise SnapshotExtractionError("invalid_database")
            budget.charge(record)
            getattr(snapshots[provider], collection_name).append(record)

    for record in iter_report_rows(connection, "subagents", window):
        provider = record.get("provider")
        if not isinstance(provider, str):
            raise SnapshotExtractionError("invalid_database")
        budget.charge(record)
        snapshots.setdefault(provider, Snapshot(provider=provider)).subagents.append(
            record
        )

    validated: list[Snapshot] = []
    serialized_bytes = 0
    for provider in sorted(snapshots):
        snapshot = validate_snapshot(snapshots[provider])
        serialized_bytes += len(
            json.dumps(
                snapshot.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if serialized_bytes > limits.scalar_bytes:
            raise SnapshotExtractionError("snapshot_too_large")
        validated.append(snapshot)
    return validated
