from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    BigInteger,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema
from storage_helpers import read_table

from cli_consumption import schema as schema_module
from cli_consumption.migrations.versions import (
    v0001_baseline,
    v0002_minimize_subagents,
    v0003_canonical_timestamps,
    v0004_subagent_scope_freshness,
    v0005_sync_receipts,
)
from cli_consumption.models import Snapshot
from cli_consumption.retention import retain_before
from cli_consumption.schema import (
    BASELINE_COLUMNS,
    POSTGRESQL_MIGRATION_LOCK,
    SchemaCompatibilityError,
    _matching_type,
    downgrade_database,
    upgrade_database,
    verify_current_database_schema,
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
)
from cli_consumption.timestamps import canonical_timestamp


def _observe_migration_lock(engine, statement_fragment: str):
    guard = Lock()
    attempts = 0
    acquisitions = 0
    second_attempting = Event()
    first_acquired = Event()
    second_acquired = Event()

    def before_execute(
        _connection, _cursor, statement: str, _parameters, _context, _many
    ) -> None:
        nonlocal attempts
        if statement_fragment not in statement:
            return
        with guard:
            attempts += 1
            if attempts == 2:
                second_attempting.set()

    def after_execute(
        _connection, _cursor, statement: str, _parameters, _context, _many
    ) -> None:
        nonlocal acquisitions
        if statement_fragment not in statement:
            return
        with guard:
            acquisitions += 1
            if acquisitions == 1:
                first_acquired.set()
            elif acquisitions == 2:
                second_acquired.set()

    event.listen(engine, "before_cursor_execute", before_execute)
    event.listen(engine, "after_cursor_execute", after_execute)

    def remove() -> None:
        event.remove(engine, "before_cursor_execute", before_execute)
        event.remove(engine, "after_cursor_execute", after_execute)

    return second_attempting, first_acquired, second_acquired, remove


def _conversation(identifier: str, started_at: str | None) -> Conversation:
    return Conversation(
        id=identifier,
        provider="test",
        external_id=identifier,
        source_machine="machine",
        project="project",
        project_source="none",
        started_at=canonical_timestamp(started_at) if started_at else None,
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
            "0007"
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


def test_concurrent_sqlite_initialization_reaches_one_packaged_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "concurrent-empty.sqlite")
    second_attempting, first_acquired, second_acquired, remove = (
        _observe_migration_lock(engine, "BEGIN IMMEDIATE")
    )
    release_holder = Event()
    original_preflight = schema_module._preflight_unversioned

    def hold_first_migration(connection) -> None:
        assert first_acquired.is_set()
        assert connection.in_transaction()
        assert release_holder.wait(2)
        original_preflight(connection)

    monkeypatch.setattr(schema_module, "_preflight_unversioned", hold_first_migration)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(initialize_database, engine)
            assert first_acquired.wait(2)
            waiter = executor.submit(initialize_database, engine)
            assert second_attempting.wait(2)
            assert not second_acquired.wait(0.1)
            release_holder.set()
            holder.result()
            waiter.result()
            assert second_acquired.wait(2)
    finally:
        release_holder.set()
        remove()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007"
        )
    assert set(inspect(engine).get_table_names()) == {
        *BASELINE_COLUMNS,
        "alembic_version",
    }
    engine.dispose()


def test_concurrent_sqlite_migration_is_idempotent(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "concurrent-migration.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0002")
    barrier = Barrier(3)

    def migrate() -> None:
        barrier.wait()
        upgrade_database(engine)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(migrate) for _ in range(3)]
        for future in futures:
            future.result()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007"
        )
    assert "ix_conversations_ended_at" in {
        item["name"] for item in inspect(engine).get_indexes("conversations")
    }
    engine.dispose()


