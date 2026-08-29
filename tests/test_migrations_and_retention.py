from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from cli_consumption.migrations.versions import (
    v0001_baseline,
    v0002_minimize_subagents,
)
from cli_consumption.models import Snapshot
from cli_consumption.retention import retain_before
from cli_consumption.schema import (
    BASELINE_COLUMNS,
    SchemaCompatibilityError,
    downgrade_database,
    upgrade_database,
)
from cli_consumption.storage import (
    Base,
    Conversation,
    IngestionRun,
    Subagent,
    Turn,
    create_database_engine,
    ingest_snapshot,
    initialize_database,
    read_table,
)


def _conversation(identifier: str, started_at: str | None) -> Conversation:
    return Conversation(
        id=identifier,
        provider="test",
        external_id=identifier,
        source_machine="machine",
        project="project",
        project_source="none",
        started_at=started_at,
        ended_at=None,
        duration_seconds=None,
        source="synthetic",
        models_json="[]",
        iterations=0,
        model_calls=0,
        tool_calls=0,
        compactions=0,
        event_count=0,
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


def _turn(identifier: str, conversation_id: str) -> Turn:
    return Turn(
        id=identifier,
        conversation_id=conversation_id,
        external_id=identifier,
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


def test_empty_database_upgrades_to_packaged_head(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "empty.sqlite")

    upgrade_database(engine)
    inspector = inspect(engine)

    assert set(inspector.get_table_names()) == {*BASELINE_COLUMNS, "alembic_version"}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0002"
        )
    for table in Base.metadata.sorted_tables:
        assert {column["name"] for column in inspector.get_columns(table.name)} == (
            {column.name for column in table.columns}
        )
    assert "agent_nickname" not in {
        column["name"] for column in inspector.get_columns("subagents")
    }
    assert inspector.get_foreign_keys("turns")[0]["options"] == {"ondelete": "CASCADE"}
    assert {index["name"] for index in inspector.get_indexes("model_calls")} >= {
        "ix_model_calls_conversation_id",
        "ix_model_calls_model",
        "ix_model_calls_timestamp",
        "ix_model_calls_turn_id",
    }
    engine.dispose()


def test_unversioned_database_is_adopted_without_data_loss(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "legacy.sqlite")
    initialize_database(engine)
    downgrade_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ingestion_runs VALUES "
                "('run', 'test', '2026-01-01T00:00:00+00:00', 1, 1, 0, 0, 0)"
            )
        )
        connection.execute(text("DROP TABLE alembic_version"))

    initialize_database(engine)

    assert read_table(engine, "ingestion_runs")[0]["id"] == "run"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0002"
        )
    assert "agent_nickname" not in {
        column["name"] for column in inspect(engine).get_columns("subagents")
    }
    engine.dispose()


