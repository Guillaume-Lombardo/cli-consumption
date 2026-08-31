from __future__ import annotations

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Event
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from cli_consumption import snapshot_extraction as extraction_module
from cli_consumption.models import Snapshot, empty_tokens
from cli_consumption.schema import downgrade_database
from cli_consumption.snapshot_extraction import (
    SnapshotExtractionError,
    SnapshotExtractionLimits,
    extract_snapshots,
)
from cli_consumption.storage import (
    create_database_engine,
    ingest_snapshot,
    initialize_database,
    validate_snapshot,
)

CANARY = "PRIVATE-SNAPSHOT-EXTRACTION-CANARY"


def _conversation(
    provider: str,
    identifier: str,
    started_at: str,
    *,
    event_count: int = 1,
) -> dict[str, Any]:
    return {
        "id": f"{provider}:{identifier}",
        "provider": provider,
        "external_id": identifier,
        "source_machine": "machine",
        "project": "project",
        "project_source": "none",
        "started_at": started_at,
        "ended_at": None,
        "duration_seconds": None,
        "source": "synthetic",
        "models": ["model"],
        "iterations": 1,
        "model_calls": 1,
        "tool_calls": 1,
        "compactions": 1,
        "event_count": event_count,
        "content_hash": f"{event_count}" * 64,
        **empty_tokens(),
    }


def _snapshot(
    provider: str,
    identifier: str,
    started_at: str = "2026-08-01T00:00:00Z",
    *,
    complete: bool = False,
    event_count: int = 1,
) -> Snapshot:
    conversation = _conversation(
        provider, identifier, started_at, event_count=event_count
    )
    snapshot = Snapshot(provider=provider, conversations=[conversation])
    if not complete:
        return snapshot
    conversation_id = str(conversation["id"])
    turn_id = f"{conversation_id}:turn"
    snapshot.turns.append(
        {
            "id": turn_id,
            "conversation_id": conversation_id,
            "external_id": "turn",
            "started_at": started_at,
            "ended_at": None,
            "status": "completed",
            "duration_ms": None,
            "time_to_first_token_ms": None,
            "model_calls": 1,
            "tool_calls": 1,
            **empty_tokens(),
        }
    )
    snapshot.model_calls.append(
        {
            "id": f"{conversation_id}:call",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "sequence": 0,
            "timestamp": started_at,
            "model": "model",
            **empty_tokens(),
        }
    )
    snapshot.tool_calls.append(
        {
            "id": f"{conversation_id}:tool",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "sequence": 0,
            "timestamp": started_at,
            "tool_name": "exec",
            "outer_tool_name": "exec",
        }
    )
    snapshot.work_items.append(
        {
            "id": f"{conversation_id}:work",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "sequence": 0,
            "kind": "command",
            "tool_name": "exec",
            "started_at_ms": 1,
            "completed_at_ms": 2,
            "duration_ms": 1,
            "status": "completed",
        }
    )
    snapshot.context_samples.append(
        {
            "id": f"{conversation_id}:context",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "sequence": 0,
            "timestamp": started_at,
            "input_tokens": 1,
            "context_window_tokens": 2,
        }
    )
    snapshot.turn_settings.append(
        {
            "id": f"{conversation_id}:setting",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "model": "model",
            "effort": "medium",
            "collaboration_mode": "default",
            "service_tier": "standard",
            "context_window_tokens": 2,
        }
    )
    snapshot.compaction_events.append(
        {
            "id": f"{conversation_id}:compaction",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "sequence": 0,
            "timestamp": started_at,
        }
    )
    return snapshot


