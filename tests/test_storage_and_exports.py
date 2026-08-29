from __future__ import annotations

import builtins
import csv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest
from sqlalchemy import Table, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.dashboard import (
    _dashboard_payload,
    _round_epoch_day,
    _round_timestamp,
    _tool_category,
    generate_dashboard,
)
from cli_consumption.exporting import export_csv
from cli_consumption.models import Snapshot
from cli_consumption.storage import (
    SCHEMA_TABLES,
    TABLES,
    create_database_engine,
    ingest_snapshot,
    initialize_database,
    normalize_database_url,
    read_table,
)


def test_ingestion_is_idempotent_and_exports_are_self_contained(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    snapshot = CodexAdapter().collect(
        [("workstation", home)],
        [("</script><script>project-label</script>", "/srv/work")],
    )
    engine = create_database_engine(tmp_path / "usage.sqlite")

    first = ingest_snapshot(engine, snapshot)
    second = ingest_snapshot(engine, snapshot)

    assert (first.written, first.skipped) == (1, 0)
    assert (second.written, second.skipped) == (0, 1)
    assert len(read_table(engine, "conversations")) == 1
    assert len(read_table(engine, "model_calls")) == 1

    output = tmp_path / "reports"
    paths = export_csv(engine, output)
    dashboard = output / "dashboard.html"
    generate_dashboard(engine, dashboard)
    assert len(paths) == 10
    assert not (output / "subagent_scopes.csv").exists()
    html = dashboard.read_text(encoding="utf-8")
    assert "CLI Consumption" in html
    assert "Turn performance" in html
    assert "Turn rate" in html
    assert "Technical throughput" not in html
    assert 'id="themeToggle"' in html
    assert "localStorage.getItem('cli-consumption-theme')" in html
    assert "radial-gradient" not in html
    assert "linear-gradient" not in html
    assert html.index("<h2>Models</h2>") < html.index("<h2>Turn performance</h2>")
    assert "Median total-token count only for providers" in html
    assert "tokenSemantics==='additive'" in html
    assert html.count('class="info"') == 1
    assert "Data quality" in html
    assert "https://" not in html
    assert "secret value" not in html
    assert "conversation-1" not in html
    assert "content_hash" not in html
    assert "external_id" not in html
    assert "agent_nickname" not in html
    assert html.count("<script>") == 1
    assert "<script>project-label" not in html
    assert "<\\/script><script>" not in html
    for path in paths:
        assert "secret value" not in path.read_text(encoding="utf-8")
    for table in (
        "conversations",
        "turns",
        "model_calls",
        "tool_calls",
        "work_items",
        "context_samples",
        "turn_settings",
        "compaction_events",
        "subagents",
        "ingestion_runs",
    ):
        assert "secret value" not in str(read_table(engine, table))

    payload = _dashboard_payload(engine)
    assert set(payload) == {
        "meta",
        "conversations",
        "turns",
        "modelCalls",
        "toolCalls",
        "workItems",
        "contextSamples",
        "turnSettings",
        "compactions",
        "subagents",
        "ingestionRuns",
    }
    assert payload["meta"] == {"shareSafe": False}
    assert payload["conversations"][0]["tokenSemantics"] == "additive"
    assert set(payload["conversations"][0]) == {
        "key",
        "provider",
        "tokenSemantics",
        "machine",
        "project",
        "startedAt",
        "endedAt",
        "durationSeconds",
        "models",
        "turns",
        "modelCalls",
        "toolCalls",
        "compactions",
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
    assert set(payload["toolCalls"][0]) == {
        "conversationKey",
        "turnKey",
        "sequence",
        "timestamp",
        "tool",
    }
    assert set(payload["workItems"][0]) == {
        "conversationKey",
        "turnKey",
        "kind",
        "tool",
        "startedAtMs",
        "durationMs",
        "status",
    }
    assert payload["contextSamples"][0]["contextWindowTokens"] == 1000
    assert payload["turnSettings"][0]["effort"] == "high"
    with (output / "conversations.csv").open(encoding="utf-8") as handle:
        assert next(iter(csv.DictReader(handle)))["source_machine"] == "workstation"
    engine.dispose()


def test_share_safe_dashboard_pseudonymizes_labels_and_omits_exact_times(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    engine = create_database_engine(tmp_path / "usage.sqlite")
    ingest_snapshot(engine, CodexAdapter().collect([("private-machine", home)]))

    output = tmp_path / "safe.html"
    generate_dashboard(engine, output, share_safe=True)
    html = output.read_text(encoding="utf-8")

    assert "Share-safe dashboard" in html
    assert "private-machine" not in html
    assert "service" not in html
    assert "gpt-5.6" not in html
    assert '"tool":"exec_command"' not in html
    assert "2026-08-25T10:00" not in html
    assert "machine-1" in html
    assert "project-1" in html
    assert "model-1" in html
    assert "Shell and processes" in html
    assert _round_timestamp("privacy canary") is None
    assert _round_timestamp(None) is None
    assert _round_timestamp("2026-08-25T10:00:00Z") == ("2026-08-25T00:00:00+00:00")
    assert _round_epoch_day(float("inf")) is None
    assert _round_epoch_day(0) == 0
    assert _round_epoch_day(10**30) is None
    assert _tool_category("spawn_agent") == "Agent coordination"
    assert _tool_category("exec_command") == "Shell and processes"
    assert _tool_category("apply_patch") == "Files and workspace"
    assert _tool_category("web__run") == "Web"
    assert _tool_category("update_plan") == "Planning"
    assert _tool_category("image_gen__imagegen") == "Media"
    assert _tool_category("mcp__service__call") == "Integrations"
    assert _tool_category("provider_specific") == "Other"
    engine.dispose()


def test_additive_analytics_tables_compile_for_postgresql() -> None:
    for table_name in (
        "work_items",
        "context_samples",
        "turn_settings",
        "compaction_events",
    ):
        table = cast(Table, TABLES[table_name].__table__)
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert f"CREATE TABLE {table_name}" in ddl
        assert "FOREIGN KEY(conversation_id)" in ddl


def test_existing_database_gains_additive_analytics_tables(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "existing.sqlite")
    original_tables = (
        "conversations",
        "turns",
        "model_calls",
        "tool_calls",
        "subagents",
        "ingestion_runs",
    )
    for table_name in original_tables:
        cast(Table, TABLES[table_name].__table__).create(engine)

    assert "work_items" not in inspect(engine).get_table_names()
    initialize_database(engine)

    assert set(SCHEMA_TABLES) | {"alembic_version"} == set(
        inspect(engine).get_table_names()
    )
    engine.dispose()


def test_richer_replacement_is_atomic_and_older_copy_cannot_regress_it(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    original = CodexAdapter().collect([("desktop", home)])
    engine = create_database_engine(tmp_path / "usage.sqlite")
    ingest_snapshot(engine, original)

    rollout_factory(home, extra_event=True)
    richer = CodexAdapter().collect([("desktop", home)])
    result = ingest_snapshot(engine, richer)
    assert (result.written, result.skipped) == (1, 0)
    assert len(read_table(engine, "work_items")) == 1
    assert len(read_table(engine, "compaction_events")) == 1

    result = ingest_snapshot(engine, original)
    assert (result.written, result.skipped) == (0, 1)
    assert len(read_table(engine, "compaction_events")) == 1

    invalid = Snapshot.from_dict(richer.to_dict())
    invalid.work_items[0]["raw_item"] = "privacy canary"
    with pytest.raises(ValueError, match="invalid_snapshot"):
        ingest_snapshot(engine, invalid)
    assert len(read_table(engine, "compaction_events")) == 1
    assert "privacy canary" not in str(read_table(engine, "work_items"))
    engine.dispose()


def test_identical_snapshot_cannot_delete_subagent_scope(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    snapshot = CodexAdapter().collect([("desktop", home)])
    snapshot.subagents.append(
        {
            "id": "codex:desktop:child-thread",
            "provider": "codex",
            "source_machine": "desktop",
            "parent_thread_id": "conversation-1",
            "child_thread_id": "child-thread",
            "status": "completed",
            "created_at_ms": 1,
            "updated_at_ms": 2,
            "agent_role": "worker",
            "tokens_used": 3,
        }
    )
    engine = create_database_engine(tmp_path / "usage.sqlite")

    ingest_snapshot(engine, snapshot)
    assert {row["id"] for row in read_table(engine, "subagents")} == {
        "codex:desktop:child-thread"
    }

    without_edges = Snapshot.from_dict(snapshot.to_dict())
    without_edges.subagents.clear()
    result = ingest_snapshot(engine, without_edges)

    assert (result.written, result.skipped) == (0, 1)
    assert {row["id"] for row in read_table(engine, "subagents")} == {
        "codex:desktop:child-thread"
    }
    engine.dispose()


def test_older_copy_cannot_regress_subagent_scope_and_newer_deletion_wins(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    older = CodexAdapter().collect([("desktop", home)])
    rollout_factory(home, extra_event=True)
    recent = CodexAdapter().collect([("desktop", home)])
    recent.subagents.append(
        {
            "id": "codex:desktop:recent-child",
            "provider": "codex",
            "source_machine": "desktop",
            "parent_thread_id": "conversation-1",
            "child_thread_id": "recent-child",
            "status": "completed",
            "created_at_ms": 1,
            "updated_at_ms": 2,
            "agent_role": "worker",
            "tokens_used": 3,
        }
    )
    engine = create_database_engine(tmp_path / "usage.sqlite")

    ingest_snapshot(engine, recent)
    first_old = ingest_snapshot(engine, older)
    second_old = ingest_snapshot(engine, older)

    assert (first_old.written, first_old.skipped) == (0, 1)
    assert (second_old.written, second_old.skipped) == (0, 1)
    assert {row["id"] for row in read_table(engine, "subagents")} == {
        "codex:desktop:recent-child"
    }

    identical_stale_graph = Snapshot.from_dict(recent.to_dict())
    identical_stale_graph.subagents[0]["status"] = "failed"
    ingest_snapshot(engine, identical_stale_graph)
    assert read_table(engine, "subagents")[0]["status"] == "completed"

    identical_without_edges = Snapshot.from_dict(recent.to_dict())
    identical_without_edges.subagents.clear()
    ingest_snapshot(engine, identical_without_edges)
    assert {row["id"] for row in read_table(engine, "subagents")} == {
        "codex:desktop:recent-child"
    }

    newer_without_edges = Snapshot.from_dict(recent.to_dict())
    newer_without_edges.subagents.clear()
    newer_without_edges.conversations[0]["event_count"] += 1
    newer_without_edges.conversations[0]["content_hash"] = "f" * 64
    deletion = ingest_snapshot(engine, newer_without_edges)

    assert (deletion.written, deletion.skipped) == (1, 0)
    assert read_table(engine, "subagents") == []
    engine.dispose()


def test_mixed_richer_and_stale_conversations_preserve_scope(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home, extra_event=True)
    stored = CodexAdapter().collect([("desktop", home)])
    stored.conversations[0]["event_count"] = 2
    second = dict(stored.conversations[0])
    second["id"] = "codex:conversation-2"
    second["external_id"] = "conversation-2"
    second["content_hash"] = "e" * 64
    stored.conversations.append(second)
    stored.subagents.append(
        {
            "id": "codex:desktop:child-thread",
            "provider": "codex",
            "source_machine": "desktop",
            "parent_thread_id": "conversation-1",
            "child_thread_id": "child-thread",
            "status": "completed",
            "created_at_ms": 1,
            "updated_at_ms": 2,
            "agent_role": "worker",
            "tokens_used": 3,
        }
    )
    engine = create_database_engine(tmp_path / "usage.sqlite")
    ingest_snapshot(engine, stored)

    mixed = Snapshot.from_dict(stored.to_dict())
    mixed.subagents.clear()
    mixed.conversations[0]["event_count"] = 3
    mixed.conversations[0]["content_hash"] = "d" * 64
    mixed.conversations[1]["event_count"] = 1
    mixed.conversations[1]["content_hash"] = "c" * 64
    result = ingest_snapshot(engine, mixed)

    assert (result.written, result.skipped) == (1, 1)
    assert {row["id"] for row in read_table(engine, "subagents")} == {
        "codex:desktop:child-thread"
    }
    engine.dispose()


def test_graph_only_scope_is_authoritative_only_when_first_seen(tmp_path: Path) -> None:
    def graph(child: str) -> Snapshot:
        return Snapshot(
            provider="codex",
            subagents=[
                {
                    "id": f"codex:desktop:{child}",
                    "provider": "codex",
                    "source_machine": "desktop",
                    "parent_thread_id": "parent",
                    "child_thread_id": child,
                    "status": "completed",
                    "created_at_ms": 1,
                    "updated_at_ms": 2,
                    "agent_role": "worker",
                    "tokens_used": 3,
                }
            ],
        )

    engine = create_database_engine(tmp_path / "usage.sqlite")
    ingest_snapshot(engine, graph("first"))
    ingest_snapshot(engine, graph("unproven-newer"))

    assert {row["id"] for row in read_table(engine, "subagents")} == {
        "codex:desktop:first"
    }
    engine.dispose()


def test_concurrent_sqlite_scope_updates_keep_richest_graph(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    base = CodexAdapter().collect([("desktop", home)])
    base.conversations[0]["event_count"] = 1
    base.conversations[0]["content_hash"] = "1" * 64
    engine = create_database_engine(tmp_path / "usage.sqlite")
    ingest_snapshot(engine, base)

    def version(event_count: int, child: str) -> Snapshot:
        snapshot = Snapshot.from_dict(base.to_dict())
        snapshot.conversations[0]["event_count"] = event_count
        snapshot.conversations[0]["content_hash"] = str(event_count) * 64
        snapshot.subagents.append(
            {
                "id": f"codex:desktop:{child}",
                "provider": "codex",
                "source_machine": "desktop",
                "parent_thread_id": "conversation-1",
                "child_thread_id": child,
                "status": "completed",
                "created_at_ms": event_count,
                "updated_at_ms": event_count,
                "agent_role": "worker",
                "tokens_used": event_count,
            }
        )
        return snapshot

    barrier = Barrier(2)

    def ingest(snapshot: Snapshot) -> None:
        barrier.wait()
        ingest_snapshot(engine, snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ingest, version(0, "stale")),
            executor.submit(ingest, version(3, "newest")),
        ]
        for future in futures:
            future.result()

    assert read_table(engine, "conversations")[0]["event_count"] == 3
    assert {row["id"] for row in read_table(engine, "subagents")} == {
        "codex:desktop:newest"
    }
    engine.dispose()


def test_invalid_analytics_values_are_rejected_without_echoing_content(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home, extra_event=True)
    original = CodexAdapter().collect([("desktop", home)])
    engine = create_database_engine(tmp_path / "usage.sqlite")

    mutations = (
        ("work_items", "status", "privacy_canary"),
        ("context_samples", "timestamp", "privacy canary"),
        ("context_samples", "input_tokens", -1),
        ("turn_settings", "effort", "privacy canary"),
        ("compaction_events", "timestamp", "privacy canary"),
    )
    for collection, field, value in mutations:
        invalid = Snapshot.from_dict(original.to_dict())
        getattr(invalid, collection)[0][field] = value
        with pytest.raises(ValueError) as error:
            ingest_snapshot(engine, invalid)
        assert "privacy" not in str(error.value)

    assert read_table(engine, "conversations") == []
    engine.dispose()


def test_database_urls_support_paths_and_postgresql(tmp_path: Path) -> None:
    assert normalize_database_url(tmp_path / "usage.sqlite").startswith("sqlite:///")
    postgres = "postgresql+psycopg://user:password@db/usage"
    assert normalize_database_url(postgres) == postgres
    engine = create_database_engine("postgresql://user:password@db/usage")
    assert engine.url.drivername == "postgresql+psycopg"
    engine.dispose()


def test_postgresql_driver_error_names_the_optional_dependency(monkeypatch) -> None:
    real_import = builtins.__import__

    def import_without_psycopg(name, *args, **kwargs):
        if name == "psycopg":
            raise ModuleNotFoundError("No module named 'psycopg'", name="psycopg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_psycopg)

    with pytest.raises(RuntimeError, match=r"cli-consumption\[postgres\]"):
        create_database_engine("postgresql+psycopg://user:password@db/usage")
