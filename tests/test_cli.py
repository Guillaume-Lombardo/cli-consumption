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
    assert "aider    supported" in result.stdout
    assert "amp      supported" in result.stdout
    assert "codex    supported" in result.stdout
    assert "copilot  supported" in result.stdout
    assert "crush    supported" in result.stdout
    assert "cursor   supported" in result.stdout
    assert "gemini   supported" in result.stdout
    assert "goose    supported" in result.stdout
    assert "claude   supported" in result.stdout
    assert "kilo     supported" in result.stdout
    assert "opencode supported" in result.stdout
    assert "pi       supported" in result.stdout
    assert "qwen     supported" in result.stdout


def test_version_and_unsupported_provider_are_explicit(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()

    result = runner.invoke(
        app,
        ["collect", "--provider", "unknown", "--database", str(tmp_path / "db")],
    )
    assert result.exit_code == 2
    assert "not implemented yet" in result.output


def test_collects_github_copilot_cli(tmp_path: Path) -> None:
    home = tmp_path / "copilot"
    events = home / "session-state" / "session-1" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps(
            {
                "id": "start-1",
                "timestamp": "2026-08-27T10:00:00Z",
                "parentId": None,
                "type": "session.start",
                "data": {
                    "sessionId": "session-1",
                    "version": 1,
                    "producer": "copilot-agent",
                    "copilotVersion": "1.0.80",
                    "startTime": "2026-08-27T10:00:00Z",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "copilot",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "copilot.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion copilot" in result.stdout
    assert "1 written" in result.stdout


def test_collects_kilo_code(tmp_path: Path) -> None:
    home = tmp_path / "kilo"
    home.mkdir()
    connection = sqlite3.connect(home / "kilo.db")
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO session VALUES ('ses_cli', 1000, 2000)")
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("msg_cli", "ses_cli", 1000, 1000, json.dumps({"role": "user"})),
    )
    connection.commit()
    connection.close()

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "kilo",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "kilo.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion kilo" in result.stdout
    assert "1 written" in result.stdout


def test_collects_crush(tmp_path: Path) -> None:
    home = tmp_path / "project"
    data = home / ".crush"
    data.mkdir(parents=True)
    connection = sqlite3.connect(data / "crush.db")
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, parent_session_id TEXT,
            prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            parts TEXT NOT NULL, model TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        INSERT INTO sessions VALUES ('crush-cli', NULL, 0, 0, 1000, 2000);
        """
    )
    connection.commit()
    connection.close()

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "crush",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "crush.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion crush" in result.stdout
    assert "1 written" in result.stdout


def test_collects_cursor_cli(tmp_path: Path) -> None:
    home = tmp_path / "cursor"
    session_id = "11111111-2222-3333-4444-555555555555"
    transcript = (
        home
        / "projects"
        / "srv-work-project"
        / "agent-transcripts"
        / session_id
        / f"{session_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"role":"user","message":{"content":[{"type":"text",'
        '"text":"<user_query>synthetic</user_query>"}]}}\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "cursor",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "cursor.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion cursor" in result.stdout
    assert "1 written" in result.stdout

    all_result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "all",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "all.sqlite"),
        ],
    )
    assert all_result.exit_code == 0, all_result.output
    assert all_result.stdout.count("Ingestion ") == 1
    assert "Ingestion cursor" in all_result.stdout


def test_collects_goose(tmp_path: Path) -> None:
    home = tmp_path / "goose"
    home.mkdir()
    connection = sqlite3.connect(home / "sessions.db")
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, working_dir TEXT NOT NULL,
            created_at TIMESTAMP, updated_at TIMESTAMP,
            provider_name TEXT, model_config_json TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, message_id TEXT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content_json TEXT NOT NULL,
            created_timestamp INTEGER NOT NULL, metadata_json TEXT
        );
        CREATE TABLE usage_ledger (
            id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
            created_timestamp INTEGER NOT NULL, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            is_compaction INTEGER DEFAULT 0
        );
        INSERT INTO sessions VALUES (
            'goose-cli', '/project', '2026-08-25T10:00:00Z',
            '2026-08-25T10:00:01Z', 'openai', '{"model_name":"gpt-5"}'
        );
        """
    )
    connection.commit()
    connection.close()

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "goose",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "goose.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion goose" in result.stdout
    assert "1 written" in result.stdout


def test_collects_aider(tmp_path: Path) -> None:
    home = tmp_path / "aider"
    home.mkdir()
    (home / "analytics.jsonl").write_text(
        '{"event":"launched","properties":{},"user_id":"synthetic",'
        '"time":1777300000}\n'
        '{"event":"exit","properties":{},"user_id":"synthetic",'
        '"time":1777300001}\n'
    )

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "aider",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "aider.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion aider" in result.stdout
    assert "1 written" in result.stdout


def test_collects_amp(tmp_path: Path) -> None:
    home = tmp_path / "amp"
    path = home / "threads" / "T-cli.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"id":"T-cli","created":1787652000000,"messages":'
        '[{"role":"user","content":"synthetic"}]}',
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "amp",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "amp.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion amp" in result.stdout
    assert "1 written" in result.stdout


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


def test_collects_gemini_cli(tmp_path: Path) -> None:
    home = tmp_path / "gemini"
    path = home / "tmp" / "project" / "chats" / "session-main.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"sessionId":"session","projectHash":"hash",'
        '"startTime":"2026-08-25T10:00:00Z"}\n'
        '{"id":"prompt","type":"user",'
        '"timestamp":"2026-08-25T10:00:01Z","content":"synthetic"}\n'
    )
    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "gemini",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "gemini.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion gemini" in result.stdout
    assert "1 written" in result.stdout


def test_collects_qwen_code(tmp_path: Path) -> None:
    home = tmp_path / "qwen"
    path = home / "projects" / "-project" / "chats" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"uuid":"prompt","parentUuid":null,"sessionId":"session",'
        '"timestamp":"2026-08-26T10:00:00Z","type":"user",'
        '"cwd":"/project","version":"0.22.2",'
        '"message":{"role":"user","parts":[{"text":"synthetic"}]}}\n'
    )
    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "qwen",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "qwen.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion qwen" in result.stdout
    assert "1 written" in result.stdout


