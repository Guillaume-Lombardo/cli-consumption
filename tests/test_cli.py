from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from cli_consumption.cli import app

runner = CliRunner()


def test_provider_status_is_explicit() -> None:
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0
    assert "codex    supported" in result.stdout
    assert "claude   planned" in result.stdout


def test_version_and_unsupported_provider_are_explicit(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()

    result = runner.invoke(
        app,
        ["collect", "--provider", "claude", "--database", str(tmp_path / "db")],
    )
    assert result.exit_code == 2
    assert "not implemented yet" in result.output


def test_collect_and_export(tmp_path: Path, rollout_factory) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    database = tmp_path / "usage.sqlite"
    result = runner.invoke(
        app,
        [
            "collect",
            "--source",
            f"desktop={home}",
            "--database",
            str(database),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 written" in result.stdout

    reports = tmp_path / "reports"
    result = runner.invoke(
        app,
        ["export", "--database", str(database), "--output", str(reports)],
    )
    assert result.exit_code == 0, result.output
    assert (reports / "dashboard.html").is_file()

    safe_reports = tmp_path / "safe-reports"
    result = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(database),
            "--output",
            str(safe_reports),
            "--share-safe",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [path.name for path in safe_reports.iterdir()] == ["dashboard.html"]
    assert "desktop" not in (safe_reports / "dashboard.html").read_text()

    (safe_reports / "detailed.csv").write_text("sensitive", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(database),
            "--output",
            str(safe_reports),
            "--share-safe",
        ],
    )
    assert result.exit_code == 2
    assert "must be empty" in result.output

    result = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(database),
            "--share-safe",
            "--no-dashboard",
        ],
    )
    assert result.exit_code == 2
    assert "requires --dashboard" in result.output
