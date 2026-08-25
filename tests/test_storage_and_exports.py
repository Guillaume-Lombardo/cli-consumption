from __future__ import annotations

import csv
from pathlib import Path

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.dashboard import _dashboard_payload, generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    create_database_engine,
    ingest_snapshot,
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
    assert len(paths) == 6
    html = dashboard.read_text(encoding="utf-8")
    assert "CLI Consumption" in html
    assert "Turn performance" in html
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
        "subagents",
        "ingestion_runs",
    ):
        assert "secret value" not in str(read_table(engine, table))

    payload = _dashboard_payload(engine)
    assert set(payload) == {
        "conversations",
        "turns",
        "modelCalls",
        "toolCalls",
        "subagents",
        "ingestionRuns",
    }
    assert set(payload["conversations"][0]) == {
        "key",
        "provider",
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
    with (output / "conversations.csv").open(encoding="utf-8") as handle:
        assert next(iter(csv.DictReader(handle)))["source_machine"] == "workstation"
    engine.dispose()


def test_database_urls_support_paths_and_postgresql(tmp_path: Path) -> None:
    assert normalize_database_url(tmp_path / "usage.sqlite").startswith("sqlite:///")
    postgres = "postgresql+psycopg://user:password@db/usage"
    assert normalize_database_url(postgres) == postgres
