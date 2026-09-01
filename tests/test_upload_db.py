from __future__ import annotations

import builtins
import json
import uuid
from pathlib import Path
from typing import cast

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient
from typer.testing import CliRunner

import cli_consumption.cli as cli_module
from cli_consumption.api import create_app
from cli_consumption.cli import app
from cli_consumption.models import Snapshot, empty_tokens
from cli_consumption.snapshot_extraction import (
    SnapshotExtractionError,
    extract_snapshots,
)
from cli_consumption.storage import (
    Conversation,
    IngestionRun,
    create_database_engine,
    ingest_snapshot,
)
from cli_consumption.sync import SyncClient, snapshot_idempotency_key

CANARY = "PROMPT_SECRET_UPLOAD_DB_CANARY"
RUN_ID = "12345678-1234-4abc-8def-123456789abc"
runner = CliRunner()


def _snapshot(provider: str, *, event_count: int = 1) -> Snapshot:
    return Snapshot(
        provider=provider,
        conversations=[
            {
                "id": f"{provider}:conversation",
                "provider": provider,
                "external_id": "conversation",
                "source_machine": "machine",
                "project": "project",
                "project_source": "none",
                "started_at": "2026-08-15T00:00:00Z",
                "ended_at": None,
                "duration_seconds": None,
                "source": "synthetic",
                "models": [],
                "iterations": event_count,
                "model_calls": 0,
                "tool_calls": 0,
                "compactions": 0,
                "event_count": event_count,
                "content_hash": f"{event_count}" * 64,
                **empty_tokens(),
            }
        ],
    )


def test_database_upload_replays_identical_snapshot_and_replaces_richer_copy(
    tmp_path: Path,
) -> None:
    local_database = tmp_path / "local.sqlite"
    local_engine = create_database_engine(local_database)
    ingest_snapshot(local_engine, _snapshot("codex", event_count=1))
    [first] = extract_snapshots(local_database)
    first_key = snapshot_idempotency_key(first)

    central_engine = create_database_engine(tmp_path / "central.sqlite")
    with (
        TestClient(create_app(central_engine, "test-token")) as transport,
        SyncClient(
            "http://testserver",
            "test-token",
            allow_insecure=True,
            client=cast(httpx.Client, transport),
        ) as client,
    ):
        client.require_idempotent_uploads()
        initial = client.send_snapshot(first, idempotency_key=first_key)
        replay = client.send_snapshot(first, idempotency_key=first_key)
        assert replay == initial

        ingest_snapshot(local_engine, _snapshot("codex", event_count=2))
        [richer] = extract_snapshots(local_database)
        richer_key = snapshot_idempotency_key(richer)
        assert richer_key != first_key
        replaced = client.send_snapshot(richer, idempotency_key=richer_key)
        assert replaced["written"] == 1
        assert client.send_snapshot(richer, idempotency_key=richer_key) == replaced

    with Session(central_engine) as session:
        assert session.scalar(select(func.count()).select_from(IngestionRun)) == 2
        conversation = session.get(Conversation, "codex:conversation")
        assert conversation is not None
        assert conversation.event_count == 2
        assert conversation.content_hash == "2" * 64

    query = {
        "version": 1,
        "window": {"since": None, "until": None},
        "filters": {
            "providers": [],
            "machines": [],
            "projects": [],
            "models": [],
        },
        "profile": "detailed",
    }
    read_token = str(uuid.uuid4())
    export_token = str(uuid.uuid4())
    reporting_app = create_app(
        central_engine,
        "ingest-token",
        read_token=read_token,
        export_token=export_token,
    )
    with TestClient(reporting_app) as reporting:
        dashboard = reporting.post(
            "/api/v1/reporting/dashboard",
            json=query,
            headers={"Authorization": f"Bearer {read_token}"},
        )
        exported = reporting.post(
            "/api/v1/reporting/export",
            json=query,
            headers={"Authorization": f"Bearer {export_token}"},
        )
    assert dashboard.status_code == 200
    assert dashboard.json()["conversations"][0]["project"] == "project"
    assert exported.status_code == 200
    assert '<div id="root"></div>' in exported.text
    assert "https://" not in exported.text
    local_engine.dispose()
    central_engine.dispose()


def test_upload_db_json_is_deterministic_bounded_and_replay_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = [_snapshot("gemini"), _snapshot("codex"), _snapshot("claude")]
    extracted: list[tuple[str, str | None, str | None]] = []
    observed_tokens: list[str | None] = []
    sent: list[tuple[str, str]] = []

    def extract(database: str, *, since: str | None, until: str | None):
        extracted.append((database, since, until))
        return snapshots

    class FakeUploadClient:
        def __init__(
            self, _endpoint: str, token: str | None, **_kwargs: object
        ) -> None:
            observed_tokens.append(token)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None: ...

        def require_idempotent_uploads(self) -> None: ...

        def send_snapshot(
            self, snapshot: Snapshot, *, idempotency_key: str
        ) -> dict[str, int | str]:
            sent.append((snapshot.provider, idempotency_key))
            return {
                "run_id": RUN_ID,
                "received": 1,
                "written": 1,
                "skipped": 0,
            }

    monkeypatch.setattr(cli_module, "extract_snapshots", extract)
    monkeypatch.setattr("cli_consumption.sync.SyncClient", FakeUploadClient)
    monkeypatch.setenv("PRIVATE_TOKEN_ENV", CANARY)
    database = str(tmp_path / f"{CANARY}.sqlite")
    arguments = [
        "upload-db",
        "--database",
        database,
        "--endpoint",
        "https://collector.test",
        "--since",
        "2026-08-01",
        "--until",
        "2026-08-31",
        "--token-env",
        "PRIVATE_TOKEN_ENV",
        "--json",
    ]

    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)

    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout
    assert json.loads(first.stdout) == {
        "complete": True,
        "uploads": [
            {
                "provider": provider,
                "received": 1,
                "run_id": RUN_ID,
                "skipped": 0,
                "status": "succeeded",
                "written": 1,
            }
            for provider in ("claude", "codex", "gemini")
        ],
    }
    assert extracted == [
        (database, "2026-08-01", "2026-08-31"),
        (database, "2026-08-01", "2026-08-31"),
    ]
    assert observed_tokens == [CANARY, CANARY]
    first_keys = sent[:3]
    second_keys = sent[3:]
    assert first_keys == second_keys
    assert [provider for provider, _key in first_keys] == [
        "claude",
        "codex",
        "gemini",
    ]
    assert all(uuid.UUID(key).version == 4 for _provider, key in sent)
    assert all(key not in first.stdout for _provider, key in sent)
    assert CANARY not in first.output
    assert str(tmp_path) not in first.output


