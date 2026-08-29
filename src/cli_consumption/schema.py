from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine


class SchemaCompatibilityError(RuntimeError):
    """The database cannot be safely adopted or migrated by this release."""


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
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    for table_name in existing & BASELINE_COLUMNS.keys():
        actual = frozenset(
            column["name"] for column in inspector.get_columns(table_name)
        )
        accepted = {BASELINE_COLUMNS[table_name]}
        if table_name == "subagents":
            accepted.add(BASELINE_COLUMNS[table_name] - {"agent_nickname"})
        if actual not in accepted:
            raise SchemaCompatibilityError(
                "The unversioned database does not match a published schema"
            )


def upgrade_database(engine: Engine) -> None:
    """Adopt a legacy database and migrate it to this package's schema head."""
    try:
        with engine.connect() as connection:
            config = _config(connection)
            script = ScriptDirectory.from_config(config)
            expected_heads = tuple(script.get_heads())
            known_revisions = {item.revision for item in script.walk_revisions()}
            context = MigrationContext.configure(connection)
            current_heads = tuple(context.get_current_heads())
            if not current_heads:
                _preflight_unversioned(connection)
            elif any(head not in known_revisions for head in current_heads):
                raise SchemaCompatibilityError(
                    "The database schema is newer than or unknown to this package"
                )
            connection.commit()
            command.upgrade(config, "head")
            connection.commit()
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
        with engine.connect() as connection:
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
            connection.commit()
            command.downgrade(config, revision)
            connection.commit()
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