def test_failed_sqlite_migration_releases_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "failed-concurrent-migration.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0002")
    second_attempting, first_acquired, second_acquired, remove = (
        _observe_migration_lock(engine, "BEGIN IMMEDIATE")
    )
    release_failure = Event()
    original_upgrade = schema_module.command.upgrade
    calls = 0
    guard = Lock()

    def fail_first_upgrade(config, revision: str) -> None:
        nonlocal calls
        with guard:
            calls += 1
            call = calls
        if call == 1:
            assert first_acquired.is_set()
            assert config.attributes["connection"].in_transaction()
            assert release_failure.wait(2)
            raise RuntimeError("private migration failure")
        original_upgrade(config, revision)

    monkeypatch.setattr(schema_module.command, "upgrade", fail_first_upgrade)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            failing = executor.submit(upgrade_database, engine)
            assert first_acquired.wait(2)
            waiter = executor.submit(upgrade_database, engine)
            assert second_attempting.wait(2)
            assert not second_acquired.wait(0.1)
            release_failure.set()
            with pytest.raises(SchemaCompatibilityError) as error:
                failing.result()
            assert str(error.value) == "Database schema migration failed"
            assert "private" not in str(error.value)
            waiter.result()
            assert second_acquired.wait(2)
    finally:
        release_failure.set()
        remove()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007"
        )
    engine.dispose()


def test_sqlite_migration_lock_timeout_is_bounded_and_generic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "migration-timeout.sqlite")
    initialize_database(engine)
    monkeypatch.setattr(schema_module, "SQLITE_MIGRATION_LOCK_TIMEOUT_MS", 10)

    with engine.connect() as holder:
        holder.exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(SchemaCompatibilityError) as error:
            upgrade_database(engine)
        holder.rollback()

    assert str(error.value) == "Database schema migration failed"
    assert "migration-timeout" not in str(error.value)
    engine.dispose()


def test_concurrent_sqlite_downgrade_is_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "concurrent-downgrade.sqlite")
    initialize_database(engine)
    second_attempting, first_acquired, second_acquired, remove = (
        _observe_migration_lock(engine, "BEGIN IMMEDIATE")
    )
    release_holder = Event()
    original_downgrade = schema_module.command.downgrade
    calls = 0
    guard = Lock()

    def hold_first_downgrade(config, revision: str) -> None:
        nonlocal calls
        with guard:
            calls += 1
            call = calls
        if call == 1:
            assert first_acquired.is_set()
            assert release_holder.wait(2)
        original_downgrade(config, revision)

    monkeypatch.setattr(schema_module.command, "downgrade", hold_first_downgrade)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(downgrade_database, engine, "0002")
            assert first_acquired.wait(2)
            waiter = executor.submit(downgrade_database, engine, "0002")
            assert second_attempting.wait(2)
            assert not second_acquired.wait(0.1)
            release_holder.set()
            holder.result()
            waiter.result()
            assert second_acquired.wait(2)
    finally:
        release_holder.set()
        remove()

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0002"
        )
    engine.dispose()


def test_legacy_type_comparison_distinguishes_widths_and_text_kinds() -> None:
    assert _matching_type(BigInteger(), BigInteger())
    assert _matching_type(String(64), String(64))
    assert not _matching_type(Integer(), BigInteger())
    assert not _matching_type(Text(), String(64))
    assert not _matching_type(String(255), String(64))
    assert _matching_type(DOUBLE_PRECISION(precision=53), Float())


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
            "0007"
        )
    assert "agent_nickname" not in {
        column["name"] for column in inspect(engine).get_columns("subagents")
    }
    engine.dispose()


