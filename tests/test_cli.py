from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.utils import strip_ansi
from typer.testing import CliRunner

from cli_consumption.cli import app
from cli_consumption.storage import create_database_engine, read_table

runner = CliRunner()


def normalized_cli_output(output: str) -> str:
    """Remove terminal formatting and wrapping from CLI output assertions."""
    return " ".join(strip_ansi(output).split())


def test_provider_status_is_explicit() -> None:
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0
    assert "all      auto-detect" in result.stdout
    assert "codex    supported" in result.stdout
    assert "claude   supported" in result.stdout
    assert "opencode supported" in result.stdout
    assert "pi       supported" in result.stdout


def test_version_and_unsupported_provider_are_explicit(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()

    result = runner.invoke(
        app,
        ["collect", "--provider", "kilo", "--database", str(tmp_path / "db")],
    )
    assert result.exit_code == 2
    assert "not implemented yet" in result.output


def test_collects_claude_code(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    path = home / "projects" / "project" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"type":"user","sessionId":"session","uuid":"prompt",'
        '"timestamp":"2026-08-25T10:00:00Z",'
        '"message":{"role":"user","content":"synthetic"}}\n'
    )
    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "claude-code",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "claude.sqlite"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 written" in result.stdout


def test_collects_opencode(tmp_path: Path) -> None:
    home = tmp_path / "opencode"
    home.mkdir()
    connection = sqlite3.connect(home / "opencode.db")
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        );
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            type TEXT NOT NULL,
            seq INTEGER NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO session VALUES ('ses_cli', 1000, 2000)")
    connection.execute(
        "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("msg_cli", "ses_cli", "user", 1, 1000, 1000, json.dumps({"text": "x"})),
    )
    connection.commit()
    connection.close()

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "opencode",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "opencode.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion opencode" in result.stdout
    assert "1 written" in result.stdout


def test_collects_pi(tmp_path: Path) -> None:
    home = tmp_path / "pi"
    path = home / "sessions" / "--project--" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"type":"session","version":3,"id":"session-cli",'
        '"timestamp":"2026-08-25T10:00:00Z","cwd":"/project"}\n'
        '{"type":"message","id":"prompt","parentId":null,'
        '"timestamp":"2026-08-25T10:00:01Z",'
        '"message":{"role":"user","content":"synthetic"}}\n'
    )
    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "pi",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "pi.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion pi" in result.stdout
    assert "1 written" in result.stdout


def test_collects_all_detected_providers(tmp_path: Path, rollout_factory) -> None:
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    rollout_factory(codex_home)
    claude_path = claude_home / "projects" / "project" / "session.jsonl"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(
        '{"type":"user","sessionId":"claude-session","uuid":"prompt",'
        '"timestamp":"2026-08-25T10:00:00Z",'
        '"message":{"role":"user","content":"synthetic"}}\n'
    )
    database = tmp_path / "all.sqlite"

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "all",
            "--source",
            f"desktop-codex={codex_home}",
            "--source",
            f"desktop-claude={claude_home}",
            "--database",
            str(database),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion codex" in result.stdout
    assert "Ingestion claude" in result.stdout
    engine = create_database_engine(database)
    try:
        assert {row["provider"] for row in read_table(engine, "conversations")} == {
            "codex",
            "claude",
        }
    finally:
        engine.dispose()

    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(
        app,
        ["collect", "--provider", "all", "--source", f"empty={empty}"],
    )
    assert result.exit_code == 2
    assert (
        "No supported provider data detected for source labels: empty" in result.output
    )


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
    assert [path.name for path in reports.iterdir()] == ["dashboard.html"]

    csv_reports = tmp_path / "csv-reports"
    result = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(database),
            "--output",
            str(csv_reports),
            "--csv",
            "--no-dashboard",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (csv_reports / "conversations.csv").is_file()
    assert not (csv_reports / "dashboard.html").exists()

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
    assert "requires --dashboard" in normalized_cli_output(result.output)

    result = runner.invoke(
        app,
        ["export", "--database", str(database), "--share-safe", "--csv"],
    )
    assert result.exit_code == 2
    assert "cannot be combined" in normalized_cli_output(result.output)

    result = runner.invoke(
        app,
        ["export", "--database", str(database), "--no-dashboard"],
    )
    assert result.exit_code == 2
    assert "enable --dashboard or --csv" in normalized_cli_output(result.output)