def test_extracts_valid_provider_snapshots_and_excludes_internal_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "usage database.sqlite"
    engine = create_database_engine(database)
    codex = _snapshot("codex", "parent", complete=True)
    codex.conversations.append(_conversation("codex", "child", "2026-08-02T00:00:00Z"))
    codex.conversations[-1]["source_machine"] = "runner"
    codex.subagents.append(
        {
            "id": "codex:machine:edge",
            "provider": "codex",
            "source_machine": "machine",
            "parent_thread_id": "parent",
            "child_thread_id": "child",
            "status": "completed",
            "created_at_ms": 1,
            "updated_at_ms": 2,
            "agent_role": "worker",
            "tokens_used": 0,
        }
    )
    claude = _snapshot("claude", "other", "2026-08-03T00:00:00Z")
    ingest_snapshot(engine, codex)
    ingest_snapshot(engine, claude)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ingestion_runs VALUES "
                "(:canary, :canary, :canary, 0, 0, 0, 0, 0)"
            ),
            {"canary": CANARY},
        )
        connection.execute(
            text("INSERT INTO sync_receipts VALUES (:canary, :canary)"),
            {"canary": CANARY},
        )
        connection.execute(
            text("INSERT INTO subagent_scopes VALUES (:canary, :canary, 0)"),
            {"canary": CANARY},
        )
    engine.dispose()

    extracted = extract_snapshots(database)

    assert [snapshot.provider for snapshot in extracted] == ["claude", "codex"]
    by_provider = {snapshot.provider: snapshot for snapshot in extracted}
    assert {
        record["source_machine"] for record in by_provider["codex"].conversations
    } == {"machine", "runner"}
    for provider, expected in (("codex", codex), ("claude", claude)):
        actual_dict = by_provider[provider].to_dict()
        expected_dict = validate_snapshot(expected).to_dict()
        assert actual_dict["provider"] == expected_dict["provider"]
        for collection in (
            "conversations",
            "turns",
            "model_calls",
            "tool_calls",
            "work_items",
            "context_samples",
            "turn_settings",
            "compaction_events",
            "subagents",
        ):
            assert actual_dict[collection] == sorted(
                expected_dict[collection], key=lambda record: record["id"]
            )
    output = json.dumps([snapshot.to_dict() for snapshot in extracted])
    assert CANARY not in output
    assert "sync_receipts" not in output
    assert "subagent_scopes" not in output
    assert "ingestion_runs" not in output


def test_window_keeps_complete_selected_conversation_records_and_edges(
    tmp_path: Path,
) -> None:
    database = tmp_path / "window.sqlite"
    engine = create_database_engine(database)
    snapshot = _snapshot("codex", "inside", "2026-08-15T00:00:00Z", complete=True)
    snapshot.conversations.append(
        _conversation("codex", "outside", "2026-07-01T00:00:00Z")
    )
    snapshot.subagents.append(
        {
            "id": "codex:machine:edge",
            "provider": "codex",
            "source_machine": "machine",
            "parent_thread_id": "inside",
            "child_thread_id": "outside",
            "status": "completed",
            "created_at_ms": None,
            "updated_at_ms": None,
            "agent_role": "worker",
            "tokens_used": None,
        }
    )
    ingest_snapshot(engine, snapshot)
    engine.dispose()

    [extracted] = extract_snapshots(database, since="2026-08-01", until="2026-08-31")

    assert [row["external_id"] for row in extracted.conversations] == ["inside"]
    assert len(extracted.turns) == 1
    assert len(extracted.model_calls) == 1
    assert len(extracted.tool_calls) == 1
    assert len(extracted.work_items) == 1
    assert len(extracted.context_samples) == 1
    assert len(extracted.turn_settings) == 1
    assert len(extracted.compaction_events) == 1
    assert [row["id"] for row in extracted.subagents] == ["codex:machine:edge"]


def test_invalid_window_is_rejected_before_the_database_is_open(tmp_path: Path) -> None:
    missing = tmp_path / "window.sqlite"

    with pytest.raises(SnapshotExtractionError, match="invalid_window"):
        extract_snapshots(missing, since="2026-08-02", until="2026-08-01")

    assert not missing.exists()


def test_sqlite_file_uri_is_absolute_and_percent_encoded_cross_platform() -> None:
    assert (
        extraction_module._sqlite_file_uri(
            PurePosixPath("/var/lib/cli-consumption/database name.sqlite")
        )
        == "file:///var/lib/cli-consumption/database%20name.sqlite"
    )
    assert (
        extraction_module._sqlite_file_uri(
            PureWindowsPath("C:/Users/test/database name.sqlite")
        )
        == "file:///C:/Users/test/database%20name.sqlite"
    )