def test_unversioned_head_database_preserves_validated_scope_state(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "unversioned-head.sqlite")
    initialize_database(engine)
    with Session(engine) as session, session.begin():
        session.add(_conversation("test:head", "2026-01-01T00:00:00+00:00"))
        session.add(
            Subagent(
                id="test:machine:child",
                provider="test",
                source_machine="machine",
                parent_thread_id="head",
                child_thread_id="child",
                status="completed",
                created_at_ms=1,
                updated_at_ms=2,
                agent_role="worker",
                tokens_used=3,
            )
        )
    receipt_key = "11111111-2222-4333-8444-555555555555"
    receipt = ingest_snapshot(
        engine,
        Snapshot(provider="codex"),
        idempotency_key=receipt_key,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO subagent_scopes "
                "(provider, source_machine, lock_version) "
                "VALUES ('test', 'machine', 41)"
            )
        )
        connection.execute(text("DROP TABLE alembic_version"))

    initialize_database(engine)
    initialize_database(engine)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT lock_version FROM subagent_scopes "
                    "WHERE provider = 'test' AND source_machine = 'machine'"
                )
            )
            == 41
        )
    assert [row["id"] for row in read_table(engine, "conversations")] == ["test:head"]
    assert [row["id"] for row in read_table(engine, "subagents")] == [
        "test:machine:child"
    ]
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT idempotency_key, ingestion_run_id FROM sync_receipts "
                "WHERE idempotency_key = :key"
            ),
            {"key": receipt_key},
        ).one() == (receipt_key, receipt.run_id)
    engine.dispose()


def test_unversioned_revision_0004_is_adopted_before_sync_receipt_upgrade(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "unversioned-0004.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0004")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO subagent_scopes "
                "(provider, source_machine, lock_version) "
                "VALUES ('codex', 'legacy-0004', 41)"
            )
        )
        connection.execute(text("DROP TABLE alembic_version"))

    initialize_database(engine)
    initialize_database(engine)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT lock_version FROM subagent_scopes "
                    "WHERE provider = 'codex' AND source_machine = 'legacy-0004'"
                )
            )
            == 41
        )
    assert "sync_receipts" in inspect(engine).get_table_names()
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


@pytest.mark.parametrize(
    "mutation",
    (
        "DROP INDEX ix_ingestion_runs_provider",
        """
        ALTER TABLE ingestion_runs RENAME TO ingestion_runs_original;
        CREATE TABLE ingestion_runs (
            id INTEGER PRIMARY KEY,
            provider BLOB,
            ingested_at INTEGER,
            conversations_received TEXT,
            conversations_written TEXT,
            conversations_skipped TEXT,
            malformed_records TEXT,
            duplicate_conversations TEXT
        );
        DROP TABLE ingestion_runs_original
        """,
    ),
)
def test_unversioned_database_rejects_modified_constraints_and_types(
    tmp_path: Path, mutation: str
) -> None:
    engine = create_database_engine(tmp_path / "modified.sqlite")
    initialize_database(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
        for statement in mutation.split(";"):
            if statement.strip():
                connection.execute(text(statement))

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
        v0003_canonical_timestamps.upgrade()
        v0004_subagent_scope_freshness.upgrade()
        v0005_sync_receipts.upgrade()

    ddl = output.getvalue()
    assert "CREATE TABLE conversations" in ddl
    assert "CREATE TABLE subagents" in ddl
    assert "BIGINT" in ddl
    assert "FOREIGN KEY(conversation_id)" in ddl
    assert "ON DELETE CASCADE" in ddl
    assert "CREATE INDEX ix_turn_settings_service_tier" in ddl
    assert "UPDATE subagents" in ddl
    assert "DROP COLUMN agent_nickname" in ddl
    assert "CREATE INDEX ix_conversations_ended_at" in ddl
    assert "CREATE TABLE subagent_scopes" in ddl
    assert "INSERT INTO subagent_scopes" in ddl
    assert "CREATE TABLE sync_receipts" in ddl
    assert "FOREIGN KEY(ingestion_run_id)" in ddl


def test_subagent_scope_migration_seeds_existing_scopes_and_round_trips(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "scope-state.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0003")
    with Session(engine) as session, session.begin():
        session.add(_conversation("codex:legacy", "2026-01-01T00:00:00+00:00"))
        session.add(
            Subagent(
                id="codex:edge-only:child",
                provider="codex",
                source_machine="edge-only",
                parent_thread_id="parent",
                child_thread_id="child",
                status="completed",
                created_at_ms=1,
                updated_at_ms=2,
                agent_role="worker",
                tokens_used=3,
            )
        )

    upgrade_database(engine)

    with engine.connect() as connection:
        scopes = set(
            connection.execute(
                text(
                    "SELECT provider, source_machine, lock_version FROM subagent_scopes"
                )
            ).all()
        )
    assert scopes == {("codex", "edge-only", 0), ("test", "machine", 0)}

    downgrade_database(engine, "0003")
    assert "subagent_scopes" not in inspect(engine).get_table_names()
    upgrade_database(engine)
    assert "subagent_scopes" in inspect(engine).get_table_names()
    engine.dispose()


def test_sync_receipt_migration_round_trips_and_cascades(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "sync-receipts.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0004")
    assert "sync_receipts" not in inspect(engine).get_table_names()

    upgrade_database(engine)
    inspector = inspect(engine)
    assert "sync_receipts" in inspector.get_table_names()
    foreign_key = inspector.get_foreign_keys("sync_receipts")[0]
    assert foreign_key["constrained_columns"] == ["ingestion_run_id"]
    assert foreign_key["referred_table"] == "ingestion_runs"
    assert foreign_key["options"] == {"ondelete": "CASCADE"}

    result = ingest_snapshot(
        engine,
        Snapshot(provider="codex"),
        idempotency_key="11111111-2222-4333-8444-555555555555",
    )
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ingestion_runs WHERE id = :run_id"),
            {"run_id": result.run_id},
        )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM sync_receipts")) == 0

    downgrade_database(engine, "0004")
    assert "sync_receipts" not in inspect(engine).get_table_names()
    engine.dispose()


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


