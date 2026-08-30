from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Double, Float, String, Table, inspect, text
from sqlalchemy.engine import Connection, Engine


class SchemaCompatibilityError(RuntimeError):
    """The database cannot be safely adopted or migrated by this release."""


# Stable signed-bigint advisory-lock namespace for ``b"cli-cons"``.
POSTGRESQL_MIGRATION_LOCK = 7_164_216_750_902_308_467
SQLITE_MIGRATION_LOCK_TIMEOUT_MS = 15_000
CURRENT_DATABASE_REVISION = "0005"


BASELINE_COLUMNS: dict[str, frozenset[str]] = {
    "conversations": frozenset(
        {
            "id",
            "provider",
            "external_id",
            "source_machine",
            "project",
            "project_source",
            "started_at",
            "ended_at",
            "duration_seconds",
            "source",
            "models_json",
            "iterations",
            "model_calls",
            "tool_calls",
            "compactions",
            "event_count",
            "content_hash",
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "visible_output_tokens",
            "unattributed_tokens",
            "total_tokens",
        }
    ),
    "turns": frozenset(
        {
            "id",
            "conversation_id",
            "external_id",
            "started_at",
            "ended_at",
            "status",
            "duration_ms",
            "time_to_first_token_ms",
            "model_calls",
            "tool_calls",
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "visible_output_tokens",
            "unattributed_tokens",
            "total_tokens",
        }
    ),
    "model_calls": frozenset(
        {
            "id",
            "conversation_id",
            "turn_id",
            "sequence",
            "timestamp",
            "model",
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "visible_output_tokens",
            "unattributed_tokens",
            "total_tokens",
        }
    ),
    "tool_calls": frozenset(
        {
            "id",
            "conversation_id",
            "turn_id",
            "sequence",
            "timestamp",
            "tool_name",
            "outer_tool_name",
        }
    ),
    "work_items": frozenset(
        {
            "id",
            "conversation_id",
            "turn_id",
            "sequence",
            "kind",
            "tool_name",
            "started_at_ms",
            "completed_at_ms",
            "duration_ms",
            "status",
        }
    ),
    "context_samples": frozenset(
        {
            "id",
            "conversation_id",
            "turn_id",
            "sequence",
            "timestamp",
            "input_tokens",
            "context_window_tokens",
        }
    ),
    "turn_settings": frozenset(
        {
            "id",
            "conversation_id",
            "turn_id",
            "model",
            "effort",
            "collaboration_mode",
            "service_tier",
            "context_window_tokens",
        }
    ),
    "compaction_events": frozenset(
        {"id", "conversation_id", "turn_id", "sequence", "timestamp"}
    ),
    "subagents": frozenset(
        {
            "id",
            "provider",
            "source_machine",
            "parent_thread_id",
            "child_thread_id",
            "status",
            "created_at_ms",
            "updated_at_ms",
            "agent_nickname",
            "agent_role",
            "tokens_used",
        }
    ),
    "subagent_scopes": frozenset({"provider", "source_machine", "lock_version"}),
    "sync_receipts": frozenset({"idempotency_key", "ingestion_run_id"}),
    "ingestion_runs": frozenset(
        {
            "id",
            "provider",
            "ingested_at",
            "conversations_received",
            "conversations_written",
            "conversations_skipped",
            "malformed_records",
            "duplicate_conversations",
        }
    ),
}


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    config.attributes["connection"] = connection
    return config


