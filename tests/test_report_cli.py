from __future__ import annotations

import json
from pathlib import Path

import pytest
from report_fixtures import CANARY, PATH_CANARY, seed_report_database
from sqlalchemy import func, select
from typer.testing import CliRunner

from cli_consumption import cli as cli_module
from cli_consumption.adapters.registry import resolve_adapter_spec
from cli_consumption.cli import CollectionFailure, app
from cli_consumption.storage import (
    Conversation,
    IngestionRun,
    create_database_engine,
)

runner = CliRunner()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    directory = tmp_path / f"{CANARY}-private-directory"
    directory.mkdir()
    path = directory / "usage.sqlite"
    engine = create_database_engine(path)
    try:
        seed_report_database(engine)
    finally:
        engine.dispose()
    return path


def _assert_private(output: str, *paths: Path) -> None:
    assert CANARY not in output
    assert PATH_CANARY not in output
    for path in paths:
        assert str(path) not in output


def _ingestion_runs(path: Path) -> int:
    engine = create_database_engine(path)
    try:
        with engine.connect() as connection:
            return int(
                connection.scalar(select(func.count()).select_from(IngestionRun)) or 0
            )
    finally:
        engine.dispose()


def test_report_prints_plain_tables_without_collecting(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object) -> None:
        raise AssertionError("report must not collect")

    monkeypatch.setattr(cli_module, "_collect_adapter", forbidden)
    monkeypatch.setattr(cli_module, "_collection_inputs", forbidden)
    runs = _ingestion_runs(database)

    result = runner.invoke(
        app,
        ["report", "--database", str(database)],
        env={"COLUMNS": "200", "NO_COLOR": ""},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].startswith("Daily usage, timezone UTC")
    assert "92,505" in result.stdout
    assert "\x1b" not in result.stdout
    assert _ingestion_runs(database) == runs
    _assert_private(result.output, database)


@pytest.mark.parametrize("view", ["daily", "weekly", "monthly", "session"])
@pytest.mark.parametrize("by", ["model", "provider", "project", "machine"])
def test_report_views_and_breakdowns_stay_private(
    database: Path, view: str, by: str
) -> None:
    for share_safe in ([], ["--share-safe"]):
        result = runner.invoke(
            app,
            ["report", view, "--by", by, "--database", str(database), *share_safe],
            env={"COLUMNS": "80"},
        )
        assert result.exit_code == 0, result.output
        assert max(len(line) for line in result.stdout.splitlines()) <= 80
        _assert_private(result.output, database)
        if share_safe:
            for label in ("alpha", "beta", "laptop", "desktop", "gpt-x"):
                assert label not in result.output


def test_report_json_is_deterministic_and_filtered(database: Path) -> None:
    arguments = [
        "report",
        "session",
        "--database",
        str(database),
        "--json",
        "--provider",
        "claude-code",
        "--provider",
        "codex",
        "--since",
        "2026-08-04",
        "--until",
        "2026-08-10",
        "--timezone",
        "Europe/Paris",
        "--by",
        "model",
    ]
    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert first.stdout == second.stdout
    assert first.stdout.count("\n") == 1
    payload = json.loads(first.stdout)
    assert payload["schema_version"] == 1
    assert payload["view"] == "session"
    assert payload["breakdown"] == "model"
    assert payload["timezone"] == "Europe/Paris"
    assert payload["filters"]["providers"] == ["claude", "codex"]
    assert payload["window"] == {
        "since": "2026-08-03T22:00:00.000000+00:00",
        "until": "2026-08-10T22:00:00.000000+00:00",
    }
    assert {row["session"]["provider"] for row in payload["rows"]} == {
        "claude",
        "codex",
    }
    assert (
        first.stdout
        == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    )
    _assert_private(first.output, database)


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["--timezone", f"{CANARY}/Zone"], "invalid_timezone"),
        (["--since", f"{CANARY}"], "invalid_window"),
        (["--since", "2026-08-05", "--until", "2026-08-04"], "invalid_window"),
        (["--provider", f"{CANARY}"], "unknown_provider"),
    ],
)
def test_report_rejects_invalid_parameters_with_fixed_codes(
    database: Path, arguments: list[str], code: str
) -> None:
    result = runner.invoke(
        app, ["report", "--database", str(database), "--json", *arguments]
    )
    text = runner.invoke(app, ["report", "--database", str(database), *arguments])

    assert result.exit_code == text.exit_code == 2
    assert json.loads(result.stdout) == {"error": {"code": code}}
    assert text.stdout == ""
    assert text.stderr
    _assert_private(result.output + text.output, database)


