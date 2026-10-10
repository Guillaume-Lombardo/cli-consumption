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
        "incremental": False,
        "ingestions": [
            {
                "batch_duplicates": 0,
                "batched": False,
                "batches": 1,
                "malformed": 0,
                "provider": "codex",
                "received": 1,
                "skipped": 1,
                "written": 0,
            }
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

    def collect(spec, _sources, _mappings, **_kwargs):
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


def test_quick_reports_invalid_snapshots_with_a_fixed_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_fixtures import report_snapshots

    from cli_consumption.models import SnapshotValidationError

    snapshot = next(item for item in report_snapshots() if item.provider == "claude")
    spec = resolve_adapter_spec("claude")
    monkeypatch.setattr(
        cli_module,
        "_collection_inputs",
        lambda *_args: ([(spec, [("m", tmp_path)])], []),
    )
    monkeypatch.setattr(
        cli_module, "_collect_adapter", lambda *_args, **_kwargs: snapshot
    )

    def reject(*_args: object, **_kwargs: object) -> None:
        raise SnapshotValidationError()

    monkeypatch.setattr(cli_module, "ingest_snapshot", reject)

    result = runner.invoke(
        app, ["quick", "--database", str(tmp_path / "usage.sqlite"), "--json"]
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["collection"] == {
        "failures": [{"code": "invalid_snapshot", "provider": "claude"}],
        "incremental": False,
        "ingestions": [],
    }
    assert payload["report"]["rows"] == []
    _assert_private(result.output, tmp_path)


BAD_DATABASE_URL = f"postgresql+psycopg://user:{CANARY}@db.invalid:{CANARY}/usage"


@pytest.mark.parametrize("command", ["report", "quick"])
def test_invalid_database_urls_use_a_fixed_code(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "ADAPTER_SPECS", ())

    text = runner.invoke(app, [command, "--database", BAD_DATABASE_URL])
    payload = runner.invoke(app, [command, "--database", BAD_DATABASE_URL, "--json"])

    assert text.exit_code == payload.exit_code == 2, text.output
    assert json.loads(payload.stdout) == {"error": {"code": "database_unavailable"}}
    assert text.stderr.strip() == "The usage database is unavailable."
    assert text.exception is None or isinstance(text.exception, SystemExit)
    _assert_private(text.output + payload.output)


def test_quick_database_path_failures_use_a_fixed_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "ADAPTER_SPECS", ())
    blocker = tmp_path / f"{CANARY}-file"
    blocker.write_text(PATH_CANARY, encoding="utf-8")
    database = blocker / "usage.sqlite"

    result = runner.invoke(app, ["quick", "--database", str(database), "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout) == {"error": {"code": "database_unavailable"}}
    _assert_private(result.output, database)


def test_quick_ingestion_failures_never_print_sql_or_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report_fixtures import report_snapshots
    from sqlalchemy import text as sql_text

    database = tmp_path / "usage.sqlite"
    engine = create_database_engine(database)
    try:
        engine.dispose()
        from cli_consumption.storage import initialize_database

        initialize_database(engine)
        with engine.begin() as connection:
            connection.execute(
                sql_text(
                    "CREATE TRIGGER reject_conversations BEFORE INSERT ON "
                    "conversations BEGIN SELECT RAISE(ABORT, 'rejected'); END"
                )
            )
    finally:
        engine.dispose()
    snapshot = next(item for item in report_snapshots() if item.provider == "codex")
    spec = resolve_adapter_spec("codex")
    monkeypatch.setattr(
        cli_module,
        "_collection_inputs",
        lambda *_args: ([(spec, [("m", tmp_path)])], []),
    )
    monkeypatch.setattr(
        cli_module, "_collect_adapter", lambda *_args, **_kwargs: snapshot
    )

    text = runner.invoke(app, ["quick", "--database", str(database)])
    payload = runner.invoke(app, ["quick", "--database", str(database), "--json"])

    assert text.exit_code == payload.exit_code == 2, text.output
    assert json.loads(payload.stdout) == {"error": {"code": "database_unavailable"}}
    assert text.stderr.strip() == "The usage database is unavailable."
    for output in (text.output, payload.output):
        assert "INSERT" not in output
        assert "rejected" not in output
    _assert_private(text.output + payload.output, database, tmp_path)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--until", "9999-12-31"],
        ["--since", "0001-01-01", "--timezone", "Asia/Tokyo"],
        ["--since", "0001-01-01T00:30:00+01:00"],
        ["--until", "9999-12-31T23:00:00Z", "--timezone", "Pacific/Kiritimati"],
        ["--until", "9999-12-31T23:00:00Z", "--share-safe"],
    ],
)
def test_calendar_boundaries_are_invalid_windows(
    database: Path, arguments: list[str]
) -> None:
    payload = runner.invoke(
        app, ["report", "--database", str(database), "--json", *arguments]
    )
    text = runner.invoke(app, ["report", "--database", str(database), *arguments])

    assert payload.exit_code == text.exit_code == 2, text.output
    assert json.loads(payload.stdout) == {"error": {"code": "invalid_window"}}
    assert text.stderr.startswith("Invalid report window")
    assert "Traceback" not in text.output + payload.output


def test_missing_postgresql_driver_uses_a_fixed_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_consumption.storage import MissingOptionalDependencyError

    def missing(_database: str) -> None:
        raise MissingOptionalDependencyError(PATH_CANARY)

    monkeypatch.setattr(cli_module, "create_database_engine", missing)

    result = runner.invoke(
        app, ["report", "--database", "postgresql://db.invalid/usage", "--json"]
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"error": {"code": "database_driver_missing"}}
    _assert_private(result.output)


def _quick_providers(monkeypatch: pytest.MonkeyPatch, homes: dict[str, Path]) -> None:
    specs = tuple(resolve_adapter_spec(name) for name in homes)
    monkeypatch.setattr(cli_module, "ADAPTER_SPECS", specs)
    monkeypatch.setattr(
        cli_module, "default_source_path", lambda spec: homes[spec.name]
    )


def _assert_collection_private(output: str, *paths: Path) -> None:
    from test_incremental_collection import CANARY as CONTENT_CANARY

    _assert_private(output, *paths)
    assert CONTENT_CANARY not in output
    assert "PRIVATE_PATH_CANARY" not in output
    assert "/srv/work/acme/service" not in output


def test_quick_switches_to_bounded_batches_on_aggregate_overrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollout_factory
) -> None:
    from test_incremental_collection import _claude_sessions

    claude_home = _claude_sessions(tmp_path / "PRIVATE_PATH_CANARY" / "claude", 4)
    codex_home = tmp_path / "PRIVATE_PATH_CANARY" / "codex"
    rollout_factory(codex_home)
    _quick_providers(monkeypatch, {"claude": claude_home, "codex": codex_home})
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    database = tmp_path / "usage.sqlite"

    first = runner.invoke(app, ["quick", "--database", str(database), "--json"])
    rerun = runner.invoke(app, ["quick", "--database", str(database), "--json"])
    human = runner.invoke(
        app, ["quick", "--database", str(database)], env={"COLUMNS": "160"}
    )

    assert first.exit_code == rerun.exit_code == human.exit_code == 0, first.output
    collection = json.loads(first.stdout)["collection"]
    assert collection == {
        "failures": [],
        "incremental": True,
        "ingestions": [
            {
                "batch_duplicates": 0,
                "batched": True,
                "batches": 2,
                "malformed": 0,
                "provider": "claude",
                "received": 4,
                "skipped": 0,
                "written": 4,
            },
            {
                "batch_duplicates": 0,
                "batched": False,
                "batches": 1,
                "malformed": 0,
                "provider": "codex",
                "received": 1,
                "skipped": 0,
                "written": 1,
            },
        ],
    }
    report = json.loads(first.stdout)["report"]
    assert report["totals"]["conversations"] == 5
    rerun_payload = json.loads(rerun.stdout)
    assert [item["written"] for item in rerun_payload["collection"]["ingestions"]] == [
        0,
        0,
    ]
    assert rerun_payload["report"] == report
    assert "collected in bounded batches: claude." in human.stderr
    assert "Collected claude in 2 bounded batches: 0 written" in human.stderr
    assert human.stdout.startswith("Daily usage")
    _assert_collection_private(
        first.output + rerun.output + human.output, tmp_path, database
    )