def test_timestamp_migration_canonicalizes_legacy_values_and_adds_index(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "timestamps.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0002")
    with Session(engine) as session, session.begin():
        conversation = _conversation("legacy-offset", "2026-01-01T00:00:00+00:00")
        conversation.ended_at = "2026-01-01T00:00:01+00:00"
        session.add(conversation)
        session.add(
            IngestionRun(
                id="legacy-run",
                provider="test",
                ingested_at="2026-01-01T00:00:00+00:00",
                conversations_received=1,
                conversations_written=1,
                conversations_skipped=0,
                malformed_records=0,
                duplicate_conversations=0,
            )
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE conversations SET started_at = :started, ended_at = :ended "
                "WHERE id = 'legacy-offset'"
            ),
            {
                "started": "2026-01-01T02:00:00+02:00",
                "ended": "2025-12-31T16:00:01-08:00",
            },
        )

    upgrade_database(engine)
    upgrade_database(engine)

    row = read_table(engine, "conversations")[0]
    assert row["started_at"] == "2026-01-01T00:00:00.000000+00:00"
    assert row["ended_at"] == "2026-01-01T00:00:01.000000+00:00"
    assert read_table(engine, "ingestion_runs")[0]["ingested_at"] == (
        "2026-01-01T00:00:00.000000+00:00"
    )
    assert "ix_conversations_ended_at" in {
        item["name"] for item in inspect(engine).get_indexes("conversations")
    }
    with engine.connect() as connection:
        plan = connection.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM conversations "
                "WHERE ended_at >= :bound"
            ),
            {"bound": "2026-01-01T00:00:00.000000+00:00"},
        ).all()
    assert "ix_conversations_ended_at" in str(plan)
    engine.dispose()


def test_timestamp_migration_fails_closed_and_rolls_back_without_leaking(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "invalid-timestamps.sqlite")
    initialize_database(engine)
    downgrade_database(engine, "0002")
    with Session(engine) as session, session.begin():
        session.add(_conversation("valid-offset", "2026-01-01T00:00:00+00:00"))
        session.add(
            IngestionRun(
                id="invalid-run",
                provider="test",
                ingested_at="privacy canary timestamp",
                conversations_received=0,
                conversations_written=0,
                conversations_skipped=0,
                malformed_records=0,
                duplicate_conversations=0,
            )
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE conversations SET started_at = '2026-01-01T02:00:00+02:00' "
                "WHERE id = 'valid-offset'"
            )
        )

    with pytest.raises(SchemaCompatibilityError) as error:
        upgrade_database(engine)

    assert str(error.value) == "Database schema migration failed"
    assert "privacy canary" not in str(error.value)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0002"
        )
        assert (
            connection.scalar(
                text("SELECT started_at FROM conversations WHERE id = 'valid-offset'")
            )
            == "2026-01-01T02:00:00+02:00"
        )
    assert "ix_conversations_ended_at" not in {
        item["name"] for item in inspect(engine).get_indexes("conversations")
    }
    engine.dispose()