def test_report_without_database_suggests_quick_without_paths(
    tmp_path: Path,
) -> None:
    missing = tmp_path / f"{CANARY}-missing" / "usage.sqlite"

    text = runner.invoke(app, ["report", "--database", str(missing)])
    payload = runner.invoke(app, ["report", "--database", str(missing), "--json"])

    assert text.exit_code == payload.exit_code == 2
    assert "cli-consumption quick" in text.stderr
    assert json.loads(payload.stdout) == {"error": {"code": "database_not_found"}}
    assert not missing.exists()
    assert not missing.parent.exists()
    _assert_private(text.output + payload.output, missing)


def test_report_unreadable_database_uses_a_generic_code(tmp_path: Path) -> None:
    corrupt = tmp_path / f"{CANARY}.sqlite"
    corrupt.write_bytes(b"not a database " + CANARY.encode())

    result = runner.invoke(app, ["report", "--database", str(corrupt), "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"error": {"code": "database_unavailable"}}
    _assert_private(result.output, corrupt)


def test_report_share_safe_limit_is_generic(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "cli_consumption.dashboard.MAX_DASHBOARD_INDEX_BYTES", 1, raising=True
    )

    result = runner.invoke(
        app, ["report", "--database", str(database), "--share-safe", "--json"]
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"error": {"code": "report_limit_exceeded"}}


def test_quick_collects_detected_providers_then_reports_daily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollout_factory
) -> None:
    codex_home = tmp_path / f"{CANARY}-home" / ".codex"
    rollout_factory(codex_home)
    spec = resolve_adapter_spec("codex")
    assert spec is not None
    monkeypatch.setattr(cli_module, "ADAPTER_SPECS", (spec,))
    monkeypatch.setattr(cli_module, "default_source_path", lambda _: codex_home)
    database = tmp_path / "quick" / "usage.sqlite"

    first = runner.invoke(
        app, ["quick", "--database", str(database)], env={"COLUMNS": "160"}
    )
    second = runner.invoke(
        app, ["quick", "--database", str(database), "--json"], env={"COLUMNS": "160"}
    )

    assert first.exit_code == 0, first.output
    assert "Collected codex: 1 written" in first.stderr
    assert first.stdout.startswith("Daily usage, timezone UTC")
    assert "2026-08-25" in first.stdout
    assert database.is_file()
    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["collection"] == {
        "failures": [],
        "ingestions": [
            {"malformed": 0, "provider": "codex", "skipped": 1, "written": 0}
        ],
    }
    assert payload["report"]["view"] == "daily"
    assert [row["period"] for row in payload["report"]["rows"]] == ["2026-08-25"]
    engine = create_database_engine(database)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(select(func.count()).select_from(Conversation)) == 1
            )
    finally:
        engine.dispose()
    _assert_private(first.output + second.output, codex_home, database)


def test_quick_reports_partial_failures_with_fixed_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_fixtures import report_snapshots

    snapshots = {snapshot.provider: snapshot for snapshot in report_snapshots()}
    specs = [resolve_adapter_spec(name) for name in ("claude", "copilot", "codex")]
    monkeypatch.setattr(
        cli_module,
        "_collection_inputs",
        lambda *_args: ([(spec, [("m", tmp_path)]) for spec in specs], []),
    )

    def collect(spec, _sources, _mappings):
        if spec.name == "copilot":
            raise CollectionFailure(
                "copilot",
                "provider_limit_exceeded",
                "Provider 'copilot' data exceeds collection safety limits.",
            )
        return snapshots[spec.name]

    monkeypatch.setattr(cli_module, "_collect_adapter", collect)
    database = tmp_path / "usage.sqlite"

    text = runner.invoke(
        app, ["quick", "--database", str(database)], env={"COLUMNS": "160"}
    )
    payload = runner.invoke(app, ["quick", "--database", str(database), "--json"])

    assert text.exit_code == payload.exit_code == 2
    assert "Provider 'copilot' data exceeds collection safety limits." in text.stderr
    assert "Daily usage" in text.stdout
    result = json.loads(payload.stdout)
    assert result["collection"]["failures"] == [
        {"code": "provider_limit_exceeded", "provider": "copilot"}
    ]
    assert [item["provider"] for item in result["collection"]["ingestions"]] == [
        "claude",
        "codex",
    ]
    assert result["report"]["totals"]["calls"] == 5
    _assert_private(text.output + payload.output, database)


def test_quick_without_detected_providers_still_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "ADAPTER_SPECS", ())
    database = tmp_path / "usage.sqlite"

    result = runner.invoke(app, ["quick", "--database", str(database)])
    invalid = runner.invoke(
        app, ["quick", "--database", str(database), "--timezone", "Nowhere/x", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert "No supported provider data detected." in result.stderr
    assert "No usage recorded for this selection." in result.stdout
    assert invalid.exit_code == 2
    assert json.loads(invalid.stdout) == {"error": {"code": "invalid_timezone"}}
