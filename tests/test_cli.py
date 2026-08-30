from __future__ import annotations

import builtins
import json
import sqlite3
from pathlib import Path
from typing import Any

from click.utils import strip_ansi
from storage_helpers import read_table
from typer.testing import CliRunner

import cli_consumption.cli as cli_module
from cli_consumption.adapters.registry import AdapterSpec
from cli_consumption.cli import app
from cli_consumption.models import Snapshot
from cli_consumption.storage import (
    create_database_engine,
    initialize_database,
)

runner = CliRunner()


def normalized_cli_output(output: str) -> str:
    """Remove terminal formatting and wrapping from CLI output assertions."""
    return " ".join(strip_ansi(output).split())


def test_provider_status_is_explicit() -> None:
    result = runner.invoke(app, ["providers"])
    assert result.exit_code == 0
    assert "all      auto-detect" in result.stdout
    assert "aider    supported" in result.stdout
    assert "amazon-q supported" in result.stdout
    assert "amp      supported" in result.stdout
    assert "codex    supported" in result.stdout
    assert "copilot  supported" in result.stdout
    assert "continue supported" in result.stdout
    assert "crush    supported" in result.stdout
    assert "cursor   supported" in result.stdout
    assert "gemini   supported" in result.stdout
    assert "goose    supported" in result.stdout
    assert "grok     supported" in result.stdout
    assert "claude   supported" in result.stdout
    assert "cline    supported" in result.stdout
    assert "kilo     supported" in result.stdout
    assert "kimi     supported" in result.stdout
    assert "mistral-vibe supported" in result.stdout
    assert "opencode supported" in result.stdout
    assert "openhands supported" in result.stdout
    assert "pi       supported" in result.stdout
    assert "plandex  supported" in result.stdout
    assert "qwen     supported" in result.stdout


def test_provider_json_is_deterministic_and_does_not_leak_diagnostic_errors(
    tmp_path: Path, monkeypatch
) -> None:
    canary = "PROMPT_SECRET_CANARY"
    marker = tmp_path / "marker"
    marker.write_text(canary, encoding="utf-8")

    class LeakyAdapter:
        name = "synthetic"

        def collect(
            self,
            sources: list[tuple[str, Path]],
            project_mappings: list[tuple[str, str]],
        ) -> Snapshot:
            content = (sources[0][1] / "marker").read_text(encoding="utf-8")
            raise ValueError(f"{sources[0][1]}: {content}")

    spec = AdapterSpec("synthetic", LeakyAdapter, ".synthetic", ("marker",))
    monkeypatch.setattr(cli_module, "ADAPTER_SPECS", (spec,))
    monkeypatch.setattr(cli_module, "default_source_path", lambda _: tmp_path)

    first = runner.invoke(app, ["providers", "--json"])
    second = runner.invoke(app, ["providers", "--json"])

    assert first.exit_code == 0, first.output
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == {
        "schema_version": 2,
        "providers": [
            {
                "aliases": [],
                "name": "synthetic",
                "status": "degraded",
                "support": "supported",
                "token_semantics": "unavailable",
            }
        ],
    }
    assert canary not in first.stdout
    assert str(tmp_path) not in first.stdout
    assert first.stderr == ""


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