def test_retention_previews_then_atomically_cascades_metadata(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "retention.sqlite")
    initialize_database(engine)
    ended_only_old = _conversation("test:ended-only-old", None)
    ended_only_old.ended_at = canonical_timestamp("2026-05-31T23:59:59.999999Z")
    ended_only_boundary = _conversation("test:ended-only-boundary", None)
    ended_only_boundary.ended_at = canonical_timestamp("2026-06-01T00:00:00Z")
    with Session(engine) as session, session.begin():
        session.add_all(
            [
                _conversation("test:old", "2026-01-01T00:00:00+00:00"),
                _conversation("test:offset-old", "2026-05-31T23:00:00+00:00"),
                _conversation("test:boundary", "2026-06-01T00:00:00+00:00"),
                _conversation("test:unknown", None),
                ended_only_old,
                ended_only_boundary,
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
                    ingested_at="2026-01-01T00:00:00.000000+00:00",
                    conversations_received=0,
                    conversations_written=0,
                    conversations_skipped=0,
                    malformed_records=0,
                    duplicate_conversations=0,
                ),
                IngestionRun(
                    id="offset-old-run",
                    provider="test",
                    ingested_at="2026-05-31T23:00:00.000000+00:00",
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
        3,
        1,
        2,
    )
    assert preview.applied is False
    assert len(read_table(engine, "conversations")) == 6

    applied = retain_before(engine, cutoff, apply=True)
    assert applied == preview.__class__(cutoff, 3, 1, 2, True)
    assert {row["id"] for row in read_table(engine, "conversations")} == {
        "test:boundary",
        "test:ended-only-boundary",
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


def test_postgresql_runtime_migrations_ingestion_and_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if database_url is None:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    schema_name = f"cli_consumption_test_{uuid.uuid4().hex}"
    admin_engine = create_database_engine(database_url)
    test_engine = None
    lock_probe_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        scoped_url = make_url(database_url).update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        )
        test_engine = create_engine(scoped_url)
        lock_probe_engine = create_engine(scoped_url)

        second_attempting, first_acquired, second_acquired, remove = (
            _observe_migration_lock(test_engine, "pg_advisory_xact_lock")
        )
        release_holder = Event()
        original_preflight = schema_module._preflight_unversioned

        def hold_first_postgresql_migration(connection) -> None:
            assert first_acquired.is_set()
            assert connection.in_transaction()
            assert release_holder.wait(2)
            original_preflight(connection)

        monkeypatch.setattr(
            schema_module, "_preflight_unversioned", hold_first_postgresql_migration
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                holder = executor.submit(upgrade_database, test_engine)
                assert first_acquired.wait(2)
                waiter = executor.submit(upgrade_database, test_engine)
                assert second_attempting.wait(2)
                assert not second_acquired.wait(0.1)
                release_holder.set()
                holder.result()
                waiter.result()
                assert second_acquired.wait(2)
        finally:
            release_holder.set()
            remove()
            monkeypatch.setattr(
                schema_module, "_preflight_unversioned", original_preflight
            )

        with lock_probe_engine.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": POSTGRESQL_MIGRATION_LOCK},
                )
                is True
            )
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
                == "0007"
            )
            verify_current_database_schema(connection)

        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO subagent_scopes "
                    "(provider, source_machine, lock_version) "
                    "VALUES ('codex', 'adoption', 41)"
                )
            )
            connection.execute(text("DROP TABLE alembic_version"))
        migration_barrier = Barrier(3)

        def migrate_concurrently() -> None:
            migration_barrier.wait()
            upgrade_database(test_engine)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(migrate_concurrently) for _ in range(3)]
            for future in futures:
                future.result()
        upgrade_database(test_engine)
        with test_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0007"
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT lock_version FROM subagent_scopes "
                        "WHERE provider = 'codex' AND source_machine = 'adoption'"
                    )
                )
                == 41
            )

        downgrade_database(test_engine, "0002")
        with Session(test_engine) as session, session.begin():
            session.add(_conversation("postgres-legacy", "2026-01-01T00:00:00+00:00"))
            session.add(
                IngestionRun(
                    id="postgres-invalid-run",
                    provider="test",
                    ingested_at="privacy canary timestamp",
                    conversations_received=0,
                    conversations_written=0,
                    conversations_skipped=0,
                    malformed_records=0,
                    duplicate_conversations=0,
                )
            )
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE conversations SET started_at = "
                    "'2026-01-01T02:00:00+02:00' WHERE id = 'postgres-legacy'"
                )
            )

        with pytest.raises(SchemaCompatibilityError) as error:
            upgrade_database(test_engine)
        assert str(error.value) == "Database schema migration failed"
        assert "privacy canary" not in str(error.value)
        with lock_probe_engine.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": POSTGRESQL_MIGRATION_LOCK},
                )
                is True
            )
        with test_engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0002"
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT started_at FROM conversations "
                        "WHERE id = 'postgres-legacy'"
                    )
                )
                == "2026-01-01T02:00:00+02:00"
            )
        with test_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM ingestion_runs WHERE id = 'postgres-invalid-run'")
            )
        second_attempting, first_acquired, second_acquired, remove = (
            _observe_migration_lock(test_engine, "pg_advisory_xact_lock")
        )
        release_holder = Event()
        original_upgrade = schema_module.command.upgrade
        upgrade_calls = 0
        upgrade_guard = Lock()

        def hold_first_postgresql_upgrade(config, revision: str) -> None:
            nonlocal upgrade_calls
            with upgrade_guard:
                upgrade_calls += 1
                call = upgrade_calls
            if call == 1:
                assert first_acquired.is_set()
                assert config.attributes["connection"].in_transaction()
                assert release_holder.wait(2)
            original_upgrade(config, revision)

        monkeypatch.setattr(
            schema_module.command, "upgrade", hold_first_postgresql_upgrade
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                holder = executor.submit(upgrade_database, test_engine)
                assert first_acquired.wait(2)
                waiter = executor.submit(upgrade_database, test_engine)
                assert second_attempting.wait(2)
                assert not second_acquired.wait(0.1)
                release_holder.set()
                holder.result()
                waiter.result()
                assert second_acquired.wait(2)
        finally:
            release_holder.set()
            remove()
            monkeypatch.setattr(schema_module.command, "upgrade", original_upgrade)
        assert read_table(test_engine, "conversations")[0]["started_at"] == (
            "2026-01-01T00:00:00.000000+00:00"
        )
        assert "ix_conversations_ended_at" in {
            item["name"] for item in inspect(test_engine).get_indexes("conversations")
        }
        with test_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM conversations WHERE id = 'postgres-legacy'")
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

        older = Snapshot.from_dict(snapshot.to_dict())
        older.conversations[0]["event_count"] = 0
        older.conversations[0]["content_hash"] = "f" * 64
        older.subagents.clear()
        skipped = ingest_snapshot(test_engine, older)
        assert (skipped.received, skipped.written, skipped.skipped) == (1, 0, 1)
        assert {row["id"] for row in read_table(test_engine, "subagents")} == {
            "codex:ci:old-child",
            "codex:ci:recent-child",
        }

        retention = retain_before(test_engine, cutoff, apply=True)
        assert (
            retention.conversations,
            retention.subagents,
            retention.ingestion_runs,
        ) == (1, 1, 2)
        assert read_table(test_engine, "conversations") == []
        assert read_table(test_engine, "turns") == []
        assert {row["id"] for row in read_table(test_engine, "subagents")} == {
            "codex:ci:recent-child"
        }

        replay_key = str(uuid.uuid4())
        replay_barrier = Barrier(2)

        def replay_concurrently() -> str:
            replay_barrier.wait()
            return ingest_snapshot(
                test_engine,
                Snapshot(provider="codex"),
                idempotency_key=replay_key,
            ).run_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            replay_futures = [executor.submit(replay_concurrently) for _ in range(2)]
            replay_run_ids = [future.result() for future in replay_futures]
        assert replay_run_ids[0] == replay_run_ids[1]
        with test_engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM sync_receipts "
                        "WHERE idempotency_key = :key"
                    ),
                    {"key": replay_key},
                )
                == 1
            )

        concurrent_base = Snapshot.from_dict(snapshot.to_dict())
        concurrent_base.turns.clear()
        concurrent_base.subagents.clear()
        concurrent_base.conversations[0]["id"] = "codex:postgres-concurrent"
        concurrent_base.conversations[0]["external_id"] = "postgres-concurrent"
        concurrent_base.conversations[0]["source_machine"] = "concurrent"
        concurrent_base.conversations[0]["event_count"] = 1
        concurrent_base.conversations[0]["content_hash"] = "1" * 64
        ingest_snapshot(test_engine, concurrent_base)

        def concurrent_version(event_count: int, child: str) -> Snapshot:
            candidate = Snapshot.from_dict(concurrent_base.to_dict())
            candidate.conversations[0]["event_count"] = event_count
            candidate.conversations[0]["content_hash"] = str(event_count) * 64
            candidate.subagents.append(
                {
                    "id": f"codex:concurrent:{child}",
                    "provider": "codex",
                    "source_machine": "concurrent",
                    "parent_thread_id": "postgres-concurrent",
                    "child_thread_id": child,
                    "status": "completed",
                    "created_at_ms": event_count,
                    "updated_at_ms": event_count,
                    "agent_role": "worker",
                    "tokens_used": event_count,
                }
            )
            return candidate

        barrier = Barrier(2)

        def concurrent_ingest(candidate: Snapshot) -> None:
            barrier.wait()
            ingest_snapshot(test_engine, candidate)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(concurrent_ingest, concurrent_version(0, "stale")),
                executor.submit(concurrent_ingest, concurrent_version(3, "newest")),
            ]
            for future in futures:
                future.result()

        concurrent_rows = [
            row
            for row in read_table(test_engine, "subagents")
            if row["source_machine"] == "concurrent"
        ]
        concurrent_conversation = next(
            row
            for row in read_table(test_engine, "conversations")
            if row["id"] == "codex:postgres-concurrent"
        )
        assert concurrent_conversation["event_count"] == 3
        assert [row["id"] for row in concurrent_rows] == ["codex:concurrent:newest"]

        second_attempting, first_acquired, second_acquired, remove = (
            _observe_migration_lock(test_engine, "pg_advisory_xact_lock")
        )
        release_holder = Event()
        original_downgrade = schema_module.command.downgrade
        downgrade_calls = 0
        downgrade_guard = Lock()

        def hold_first_postgresql_downgrade(config, revision: str) -> None:
            nonlocal downgrade_calls
            with downgrade_guard:
                downgrade_calls += 1
                call = downgrade_calls
            if call == 1:
                assert first_acquired.is_set()
                assert config.attributes["connection"].in_transaction()
                assert release_holder.wait(2)
            original_downgrade(config, revision)

        monkeypatch.setattr(
            schema_module.command, "downgrade", hold_first_postgresql_downgrade
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                holder = executor.submit(downgrade_database, test_engine)
                assert first_acquired.wait(2)
                waiter = executor.submit(downgrade_database, test_engine)
                assert second_attempting.wait(2)
                assert not second_acquired.wait(0.1)
                release_holder.set()
                holder.result()
                waiter.result()
                assert second_acquired.wait(2)
        finally:
            release_holder.set()
            remove()
            monkeypatch.setattr(schema_module.command, "downgrade", original_downgrade)
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
        if lock_probe_engine is not None:
            lock_probe_engine.dispose()
        if test_engine is not None:
            test_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