def test_unbounded_extraction_preserves_graph_only_provider_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "graph-only.sqlite"
    engine = create_database_engine(database)
    graph = Snapshot(
        provider="codex",
        subagents=[
            {
                "id": "codex:machine:edge",
                "provider": "codex",
                "source_machine": "machine",
                "parent_thread_id": "parent",
                "child_thread_id": "child",
                "status": "completed",
                "created_at_ms": None,
                "updated_at_ms": None,
                "agent_role": "worker",
                "tokens_used": None,
            }
        ],
    )
    ingest_snapshot(engine, graph)
    engine.dispose()

    [extracted] = extract_snapshots(database)

    assert extracted.conversations == []
    assert extracted.subagents == graph.subagents


@pytest.mark.parametrize("mutation", ["old", "new", "modified"])
def test_rejects_non_current_or_modified_schema_without_migrating(
    tmp_path: Path, mutation: str
) -> None:
    database = tmp_path / f"{mutation}.sqlite"
    engine = create_database_engine(database)
    initialize_database(engine)
    if mutation == "old":
        downgrade_database(engine, "0004")
    else:
        with engine.begin() as connection:
            if mutation == "new":
                connection.execute(
                    text("UPDATE alembic_version SET version_num='9999'")
                )
            else:
                connection.execute(
                    text("ALTER TABLE conversations ADD COLUMN unexpected TEXT")
                )
    engine.dispose()

    with pytest.raises(SnapshotExtractionError) as captured:
        extract_snapshots(database)

    assert str(captured.value) == "incompatible_database"
    check = create_database_engine(database)
    with check.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == (
        "0004" if mutation == "old" else "9999" if mutation == "new" else "0005"
    )
    check.dispose()


@pytest.mark.parametrize(
    ("limits", "snapshot"),
    [
        (SnapshotExtractionLimits(conversations=1), None),
        (SnapshotExtractionLimits(records=1), "complete"),
        (SnapshotExtractionLimits(scalar_bytes=1), "single"),
    ],
)
def test_extraction_limits_fail_with_one_generic_code(
    tmp_path: Path,
    limits: SnapshotExtractionLimits,
    snapshot: str | None,
) -> None:
    database = tmp_path / f"{CANARY}.sqlite"
    engine = create_database_engine(database)
    value = _snapshot("codex", "one", complete=snapshot == "complete")
    if snapshot is None:
        value.conversations.append(
            _conversation("codex", "two", "2026-08-02T00:00:00Z")
        )
    ingest_snapshot(engine, value)
    engine.dispose()

    with pytest.raises(SnapshotExtractionError) as captured:
        extract_snapshots(database, limits=limits)

    assert str(captured.value) == "snapshot_too_large"
    assert CANARY not in str(captured.value)


def test_scalar_byte_limit_is_preflighted_before_rows_are_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "preflight.sqlite"
    engine = create_database_engine(database)
    ingest_snapshot(engine, _snapshot("codex", "one"))
    engine.dispose()

    def fail_if_materialized(*_args, **_kwargs):
        raise AssertionError("rows must not be materialized")

    monkeypatch.setattr(extraction_module, "iter_report_rows", fail_if_materialized)
    with pytest.raises(SnapshotExtractionError, match="snapshot_too_large"):
        extract_snapshots(
            database,
            limits=SnapshotExtractionLimits(scalar_bytes=1),
        )