def _preflight_unversioned(connection: Connection) -> None:
    from cli_consumption.storage import SCHEMA_TABLES

    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for table_name in existing & BASELINE_COLUMNS.keys():
        columns = inspector.get_columns(table_name)
        primary_key_columns = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
        )
        actual = frozenset(column["name"] for column in columns)
        accepted = {BASELINE_COLUMNS[table_name]}
        if table_name == "subagents":
            accepted.add(BASELINE_COLUMNS[table_name] - {"agent_nickname"})
        if actual not in accepted:
            _reject_unpublished_schema()

        declared = cast(Table, SCHEMA_TABLES[table_name].__table__)
        declared_columns = {column.name: column for column in declared.columns}
        for column in columns:
            name = column["name"]
            if name == "agent_nickname":
                if not _matches_legacy_nickname(column):
                    _reject_unpublished_schema()
                continue
            expected = declared_columns[name]
            if (
                (name in primary_key_columns) != expected.primary_key
                or bool(column["nullable"]) != expected.nullable
                or not _matching_type(column["type"], expected.type)
                or column.get("default") is not None
            ):
                _reject_unpublished_schema()

        expected_indexes = {
            (
                index.name,
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in declared.indexes
        }
        actual_indexes = {
            (
                index["name"],
                tuple(index["column_names"]),
                bool(index["unique"]),
            )
            for index in inspector.get_indexes(table_name)
        }
        accepted_indexes = {frozenset(expected_indexes)}
        if table_name == "conversations":
            accepted_indexes.add(
                frozenset(
                    expected_indexes
                    - {("ix_conversations_ended_at", ("ended_at",), False)}
                )
            )
        if frozenset(actual_indexes) not in accepted_indexes:
            _reject_unpublished_schema()

        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
                (constraint.ondelete or "").upper(),
            )
            for constraint in declared.foreign_key_constraints
        }
        actual_foreign_keys = {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                str(foreign_key.get("options", {}).get("ondelete", "")).upper(),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        if actual_foreign_keys != expected_foreign_keys:
            _reject_unpublished_schema()
        if inspector.get_check_constraints(
            table_name
        ) or inspector.get_unique_constraints(table_name):
            _reject_unpublished_schema()


def _matches_declared_layout(
    connection: Connection, table_names: frozenset[str]
) -> bool:
    from cli_consumption.storage import SCHEMA_TABLES

    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    if not table_names.issubset(existing):
        return False
    for table_name in table_names:
        model = SCHEMA_TABLES[table_name]
        declared = cast(Table, model.__table__)
        if {column["name"] for column in inspector.get_columns(table_name)} != {
            column.name for column in declared.columns
        }:
            return False
        expected_indexes = {
            (
                index.name,
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in declared.indexes
        }
        actual_indexes = {
            (
                index["name"],
                tuple(index["column_names"]),
                bool(index["unique"]),
            )
            for index in inspector.get_indexes(table_name)
        }
        if actual_indexes != expected_indexes:
            return False
    return True


def _matches_current_head_layout(connection: Connection) -> bool:
    from cli_consumption.storage import SCHEMA_TABLES

    return _matches_declared_layout(connection, frozenset(SCHEMA_TABLES))


def _matches_revision_0004_layout(connection: Connection) -> bool:
    from cli_consumption.storage import SCHEMA_TABLES

    inspector = inspect(connection)
    if "sync_receipts" in inspector.get_table_names():
        return False
    return _matches_declared_layout(
        connection,
        frozenset(SCHEMA_TABLES) - {"sync_receipts"},
    )


def _matching_type(actual: object, expected: object) -> bool:
    actual_generic = getattr(actual, "as_generic", lambda: actual)()
    expected_generic = getattr(expected, "as_generic", lambda: expected)()
    if type(actual_generic) is not type(expected_generic):
        return isinstance(actual_generic, Double) and type(expected_generic) is Float
    if isinstance(expected_generic, String):
        return getattr(actual_generic, "length", None) == expected_generic.length
    return True


def _matches_legacy_nickname(column: Mapping[str, object]) -> bool:
    return (
        _matching_type(column["type"], String(255))
        and column["nullable"] is False
        and not column.get("primary_key")
    )


def _reject_unpublished_schema() -> None:
    raise SchemaCompatibilityError(
        "The unversioned database does not match a published schema"
    )


@contextmanager
def _migration_lock(connection: Connection):
    """Serialize one schema operation in a single database transaction."""
    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql(
            f"PRAGMA busy_timeout = {SQLITE_MIGRATION_LOCK_TIMEOUT_MS}"
        )
        connection.commit()
        transaction = connection.begin()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif dialect == "postgresql":
        transaction = connection.begin()
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": POSTGRESQL_MIGRATION_LOCK},
        )
    else:  # pragma: no cover - guarded by the supported database backends
        raise RuntimeError("Unsupported database backend")
    try:
        yield
    except BaseException:
        transaction.rollback()
        raise
    else:
        transaction.commit()
    finally:
        if transaction.is_active:
            connection.rollback()


def upgrade_database(engine: Engine) -> None:
    """Adopt a legacy database and migrate it to this package's schema head."""
    try:
        with engine.connect() as connection, _migration_lock(connection):
            config = _config(connection)
            script = ScriptDirectory.from_config(config)
            expected_heads = tuple(script.get_heads())
            known_revisions = {item.revision for item in script.walk_revisions()}
            context = MigrationContext.configure(connection)
            current_heads = tuple(context.get_current_heads())
            adopt_revision: str | None = None
            if not current_heads:
                _preflight_unversioned(connection)
                if _matches_current_head_layout(connection):
                    adopt_revision = expected_heads[0]
                elif _matches_revision_0004_layout(connection):
                    adopt_revision = "0004"
            elif any(head not in known_revisions for head in current_heads):
                raise SchemaCompatibilityError(
                    "The database schema is newer than or unknown to this package"
                )
            elif current_heads == expected_heads:
                return
            if adopt_revision is not None:
                command.stamp(config, adopt_revision)
            if adopt_revision != expected_heads[0]:
                command.upgrade(config, "head")
            migrated_heads = tuple(
                MigrationContext.configure(connection).get_current_heads()
            )
            if migrated_heads != expected_heads:
                raise SchemaCompatibilityError(
                    "The database did not reach the expected schema version"
                )
    except SchemaCompatibilityError:
        raise
    except Exception:
        raise SchemaCompatibilityError("Database schema migration failed") from None


def downgrade_database(engine: Engine, revision: str = "0001") -> None:
    """Downgrade to a known revision, never below the adopted baseline."""
    try:
        with engine.connect() as connection, _migration_lock(connection):
            config = _config(connection)
            script = ScriptDirectory.from_config(config)
            known_revisions = {item.revision for item in script.walk_revisions()}
            if revision not in known_revisions:
                raise SchemaCompatibilityError(
                    "The requested database schema revision is unknown"
                )
            current_heads = tuple(
                MigrationContext.configure(connection).get_current_heads()
            )
            if not current_heads or any(
                head not in known_revisions for head in current_heads
            ):
                raise SchemaCompatibilityError(
                    "The current database schema revision is unknown"
                )
            command.downgrade(config, revision)
            migrated_heads = tuple(
                MigrationContext.configure(connection).get_current_heads()
            )
            if migrated_heads != (revision,):
                raise SchemaCompatibilityError(
                    "The database did not reach the requested schema version"
                )
    except SchemaCompatibilityError:
        raise
    except Exception:
        raise SchemaCompatibilityError("Database schema migration failed") from None