def test_collects_all_detected_providers(tmp_path: Path, rollout_factory) -> None:
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    crush_home = tmp_path / "crush-project"
    kilo_home = tmp_path / "kilo"
    rollout_factory(codex_home)
    claude_path = claude_home / "projects" / "project" / "session.jsonl"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(
        '{"type":"user","sessionId":"claude-session","uuid":"prompt",'
        '"timestamp":"2026-08-25T10:00:00Z",'
        '"message":{"role":"user","content":"synthetic"}}\n'
    )
    kilo_home.mkdir()
    connection = sqlite3.connect(kilo_home / "kilo.db")
    connection.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        INSERT INTO session VALUES ('kilo-session', 1000, 2000);
        """
    )
    connection.commit()
    connection.close()
    crush_data = crush_home / ".crush"
    crush_data.mkdir(parents=True)
    connection = sqlite3.connect(crush_data / "crush.db")
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, parent_session_id TEXT,
            prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            parts TEXT NOT NULL, model TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        INSERT INTO sessions VALUES ('crush-session', NULL, 0, 0, 1000, 2000);
        """
    )
    connection.commit()
    connection.close()
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
            "--source",
            f"desktop-crush={crush_home}",
            "--source",
            f"desktop-kilo={kilo_home}",
            "--database",
            str(database),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion codex" in result.stdout
    assert "Ingestion claude" in result.stdout
    assert "Ingestion crush" in result.stdout
    assert "Ingestion kilo" in result.stdout
    engine = create_database_engine(database)
    try:
        assert {row["provider"] for row in read_table(engine, "conversations")} == {
            "codex",
            "claude",
            "crush",
            "kilo",
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