def test_database_errors_are_generic_and_connection_is_query_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = tmp_path / f"{CANARY}.sqlite"
    with pytest.raises(SnapshotExtractionError) as missing_error:
        extract_snapshots(missing)
    assert str(missing_error.value) == "database_unavailable"
    assert CANARY not in str(missing_error.value)
    assert CANARY not in caplog.text
    assert not missing.exists()

    database = tmp_path / "readonly.sqlite"
    engine = create_database_engine(database)
    ingest_snapshot(engine, _snapshot("codex", "one"))
    engine.dispose()
    original_verify = extraction_module.verify_current_database_schema

    def verify_and_probe(connection) -> None:
        original_verify(connection)
        with pytest.raises(SQLAlchemyError):
            connection.execute(text("DELETE FROM conversations"))

    monkeypatch.setattr(
        extraction_module, "verify_current_database_schema", verify_and_probe
    )
    [snapshot] = extract_snapshots(database)
    assert [record["external_id"] for record in snapshot.conversations] == ["one"]

    def fail_operationally(_connection) -> None:
        raise SQLAlchemyError(CANARY)

    monkeypatch.setattr(
        extraction_module, "verify_current_database_schema", fail_operationally
    )
    with pytest.raises(SnapshotExtractionError) as operational_error:
        extract_snapshots(database)
    assert str(operational_error.value) == "database_unavailable"
    assert CANARY not in str(operational_error.value)


def test_invalid_stored_value_is_refused_without_echo_or_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "invalid-value.sqlite"
    engine = create_database_engine(database)
    ingest_snapshot(engine, _snapshot("codex", "one"))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE conversations SET models_json=:canary"),
            {"canary": CANARY},
        )
    engine.dispose()

    with pytest.raises(SnapshotExtractionError) as captured:
        extract_snapshots(database)

    assert str(captured.value) == "invalid_database"
    assert CANARY not in str(captured.value)
    assert CANARY not in caplog.text


def test_wal_extraction_uses_one_snapshot_during_concurrent_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "wal.sqlite"
    engine = create_database_engine(database)
    initialize_database(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
    ingest_snapshot(engine, _snapshot("codex", "one", event_count=1))

    conversations_read = Event()
    release_reader = Event()
    original_iter = extraction_module.iter_report_rows

    def paused_iter(connection, table_name, window, *, batch_size=1_000):
        yield from original_iter(connection, table_name, window, batch_size=batch_size)
        if table_name == "conversations":
            conversations_read.set()
            assert release_reader.wait(5)

    monkeypatch.setattr(extraction_module, "iter_report_rows", paused_iter)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            reading = executor.submit(extract_snapshots, database)
            assert conversations_read.wait(5)
            ingest_snapshot(
                engine,
                _snapshot("codex", "one", complete=True, event_count=2),
            )
            release_reader.set()
            [during_write] = reading.result(timeout=5)
    finally:
        release_reader.set()

    assert during_write.conversations[0]["content_hash"] == "1" * 64
    assert during_write.turns == []
    monkeypatch.setattr(extraction_module, "iter_report_rows", original_iter)
    [after_write] = extract_snapshots(database)
    assert after_write.conversations[0]["content_hash"] == "2" * 64
    assert len(after_write.turns) == 1
    engine.dispose()


def test_orphan_wal_without_shm_in_read_only_directory_fails_generically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    engine = create_database_engine(source)
    initialize_database(engine)
    keeper = sqlite3.connect(source, isolation_level=None)
    target_directory = tmp_path / "readonly"
    target_directory.mkdir()
    target = target_directory / f"{CANARY}.sqlite"
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("BEGIN")
        keeper.execute("SELECT count(*) FROM conversations").fetchone()
        ingest_snapshot(engine, _snapshot("codex", "one"))
        source_wal = Path(f"{source}-wal")
        assert source_wal.is_file()
        shutil.copy2(source, target)
        target_wal = Path(f"{target}-wal")
        shutil.copy2(source_wal, target_wal)
        target.chmod(0o444)
        target_wal.chmod(0o444)
        target_directory.chmod(0o555)

        with pytest.raises(SnapshotExtractionError) as captured:
            extract_snapshots(target)

        assert str(captured.value) == "database_unavailable"
        assert CANARY not in str(captured.value)
        assert not Path(f"{target}-shm").exists()
    finally:
        target_directory.chmod(0o755)
        keeper.rollback()
        keeper.close()
        engine.dispose()