@pytest.mark.parametrize("strict", [False, True], ids=["continue", "strict"])
def test_upload_db_reports_partial_results_and_strict_skips_remaining(
    monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    snapshots = [_snapshot("gemini"), _snapshot("codex"), _snapshot("claude")]
    attempted: list[str] = []

    class PartiallyFailingClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None: ...

        def require_idempotent_uploads(self) -> None: ...

        def send_snapshot(
            self, snapshot: Snapshot, *, idempotency_key: str
        ) -> dict[str, int | str]:
            attempted.append(snapshot.provider)
            assert idempotency_key == snapshot_idempotency_key(snapshot)
            if snapshot.provider == "codex":
                raise ValueError(f"{CANARY} /private/remote/body")
            return {
                "run_id": RUN_ID,
                "received": 1,
                "written": 1,
                "skipped": 0,
            }

    monkeypatch.setattr(cli_module, "extract_snapshots", lambda *_a, **_k: snapshots)
    monkeypatch.setattr("cli_consumption.sync.SyncClient", PartiallyFailingClient)
    arguments = ["upload-db", "--endpoint", "https://collector.test", "--json"]
    if strict:
        arguments.append("--strict")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["complete"] is False
    assert [item["provider"] for item in payload["uploads"]] == [
        "claude",
        "codex",
        "gemini",
    ]
    assert payload["uploads"][1] == {
        "provider": "codex",
        "status": "failed",
        "error": {"code": "remote_upload_failed"},
    }
    if strict:
        assert attempted == ["claude", "codex"]
        assert payload["uploads"][2] == {
            "provider": "gemini",
            "status": "skipped",
            "error": {"code": "strict_upload_stopped"},
        }
    else:
        assert attempted == ["claude", "codex", "gemini"]
        assert payload["uploads"][2]["status"] == "succeeded"
    assert CANARY not in result.output
    assert "/private/" not in result.output


def test_upload_db_refuses_incompatible_database_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = False

    def fail_extraction(*_args: object, **_kwargs: object):
        raise SnapshotExtractionError("incompatible_database")

    class UnusedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal created
            created = True

    monkeypatch.setattr(cli_module, "extract_snapshots", fail_extraction)
    monkeypatch.setattr("cli_consumption.sync.SyncClient", UnusedClient)

    result = runner.invoke(
        app,
        ["upload-db", "--endpoint", "https://collector.test", "--json"],
    )

    assert result.exit_code == 2
    assert created is False
    assert json.loads(result.stdout) == {
        "complete": False,
        "error": {"code": "incompatible_database"},
        "uploads": [],
    }


def test_upload_db_empty_selection_completes_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = False

    class UnusedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal created
            created = True

    monkeypatch.setattr(cli_module, "extract_snapshots", lambda *_a, **_k: [])
    monkeypatch.setattr("cli_consumption.sync.SyncClient", UnusedClient)

    result = runner.invoke(
        app,
        ["upload-db", "--endpoint", "https://collector.test", "--json"],
    )

    assert result.exit_code == 0
    assert created is False
    assert json.loads(result.stdout) == {"complete": True, "uploads": []}


def test_upload_db_requires_replay_capability_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_consumption.sync import IdempotencyUnsupportedError

    posted = False

    class LegacyClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None: ...

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None: ...

        def require_idempotent_uploads(self) -> None:
            raise IdempotencyUnsupportedError

        def send_snapshot(self, *_args: object, **_kwargs: object):
            nonlocal posted
            posted = True

    monkeypatch.setattr(
        cli_module, "extract_snapshots", lambda *_a, **_k: [_snapshot("codex")]
    )
    monkeypatch.setattr("cli_consumption.sync.SyncClient", LegacyClient)

    result = runner.invoke(
        app,
        ["upload-db", "--endpoint", "https://collector.test", "--json"],
    )

    assert result.exit_code == 2
    assert posted is False
    assert json.loads(result.stdout) == {
        "complete": False,
        "error": {"code": "idempotency_unsupported"},
        "uploads": [],
    }


def test_upload_db_missing_dependency_is_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def import_without_sync(name, *args, **kwargs):
        if name == "cli_consumption.sync":
            raise ModuleNotFoundError(CANARY, name="httpx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_sync)
    missing = runner.invoke(
        app,
        ["upload-db", "--endpoint", "https://collector.test", "--json"],
    )
    assert missing.exit_code == 2
    assert json.loads(missing.stdout) == {
        "complete": False,
        "error": {"code": "upload_dependency_missing"},
        "uploads": [],
    }
    assert CANARY not in missing.output
