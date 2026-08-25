from __future__ import annotations

import csv
from pathlib import Path

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.dashboard import generate_dashboard
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
    snapshot = CodexAdapter().collect([("workstation", home)])
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
    assert "CLI Consumption" in dashboard.read_text(encoding="utf-8")
    assert "https://" not in dashboard.read_text(encoding="utf-8")
    with (output / "conversations.csv").open(encoding="utf-8") as handle:
        assert next(iter(csv.DictReader(handle)))["source_machine"] == "workstation"
    engine.dispose()


def test_database_urls_support_paths_and_postgresql(tmp_path: Path) -> None:
    assert normalize_database_url(tmp_path / "usage.sqlite").startswith("sqlite:///")
    postgres = "postgresql+psycopg://user:password@db/usage"
    assert normalize_database_url(postgres) == postgres