def test_sync_reports_missing_optional_dependencies_without_a_traceback(
    monkeypatch,
) -> None:
    real_import = builtins.__import__

    def import_without_sync(name, *args, **kwargs):
        if name == "cli_consumption.sync":
            raise ModuleNotFoundError("No module named 'httpx'", name="httpx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_sync)
    result = runner.invoke(app, ["sync", "--endpoint", "https://collector.test"])

    assert result.exit_code == 2
    assert "cli-consumption[sync]" in normalized_cli_output(result.output)
    assert "Traceback" not in result.output

    json_result = runner.invoke(
        app,
        ["sync", "--endpoint", "https://collector.test", "--json"],
    )
    assert json_result.exit_code == 2
    assert json.loads(json_result.stdout) == {
        "complete": False,
        "error": {"code": "sync_dependency_missing"},
        "synchronizations": [],
    }
    assert json_result.stderr == ""


def test_sync_reuses_one_client_and_reports_generic_partial_failure(
    monkeypatch,
) -> None:
    canary = "PROMPT_SECRET_CANARY"
    snapshots = [
        Snapshot(provider="codex", duplicate_conversations=2),
        Snapshot(provider="claude", malformed_records=1),
    ]
    observed: dict[str, Any] = {"created": 0, "providers": [], "closed": False}

    class FakeSyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            observed["created"] += 1

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            observed["closed"] = True

        def send_snapshot(self, snapshot: Snapshot) -> dict[str, int | str]:
            observed["providers"].append(snapshot.provider)
            if snapshot.provider == "claude":
                raise ValueError(f"/private/provider/path: {canary}")
            return {"run_id": "run-1", "received": 0, "written": 0, "skipped": 0}

    monkeypatch.setattr(cli_module, "_collect_snapshots", lambda *_args: snapshots)
    monkeypatch.setattr("cli_consumption.sync.SyncClient", FakeSyncClient)

    result = runner.invoke(app, ["sync", "--endpoint", "https://collector.test"])

    assert result.exit_code == 2
    assert "Remote ingestion codex run-1" in result.output
    assert "0 malformed, 2 duplicates" in normalized_cli_output(result.output)
    assert "Remote ingestion claude failed" in normalized_cli_output(result.output)
    assert "partially completed: 1 succeeded, 1 failed" in normalized_cli_output(
        result.output
    )
    assert canary not in result.output
    assert "/private/" not in result.output
    assert observed == {
        "created": 1,
        "providers": ["codex", "claude"],
        "closed": True,
    }


def test_sync_json_is_deterministic_and_reports_partial_diagnostics(
    monkeypatch,
) -> None:
    canary = "PROMPT_SECRET_CANARY"
    snapshots = [
        Snapshot(provider="codex", duplicate_conversations=2),
        Snapshot(provider="claude", malformed_records=1),
        Snapshot(provider="gemini"),
    ]

    class FakeSyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None: ...

        def send_snapshot(self, snapshot: Snapshot) -> dict[str, int | str]:
            if snapshot.provider == "claude":
                raise ValueError(f"/private/provider/path: {canary}")
            return {
                "run_id": f"run-{snapshot.provider}",
                "received": 3,
                "written": 2,
                "skipped": 1,
            }

    monkeypatch.setattr(cli_module, "_collect_snapshots", lambda *_args: snapshots)
    monkeypatch.setattr("cli_consumption.sync.SyncClient", FakeSyncClient)

    first = runner.invoke(
        app, ["sync", "--endpoint", "https://collector.test", "--json"]
    )
    second = runner.invoke(
        app, ["sync", "--endpoint", "https://collector.test", "--json"]
    )

    assert first.exit_code == second.exit_code == 2
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == {
        "complete": False,
        "synchronizations": [
            {
                "duplicates": 2,
                "malformed": 0,
                "provider": "codex",
                "received": 3,
                "run_id": "run-codex",
                "skipped": 1,
                "status": "succeeded",
                "written": 2,
            },
            {
                "duplicates": 0,
                "error": {"code": "remote_sync_failed"},
                "malformed": 1,
                "provider": "claude",
                "status": "failed",
            },
            {
                "duplicates": 0,
                "malformed": 0,
                "provider": "gemini",
                "received": 3,
                "run_id": "run-gemini",
                "skipped": 1,
                "status": "succeeded",
                "written": 2,
            },
        ],
    }
    assert first.stderr == ""
    assert canary not in first.stdout
    assert "/private/" not in first.stdout


def test_sync_json_reports_complete_success(monkeypatch) -> None:
    snapshot = Snapshot(
        provider="codex",
        malformed_records=1,
        duplicate_conversations=2,
    )

    class FakeSyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None: ...

        def send_snapshot(self, _snapshot: Snapshot) -> dict[str, int | str]:
            return {
                "run_id": "run-1",
                "received": 4,
                "written": 3,
                "skipped": 1,
            }

    monkeypatch.setattr(cli_module, "_collect_snapshots", lambda *_args: [snapshot])
    monkeypatch.setattr("cli_consumption.sync.SyncClient", FakeSyncClient)

    result = runner.invoke(
        app, ["sync", "--endpoint", "https://collector.test", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "complete": True,
        "synchronizations": [
            {
                "duplicates": 2,
                "malformed": 1,
                "provider": "codex",
                "received": 4,
                "run_id": "run-1",
                "skipped": 1,
                "status": "succeeded",
                "written": 3,
            }
        ],
    }


def test_sync_client_setup_errors_are_generic_in_human_and_json_output(
    monkeypatch,
) -> None:
    canary = "PROMPT_SECRET_CANARY"

    class FailingSyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise ValueError(f"/private/provider/path: {canary}")

    monkeypatch.setattr(
        cli_module,
        "_collect_snapshots",
        lambda *_args: [Snapshot(provider="codex")],
    )
    monkeypatch.setattr("cli_consumption.sync.SyncClient", FailingSyncClient)

    human = runner.invoke(app, ["sync", "--endpoint", "https://collector.test"])
    machine = runner.invoke(
        app, ["sync", "--endpoint", "https://collector.test", "--json"]
    )

    assert human.exit_code == machine.exit_code == 2
    assert "Remote synchronization failed" in human.stderr
    assert json.loads(machine.stdout) == {
        "complete": False,
        "error": {"code": "remote_sync_failed"},
        "synchronizations": [],
    }
    combined = human.output + machine.output
    assert canary not in combined
    assert "/private/" not in combined


def test_sync_collection_errors_are_generic_in_human_and_json_output(
    monkeypatch,
) -> None:
    canary = "PROMPT_SECRET_CANARY"

    def fail_collection(*_args: object) -> list[Snapshot]:
        raise ValueError(f"/private/provider/path: {canary}")

    monkeypatch.setattr(cli_module, "_collect_snapshots", fail_collection)

    human = runner.invoke(app, ["sync", "--endpoint", "https://collector.test"])
    machine = runner.invoke(
        app, ["sync", "--endpoint", "https://collector.test", "--json"]
    )

    assert human.exit_code == machine.exit_code == 2
    assert "Local provider collection failed" in human.stderr
    assert json.loads(machine.stdout) == {
        "complete": False,
        "error": {"code": "local_collection_failed"},
        "synchronizations": [],
    }
    combined = human.output + machine.output
    assert canary not in combined
    assert "/private/" not in combined


def test_sync_strict_refuses_the_complete_batch_before_transport(monkeypatch) -> None:
    snapshots = [
        Snapshot(provider="codex"),
        Snapshot(
            provider="claude",
            malformed_records=1,
            duplicate_conversations=2,
        ),
    ]
    created = False

    class UnusedSyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal created
            created = True

    monkeypatch.setattr(cli_module, "_collect_snapshots", lambda *_args: snapshots)
    monkeypatch.setattr("cli_consumption.sync.SyncClient", UnusedSyncClient)

    result = runner.invoke(
        app,
        [
            "sync",
            "--endpoint",
            "https://collector.test",
            "--strict",
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert created is False
    assert json.loads(result.stdout) == {
        "complete": False,
        "error": {"code": "malformed_records"},
        "synchronizations": [
            {
                "duplicates": 0,
                "malformed": 0,
                "provider": "codex",
                "status": "refused",
            },
            {
                "duplicates": 2,
                "malformed": 1,
                "provider": "claude",
                "status": "refused",
            },
        ],
    }


def test_serve_reports_missing_optional_dependencies_without_a_traceback(
    monkeypatch,
) -> None:
    real_import = builtins.__import__

    def import_without_server(name, *args, **kwargs):
        if name == "uvicorn":
            raise ModuleNotFoundError("No module named 'uvicorn'", name="uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_server)
    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "cli-consumption[server]" in normalized_cli_output(result.output)
    assert "Traceback" not in result.output


def test_serve_disposes_engine_when_server_stops_or_fails(monkeypatch) -> None:
    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    observed: dict[str, object] = {}

    def run(app, **kwargs) -> None:
        observed.update(app=app, **kwargs)
        raise RuntimeError("server stopped")

    monkeypatch.setattr(cli_module, "_open_database", lambda _database: engine)
    monkeypatch.setattr("cli_consumption.api.create_app", lambda _engine, _token: "app")
    monkeypatch.setattr("uvicorn.run", run)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 1
    assert engine.disposed is True
    assert observed == {
        "app": "app",
        "host": "127.0.0.1",
        "port": 8765,
        "access_log": False,
    }


def test_postgres_reports_missing_optional_dependencies_without_a_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    real_import = builtins.__import__

    def import_without_postgres(name, *args, **kwargs):
        if name == "psycopg":
            raise ModuleNotFoundError("No module named 'psycopg'", name="psycopg")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_postgres)
    result = runner.invoke(
        app,
        [
            "export",
            "--database",
            "postgresql+psycopg://usage@db/cli_consumption",
            "--output",
            str(tmp_path / "reports"),
        ],
    )

    assert result.exit_code == 2
    assert "cli-consumption[postgres]" in normalized_cli_output(result.output)
    assert "Traceback" not in result.output


def test_retention_is_a_preview_unless_apply_is_explicit(tmp_path: Path) -> None:
    database = tmp_path / "retention.sqlite"
    engine = create_database_engine(database)
    initialize_database(engine)
    engine.dispose()

    preview = runner.invoke(
        app,
        [
            "retention",
            "--keep-days",
            "30",
            "--database",
            str(database),
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert "Preview retention" in preview.stdout

    applied = runner.invoke(
        app,
        [
            "retention",
            "--keep-days",
            "30",
            "--database",
            str(database),
            "--apply",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert "Applied retention" in applied.stdout

    machine = runner.invoke(
        app,
        [
            "retention",
            "--keep-days",
            "30",
            "--database",
            str(database),
            "--json",
        ],
    )
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.stdout) == {
        "applied": False,
        "conversations": 0,
        "cutoff": json.loads(machine.stdout)["cutoff"],
        "ingestion_runs": 0,
        "subagents": 0,
    }


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


def test_collects_grok_build(tmp_path: Path) -> None:
    home = tmp_path / "grok"
    session_dir = home / "sessions" / "%2Fproject" / "grok-cli"
    session_dir.mkdir(parents=True)
    (session_dir / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": "grok-cli", "cwd": "/project"},
                "created_at": "2026-08-27T10:00:00Z",
                "updated_at": "2026-08-27T10:00:01Z",
                "num_messages": 0,
                "current_model_id": "grok-4.6-build",
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "grok",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "grok.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion grok" in result.stdout
    assert "1 written" in result.stdout


def test_collects_continue_cli(tmp_path: Path) -> None:
    home = tmp_path / "continue"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session-1.json").write_text(
        json.dumps(
            {
                "sessionId": "session-1",
                "workspaceDirectory": "/project",
                "history": [
                    {
                        "message": {"role": "user", "content": "synthetic"},
                        "contextItems": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "continue",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "continue.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion continue" in result.stdout
    assert "1 written" in result.stdout


def test_collects_mistral_vibe_cli(tmp_path: Path) -> None:
    home = tmp_path / "vibe"
    session_dir = home / "logs" / "session" / "session_cli"
    session_dir.mkdir(parents=True)
    (session_dir / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "vibe-cli",
                "start_time": "2026-08-27T10:00:00Z",
                "stats": {"steps": 0},
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "messages.jsonl").write_text(
        json.dumps({"role": "user", "message_id": "message-cli"}) + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "mistral-vibe",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "mistral-vibe.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion mistral-vibe" in result.stdout
    assert "1 written" in result.stdout


def test_collects_openhands_cli(tmp_path: Path) -> None:
    home = tmp_path / "openhands"
    conversation = home / "conversations" / "openhands-cli"
    events = conversation / "events"
    events.mkdir(parents=True)
    (conversation / "base_state.json").write_text(
        json.dumps(
            {
                "id": "openhands-cli",
                "execution_status": "idle",
                "stats": {"usage_to_metrics": {}},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "collect",
            "--provider",
            "openhands",
            "--source",
            f"desktop={home}",
            "--database",
            str(tmp_path / "openhands.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Ingestion openhands" in result.stdout
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
    continue_home = tmp_path / "continue"
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
    continue_path = continue_home / "sessions" / "session.json"
    continue_path.parent.mkdir(parents=True)
    continue_path.write_text(
        json.dumps(
            {
                "sessionId": "continue-session",
                "history": [{"message": {"role": "user", "content": "synthetic"}}],
            }
        ),
        encoding="utf-8",
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
            f"desktop-continue={continue_home}",
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
    assert "Ingestion continue" in result.stdout
    assert "Ingestion crush" in result.stdout
    assert "Ingestion kilo" in result.stdout
    engine = create_database_engine(database)
    try:
        assert {row["provider"] for row in read_table(engine, "conversations")} == {
            "codex",
            "claude",
            "continue",
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


def test_collect_and_export_machine_readable_results(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout = rollout_factory(home)
    database = tmp_path / "usage.sqlite"

    collected = runner.invoke(
        app,
        [
            "collect",
            "--source",
            f"desktop={home}",
            "--database",
            str(database),
            "--json",
        ],
    )
    assert collected.exit_code == 0, collected.output
    payload = json.loads(collected.stdout)
    assert payload["ingestions"][0] | {"run_id": "ignored"} == {
        "provider": "codex",
        "run_id": "ignored",
        "received": 1,
        "written": 1,
        "skipped": 0,
        "malformed": 0,
        "duplicates": 0,
    }

    reports = tmp_path / "reports"
    exported = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(database),
            "--output",
            str(reports),
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout) == {
        "files": ["dashboard.html"],
        "written": 1,
    }

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    refused_database = tmp_path / "strict.sqlite"
    refused = runner.invoke(
        app,
        [
            "collect",
            "--source",
            f"desktop={home}",
            "--database",
            str(refused_database),
            "--strict",
        ],
    )
    assert refused.exit_code == 2
    assert "malformed provider" in normalized_cli_output(refused.output)
    assert not refused_database.exists()


def test_export_accepts_bounded_dates_and_rejects_naive_timestamps(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    database = tmp_path / "usage.sqlite"
    collected = runner.invoke(
        app,
        [
            "collect",
            "--source",
            f"desktop={home}",
            "--database",
            str(database),
        ],
    )
    assert collected.exit_code == 0, collected.output

    reports = tmp_path / "bounded"
    result = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(database),
            "--output",
            str(reports),
            "--since",
            "2026-08-25",
            "--until",
            "2026-08-25",
        ],
    )
    assert result.exit_code == 0, result.output
    html = (reports / "dashboard.html").read_text(encoding="utf-8")
    assert '"since":"2026-08-25T00:00:00.000000+00:00"' in html
    assert '"until":"2026-08-26T00:00:00.000000+00:00"' in html

    invalid = runner.invoke(
        app,
        [
            "export",
            "--database",
            str(database),
            "--since",
            "2026-08-25T10:00:00",
        ],
    )
    assert invalid.exit_code == 2
    assert "invalid export window" in normalized_cli_output(invalid.output)