def test_quick_keeps_other_providers_after_a_failed_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollout_factory
) -> None:
    from test_claude_adapter import _agent_events, _write_jsonl
    from test_incremental_collection import _claude_sessions, _session_events

    claude_home = _claude_sessions(tmp_path / "PRIVATE_PATH_CANARY" / "claude", 2)
    project = claude_home / "projects" / "p"
    _write_jsonl(project / "z.jsonl", _session_events("z", "m-z"))
    nested = project / "z" / "subagents"
    _write_jsonl(nested / "agent-a.jsonl", _agent_events("a", "m-a", session_id="z"))
    target = tmp_path / "outside.meta.json"
    target.write_text(json.dumps({"agentType": "Explore"}), encoding="utf-8")
    (nested / "agent-a.meta.json").symlink_to(target)
    codex_home = tmp_path / "PRIVATE_PATH_CANARY" / "codex"
    rollout_factory(codex_home)
    _quick_providers(monkeypatch, {"claude": claude_home, "codex": codex_home})
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 3)
    monkeypatch.setattr(
        "cli_consumption.adapters.claude.INCREMENTAL_CANDIDATES_PER_BATCH", 1
    )
    database = tmp_path / "usage.sqlite"

    payload = runner.invoke(app, ["quick", "--database", str(database), "--json"])
    human = runner.invoke(
        app, ["quick", "--database", str(database)], env={"COLUMNS": "160"}
    )

    assert payload.exit_code == human.exit_code == 2, payload.output
    collection = json.loads(payload.stdout)["collection"]
    assert collection["failures"] == [
        {"code": "provider_limit_exceeded", "provider": "claude"}
    ]
    claude, codex = collection["ingestions"]
    assert (claude["provider"], claude["batched"], claude["batches"]) == (
        "claude",
        True,
        2,
    )
    assert (codex["provider"], codex["written"]) == ("codex", 1)
    assert json.loads(payload.stdout)["report"]["totals"]["conversations"] == 3
    assert "2 earlier batch(es) were committed; rerun is safe." in human.stderr
    assert human.stdout.startswith("Daily usage")
    _assert_collection_private(payload.output + human.output, tmp_path, database)