def test_partial_legacy_database_gains_missing_additive_table(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "partial.sqlite")
    initialize_database(engine)
    downgrade_database(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE work_items"))
        connection.execute(text("DROP TABLE alembic_version"))

    initialize_database(engine)

    assert "work_items" in inspect(engine).get_table_names()
    engine.dispose()


def test_ambiguous_unversioned_database_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "ambiguous.sqlite")
    initialize_database(engine)
    downgrade_database(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(
            text("ALTER TABLE conversations ADD COLUMN raw_content TEXT")
        )

    with pytest.raises(SchemaCompatibilityError, match="published schema"):
        initialize_database(engine)

    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()


def test_unknown_database_revision_is_rejected(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "future.sqlite")
    initialize_database(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'future'"))

    with pytest.raises(SchemaCompatibilityError, match="newer than or unknown"):
        initialize_database(engine)
    engine.dispose()


def test_migrations_emit_portable_postgresql_structure() -> None:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )

    with Operations.context(context):
        v0001_baseline.upgrade()
        v0002_minimize_subagents.upgrade()

    ddl = output.getvalue()
    assert "CREATE TABLE conversations" in ddl
    assert "CREATE TABLE subagents" in ddl
    assert "BIGINT" in ddl
    assert "FOREIGN KEY(conversation_id)" in ddl
    assert "ON DELETE CASCADE" in ddl
    assert "CREATE INDEX ix_turn_settings_service_tier" in ddl
    assert "UPDATE subagents" in ddl
    assert "DROP COLUMN agent_nickname" in ddl


def test_subagent_migration_normalizes_history_and_round_trips(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "subagents.sqlite")
    initialize_database(engine)
    downgrade_database(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO subagents (
                    id, provider, source_machine, parent_thread_id, child_thread_id,
                    status, created_at_ms, updated_at_ms, agent_nickname, agent_role,
                    tokens_used
                ) VALUES
                    ('done', 'codex', 'machine', 'parent', 'done', 'done', 1, 2,
                     'privacy canary', 'tester', 3),
                    ('odd', 'codex', 'machine', 'parent', 'odd', 'private-status', 1, 2,
                     'privacy canary', 'private role', 3),
                    ('stopped', 'codex', 'machine', 'parent', 'stopped',
                     'cancelled', 1, 2,
                     'privacy canary', 'explorer', 3)
                """
            )
        )

    upgrade_database(engine)

    assert "agent_nickname" not in {
        column["name"] for column in inspect(engine).get_columns("subagents")
    }
    rows = {row["id"]: row for row in read_table(engine, "subagents")}
    assert (rows["done"]["status"], rows["done"]["agent_role"]) == (
        "completed",
        "test",
    )
    assert (rows["odd"]["status"], rows["odd"]["agent_role"]) == (
        "unknown",
        "other",
    )
    assert (rows["stopped"]["status"], rows["stopped"]["agent_role"]) == (
        "aborted",
        "research",
    )
    assert "privacy canary" not in str(rows)

    downgrade_database(engine)
    with engine.connect() as connection:
        downgraded = connection.execute(
            text("SELECT agent_nickname FROM subagents ORDER BY id")
        ).scalars()
        assert list(downgraded) == ["", "", ""]
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0001"
        )
    upgrade_database(engine)
    assert "agent_nickname" not in {
        column["name"] for column in inspect(engine).get_columns("subagents")
    }
    engine.dispose()


def test_retention_previews_then_atomically_cascades_metadata(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "retention.sqlite")
    initialize_database(engine)
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                _conversation("test:old", "2026-01-01T00:00:00+00:00"),
                _conversation("test:offset-old", "2026-06-01T01:00:00+02:00"),
                _conversation("test:boundary", "2026-06-01T00:00:00+00:00"),
                _conversation("test:unknown", None),
                _turn("test:old:turn", "test:old"),
                Subagent(
                    id="old-edge",
                    provider="test",
                    source_machine="machine",
                    parent_thread_id="old",
                    child_thread_id="child",
                    status="completed",
                    created_at_ms=1,
                    updated_at_ms=None,
                    agent_role="test",
                    tokens_used=0,
                ),
                Subagent(
                    id="unknown-edge",
                    provider="test",
                    source_machine="machine",
                    parent_thread_id="unknown",
                    child_thread_id="child",
                    status="unknown",
                    created_at_ms=None,
                    updated_at_ms=None,
                    agent_role="test",
                    tokens_used=None,
                ),
                IngestionRun(
                    id="old-run",
                    provider="test",
                    ingested_at="2026-01-01T00:00:00+00:00",
                    conversations_received=0,
                    conversations_written=0,
                    conversations_skipped=0,
                    malformed_records=0,
                    duplicate_conversations=0,
                ),
                IngestionRun(
                    id="offset-old-run",
                    provider="test",
                    ingested_at="2026-06-01T01:00:00+02:00",
                    conversations_received=0,
                    conversations_written=0,
                    conversations_skipped=0,
                    malformed_records=0,
                    duplicate_conversations=0,
                ),
            ]
        )
    cutoff = datetime(2026, 6, 1, tzinfo=UTC)

    preview = retain_before(engine, cutoff)
    assert (preview.conversations, preview.subagents, preview.ingestion_runs) == (
        2,
        1,
        2,
    )
    assert preview.applied is False
    assert len(read_table(engine, "conversations")) == 4

    applied = retain_before(engine, cutoff, apply=True)
    assert applied == preview.__class__(cutoff, 2, 1, 2, True)
    assert {row["id"] for row in read_table(engine, "conversations")} == {
        "test:boundary",
        "test:unknown",
    }
    assert read_table(engine, "turns") == []
    assert {row["id"] for row in read_table(engine, "subagents")} == {"unknown-edge"}
    assert read_table(engine, "ingestion_runs") == []
    engine.dispose()


def test_retention_requires_timezone_aware_cutoff(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "retention.sqlite")
    with pytest.raises(ValueError, match="timezone"):
        retain_before(engine, datetime(2026, 1, 1))
    assert not (tmp_path / "retention.sqlite").exists()
    engine.dispose()


def test_postgresql_runtime_migrations_ingestion_and_retention() -> None:
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if database_url is None:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    schema_name = f"cli_consumption_test_{uuid.uuid4().hex}"
    admin_engine = create_database_engine(database_url)
    test_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        scoped_url = make_url(database_url).update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        )
        test_engine = create_engine(scoped_url)

        upgrade_database(test_engine)
        inspector = inspect(test_engine)
        assert "agent_nickname" not in {
            column["name"] for column in inspector.get_columns("subagents")
        }
        assert {index["name"] for index in inspector.get_indexes("model_calls")} >= {
            "ix_model_calls_conversation_id",
            "ix_model_calls_model",
            "ix_model_calls_timestamp",
            "ix_model_calls_turn_id",
        }
        foreign_key = inspector.get_foreign_keys("turns")[0]
        assert foreign_key["constrained_columns"] == ["conversation_id"]
        assert foreign_key["options"]["ondelete"] == "CASCADE"
        with test_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0002"
            )

        cutoff = datetime.now(UTC) + timedelta(days=1)
        recent_ms = int((cutoff + timedelta(days=1)).timestamp() * 1000)
        snapshot = Snapshot(
            provider="codex",
            conversations=[
                {
                    "id": "codex:postgres-runtime",
                    "provider": "codex",
                    "external_id": "postgres-runtime",
                    "source_machine": "ci",
                    "project": "postgres-runtime",
                    "project_source": "none",
                    "started_at": "2000-01-01T00:00:00+00:00",
                    "ended_at": "2000-01-01T00:00:01+00:00",
                    "duration_seconds": 1.0,
                    "source": "local-jsonl",
                    "models": [],
                    "iterations": 1,
                    "model_calls": 0,
                    "tool_calls": 0,
                    "compactions": 0,
                    "event_count": 1,
                    "content_hash": "0" * 64,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "uncached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "visible_output_tokens": 0,
                    "unattributed_tokens": 0,
                    "total_tokens": 0,
                }
            ],
            turns=[
                {
                    "id": "codex:postgres-runtime:turn",
                    "conversation_id": "codex:postgres-runtime",
                    "external_id": "turn",
                    "started_at": "2000-01-01T00:00:00+00:00",
                    "ended_at": "2000-01-01T00:00:01+00:00",
                    "status": "completed",
                    "duration_ms": 1000,
                    "time_to_first_token_ms": None,
                    "model_calls": 0,
                    "tool_calls": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_write_input_tokens": 0,
                    "uncached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "visible_output_tokens": 0,
                    "unattributed_tokens": 0,
                    "total_tokens": 0,
                }
            ],
            subagents=[
                {
                    "id": "codex:ci:old-child",
                    "provider": "codex",
                    "source_machine": "ci",
                    "parent_thread_id": "postgres-runtime",
                    "child_thread_id": "old-child",
                    "status": "completed",
                    "created_at_ms": 1,
                    "updated_at_ms": 1,
                    "agent_role": "worker",
                    "tokens_used": 0,
                },
                {
                    "id": "codex:ci:recent-child",
                    "provider": "codex",
                    "source_machine": "ci",
                    "parent_thread_id": "postgres-runtime",
                    "child_thread_id": "recent-child",
                    "status": "in-progress",
                    "created_at_ms": recent_ms,
                    "updated_at_ms": recent_ms,
                    "agent_role": "research",
                    "tokens_used": None,
                },
            ],
        )
        ingestion = ingest_snapshot(test_engine, snapshot)
        assert (ingestion.received, ingestion.written, ingestion.skipped) == (1, 1, 0)
        assert len(read_table(test_engine, "turns")) == 1

        retention = retain_before(test_engine, cutoff, apply=True)
        assert (
            retention.conversations,
            retention.subagents,
            retention.ingestion_runs,
        ) == (1, 1, 1)
        assert read_table(test_engine, "conversations") == []
        assert read_table(test_engine, "turns") == []
        assert {row["id"] for row in read_table(test_engine, "subagents")} == {
            "codex:ci:recent-child"
        }

        downgrade_database(test_engine)
        with test_engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT agent_nickname FROM subagents "
                        "WHERE id = 'codex:ci:recent-child'"
                    )
                )
                == ""
            )
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0001"
            )
        upgrade_database(test_engine)
        assert "agent_nickname" not in {
            column["name"] for column in inspect(test_engine).get_columns("subagents")
        }
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