def test_quick_enforces_one_batch_ceiling_per_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollout_factory
) -> None:
    from test_incremental_collection import _claude_sessions

    claude_home = _claude_sessions(tmp_path / "claude", 4)
    codex_home = tmp_path / "codex"
    rollout_factory(codex_home)
    _quick_providers(monkeypatch, {"claude": claude_home, "codex": codex_home})
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    monkeypatch.setattr(cli_module, "MAX_INCREMENTAL_BATCHES", 2)

    result = runner.invoke(
        app, ["quick", "--database", str(tmp_path / "usage.sqlite"), "--json"]
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["collection"]["failures"] == [
        {"code": "provider_limit_exceeded", "provider": "codex"}
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--since", "2026-08-10T12:00:00.000500Z"],
        ["--until", "2026-08-10T12:00:00.0005+02:00"],
        ["--since", "2026-08-10T12:00:00.123456+00:00"],
    ],
)
def test_sub_millisecond_bounds_are_invalid_windows(
    database: Path, arguments: list[str]
) -> None:
    payload = runner.invoke(
        app, ["report", "--database", str(database), "--json", *arguments]
    )
    text = runner.invoke(app, ["report", "--database", str(database), *arguments])

    assert payload.exit_code == text.exit_code == 2, text.output
    assert json.loads(payload.stdout) == {"error": {"code": "invalid_window"}}
    assert text.stderr.startswith("Invalid report window")


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-10T12:00:00.001Z",
        "2026-08-10T12:00:00.123000+02:00",
        "2026-08-10T12:00:00Z",
    ],
)
def test_millisecond_bounds_are_accepted(database: Path, value: str) -> None:
    result = runner.invoke(
        app, ["report", "--database", str(database), "--json", "--since", value]
    )

    assert result.exit_code == 0, result.output
