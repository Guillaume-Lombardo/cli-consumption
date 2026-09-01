from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

import cli_consumption.reporting_api as reporting_api_module
from cli_consumption.api import create_app
from cli_consumption.dashboard import _dashboard_payload
from cli_consumption.models import Snapshot, empty_tokens
from cli_consumption.reporting import ReportFilters, parse_export_window
from cli_consumption.reporting_api import (
    DashboardQuery,
    PaginationStore,
    PrivateExportResponse,
    ReportingError,
    ReportingRuntime,
)
from cli_consumption.storage import (
    create_database_engine,
    ingest_snapshot,
)

CANARY = "PRIVACY_CANARY_REPORTING_SECRET"
READ_VALUE = "-".join(("read", "token"))
EXPORT_VALUE = "-".join(("export", "token"))
READ_HEADERS = {"Authorization": f"Bearer {READ_VALUE}"}
EXPORT_HEADERS = {"Authorization": f"Bearer {EXPORT_VALUE}"}


def _snapshot(
    provider: str,
    *,
    external_id: str,
    machine: str,
    project: str,
    model: str,
    started_at: str,
) -> Snapshot:
    conversation_id = f"{provider}:{external_id}"
    turn_id = f"{conversation_id}:turn"
    return Snapshot(
        provider=provider,
        conversations=[
            {
                "id": conversation_id,
                "provider": provider,
                "external_id": external_id,
                "source_machine": machine,
                "project": project,
                "project_source": "none",
                "started_at": started_at,
                "ended_at": started_at,
                "duration_seconds": 1.0,
                "source": CANARY,
                "models": [model],
                "iterations": 1,
                "model_calls": 1,
                "tool_calls": 0,
                "compactions": 0,
                "event_count": 2,
                "content_hash": "a" * 64,
                **empty_tokens(),
            }
        ],
        turns=[
            {
                "id": turn_id,
                "conversation_id": conversation_id,
                "external_id": "turn",
                "started_at": started_at,
                "ended_at": started_at,
                "status": "completed",
                "duration_ms": 1_000,
                "time_to_first_token_ms": 10,
                "model_calls": 1,
                "tool_calls": 0,
                **empty_tokens(),
            }
        ],
        model_calls=[
            {
                "id": f"{turn_id}:call",
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "sequence": 0,
                "timestamp": started_at,
                "model": model,
                **empty_tokens(),
            }
        ],
    )


def _query(
    *,
    providers: list[str] | None = None,
    projects: list[str] | None = None,
    models: list[str] | None = None,
    profile: str = "detailed",
) -> dict[str, object]:
    return {
        "version": 1,
        "window": {
            "since": "2026-08-01T00:00:00Z",
            "until": "2026-09-01T00:00:00Z",
        },
        "filters": {
            "providers": providers or [],
            "machines": [],
            "projects": projects or [],
            "models": models or [],
        },
        "profile": profile,
    }


def _engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "reporting.sqlite")
    ingest_snapshot(
        engine,
        _snapshot(
            "codex",
            external_id=CANARY,
            machine="machine-a",
            project="project-a",
            model="model-a",
            started_at="2026-08-10T00:00:00Z",
        ),
    )
    ingest_snapshot(
        engine,
        _snapshot(
            "gemini",
            external_id="conversation-b",
            machine="machine-b",
            project="project-b",
            model="model-b",
            started_at="2026-08-20T00:00:00Z",
        ),
    )
    return engine


@pytest.mark.anyio
async def test_reporting_scopes_capabilities_and_cache_policy(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(
        engine,
        "ingest-token",
        read_token=READ_VALUE,
        export_token=EXPORT_VALUE,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        capabilities = (await client.get("/api/v1/capabilities")).json()
        assert capabilities["dashboard_query_versions"] == [1]
        assert capabilities["dashboard_dataset_versions"] == [1]
        assert capabilities["cursor_versions"] == [1]
        assert capabilities["max_reporting_request_bytes"] == 64 * 1024

        missing = await client.post("/api/v1/reporting/dashboard", json=_query())
        ingestion_only = await client.post(
            "/api/v1/reporting/dashboard",
            json=_query(),
            headers={"Authorization": "Bearer ingest-token"},
        )
        read = await client.post(
            "/api/v1/reporting/dashboard", json=_query(), headers=READ_HEADERS
        )
        lowercase_bearer = await client.post(
            "/api/v1/reporting/dashboard",
            json=_query(),
            headers={"Authorization": f"bearer {READ_VALUE}"},
        )
        missing_scheme = await client.post(
            "/api/v1/reporting/dashboard",
            json=_query(),
            headers={"Authorization": READ_VALUE},
        )
        read_export = await client.post(
            "/api/v1/reporting/export", json=_query(), headers=READ_HEADERS
        )
        export = await client.post(
            "/api/v1/reporting/export", json=_query(), headers=EXPORT_HEADERS
        )
        read_on_ingest = await client.post(
            "/api/v1/snapshots",
            json={"provider": "codex"},
            headers=READ_HEADERS,
        )

    assert missing.status_code == 401
    assert missing.json() == {"detail": "authentication_required"}
    assert ingestion_only.status_code == 403
    assert ingestion_only.json() == {"detail": "authorization_denied"}
    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"
    assert lowercase_bearer.status_code == 200
    assert missing_scheme.status_code == 401
    assert read_export.status_code == 403
    assert export.status_code == 200
    assert export.headers["cache-control"] == "no-store"
    assert export.headers["content-disposition"].endswith(
        'filename="cli-consumption-dashboard.html"'
    )
    assert b"__CLI_CONSUMPTION_STREAMED_PAYLOAD__" not in export.content
    assert b'<div id="root"></div>' in export.content
    assert b"https://" not in export.content
    assert CANARY.encode() not in export.content
    assert read_on_ingest.status_code == 403
    engine.dispose()


def test_export_runtime_uses_a_private_react_temporary(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    path = ReportingRuntime(engine).export(DashboardQuery.model_validate(_query()))
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        html = path.read_text(encoding="utf-8")
        assert '<div id="root"></div>' in html
        assert "offline_dashboard_root_missing" in html
        assert CANARY not in html
    finally:
        path.unlink(missing_ok=True)
        engine.dispose()


@pytest.mark.anyio
async def test_export_response_removes_temporary_when_streaming_is_cancelled(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private-export.html"
    path.write_text("private export", encoding="utf-8")
    path.chmod(0o600)
    response = PrivateExportResponse(path)

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def disconnected(_message: object) -> None:
        raise RuntimeError("client_disconnected")

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/export",
        "raw_path": b"/export",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    with pytest.raises(RuntimeError, match="client_disconnected"):
        await response(scope, receive, disconnected)
    assert not path.exists()


def test_empty_or_whitespace_credentials_are_rejected_generically(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "invalid-credential.sqlite")

    for invalid_value in ("", " ", "line\nbreak"):
        with pytest.raises(ValueError, match="invalid API credential configuration"):
            create_app(engine, read_token=invalid_value)

    engine.dispose()


def test_duplicate_credentials_are_rejected_generically(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "duplicate-credential.sqlite")

    for credentials in (
        {"api_token": READ_VALUE, "read_token": READ_VALUE},
        {"read_token": READ_VALUE, "export_token": READ_VALUE},
        {"api_token": READ_VALUE, "export_token": READ_VALUE},
    ):
        with pytest.raises(ValueError, match="invalid API credential configuration"):
            create_app(engine, **credentials)

    engine.dispose()


@pytest.mark.anyio
async def test_dashboard_matches_offline_dataset_and_excludes_private_fields(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    app = create_app(engine, read_token=READ_VALUE)
    query = _query(providers=["codex"], projects=["project-a"], models=["model-a"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        response = await client.post(
            "/api/v1/reporting/dashboard", json=query, headers=READ_HEADERS
        )
        filters = await client.post(
            "/api/v1/reporting/filters",
            json={key: value for key, value in query.items() if key != "profile"},
            headers=READ_HEADERS,
        )
        share_safe = await client.post(
            "/api/v1/reporting/dashboard",
            json=_query(providers=["codex"], profile="share-safe"),
            headers=READ_HEADERS,
        )

    assert response.status_code == filters.status_code == 200
    payload = response.json()
    expected = _dashboard_payload(
        engine,
        window=parse_export_window("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z"),
        filters=ReportFilters(
            providers=("codex",),
            projects=("project-a",),
            models=("model-a",),
        ),
    )
    for section in (
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
    ):
        assert payload[section] == expected[section]
    assert payload["contractVersion"] == 1
    assert payload["profile"] == "detailed"
    assert payload["filters"] == {
        "providers": ["codex"],
        "machines": ["machine-a"],
        "projects": ["project-a"],
        "models": ["model-a"],
    }
    assert filters.json()["filters"] == payload["filters"]
    assert share_safe.status_code == 200
    safe_payload = share_safe.json()
    assert safe_payload["conversations"][0]["machine"] == "machine-1"
    assert safe_payload["conversations"][0]["project"] == "project-1"
    assert safe_payload["conversations"][0]["models"] == ["model-1"]
    assert safe_payload["filters"] == {
        "providers": ["codex"],
        "machines": ["machine-1"],
        "projects": ["project-1"],
        "models": ["model-1"],
    }
    encoded = json.dumps(payload)
    assert CANARY not in encoded
    for prohibited in (
        "external_id",
        "content_hash",
        "source",
        "project_source",
        "run_id",
        "idempotency_key",
    ):
        assert prohibited not in encoded
    engine.dispose()


@pytest.mark.anyio
async def test_reporting_validation_and_body_limit_are_generic(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    transport = httpx.ASGITransport(app=create_app(engine, read_token=READ_VALUE))
    duplicate_filter = _query()
    duplicate_filter["filters"] = {
        "providers": ["codex", "codex"],
        "machines": [],
        "projects": [],
        "models": [],
    }
    invalid_payloads = [
        {**_query(), "unknown": CANARY},
        duplicate_filter,
        {
            **_query(),
            "window": {
                "since": "2026-08-01T00:00:00",
                "until": "2026-09-01T00:00:00Z",
            },
        },
    ]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        responses = [
            await client.post(
                "/api/v1/reporting/dashboard", json=payload, headers=READ_HEADERS
            )
            for payload in invalid_payloads
        ]
        oversized = await client.post(
            "/api/v1/reporting/dashboard",
            content=b"{}",
            headers={**READ_HEADERS, "Content-Length": str(64 * 1024 + 1)},
        )

    assert all(response.status_code == 422 for response in responses)
    assert all(
        response.json() == {"detail": "invalid_reporting_request"}
        for response in responses
    )
    assert CANARY not in "".join(response.text for response in responses)
    assert oversized.status_code == 413
    assert oversized.json() == {"detail": "request_too_large"}
    assert oversized.headers["cache-control"] == "no-store"
    engine.dispose()


@pytest.mark.anyio
async def test_reporting_concurrency_fails_immediately_without_a_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    slots = reporting_api_module.MAX_CONCURRENT_REPORTS
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0

    def blocked_dataset(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal active
        with lock:
            active += 1
            if active == slots:
                started.set()
        assert release.wait(timeout=5)
        return {
            "meta": {},
            "conversations": [],
            "turns": [],
            "modelCalls": [],
            "toolCalls": [],
            "workItems": [],
            "contextSamples": [],
            "turnSettings": [],
            "compactions": [],
            "subagents": [],
            "ingestionRuns": [],
        }

    monkeypatch.setattr(
        reporting_api_module, "build_dashboard_dataset", blocked_dataset
    )
    transport = httpx.ASGITransport(app=create_app(engine, read_token=READ_VALUE))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        running = [
            asyncio.create_task(
                client.post(
                    "/api/v1/reporting/dashboard",
                    json=_query(),
                    headers=READ_HEADERS,
                )
            )
            for _ in range(slots)
        ]
        assert await asyncio.to_thread(started.wait, 5)
        busy = await client.post(
            "/api/v1/reporting/dashboard", json=_query(), headers=READ_HEADERS
        )
        release.set()
        completed = await asyncio.gather(*running)

    assert busy.status_code == 503
    assert busy.json() == {"detail": "reporting_busy"}
    assert busy.headers["cache-control"] == "no-store"
    assert all(response.status_code == 200 for response in completed)
    engine.dispose()


@pytest.mark.anyio
async def test_reporting_failures_exclude_values_from_body_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = _engine(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(reporting_api_module, "build_dashboard_dataset", fail)
    transport = httpx.ASGITransport(app=create_app(engine, read_token=READ_VALUE))
    with caplog.at_level(logging.ERROR, logger="cli_consumption.api"):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://collector.test"
        ) as client:
            response = await client.post(
                "/api/v1/reporting/dashboard",
                json=_query(projects=[CANARY]),
                headers=READ_HEADERS,
            )

    assert response.status_code == 500
    assert response.json() == {"detail": "internal_server_error"}
    assert response.headers["cache-control"] == "no-store"
    assert CANARY not in response.text
    assert CANARY not in caplog.text
    assert READ_VALUE not in caplog.text
    engine.dispose()


@pytest.mark.anyio
async def test_pagination_is_stable_and_detail_uses_opaque_reference(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    transport = httpx.ASGITransport(app=create_app(engine, read_token=READ_VALUE))
    request = {
        "query": _query(),
        "sort": "provider",
        "direction": "asc",
        "pageSize": 1,
        "cursor": None,
    }
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        first = await client.post(
            "/api/v1/reporting/conversations", json=request, headers=READ_HEADERS
        )
        first_payload = first.json()
        reference = first_payload["items"][0]["conversationRef"]
        cursor = first_payload["nextCursor"]
        ingest_snapshot(
            engine,
            _snapshot(
                "aider",
                external_id="new-earlier-provider",
                machine="machine-c",
                project="project-c",
                model="model-c",
                started_at="2026-08-15T00:00:00Z",
            ),
        )
        second = await client.post(
            "/api/v1/reporting/conversations",
            json={**request, "cursor": cursor},
            headers=READ_HEADERS,
        )
        detail = await client.post(
            "/api/v1/reporting/conversation",
            json={"query": _query(), "conversationRef": reference},
            headers=READ_HEADERS,
        )
        mismatched = await client.post(
            "/api/v1/reporting/conversations",
            json={
                **request,
                "query": _query(providers=["gemini"]),
                "cursor": cursor,
            },
            headers=READ_HEADERS,
        )
        malformed_reference = await client.post(
            "/api/v1/reporting/conversation",
            json={"query": _query(), "conversationRef": CANARY},
            headers=READ_HEADERS,
        )

    assert first.status_code == second.status_code == detail.status_code == 200
    assert first_payload["items"][0]["provider"] == "codex"
    assert "key" not in first_payload["items"][0]
    assert second.json()["items"][0]["provider"] == "gemini"
    assert "aider" not in second.text
    assert len(reference) == 32
    assert detail.json()["conversation"]["provider"] == "codex"
    assert detail.json()["conversation"]["key"] == 0
    assert detail.json()["turns"][0]["key"] == 0
    assert detail.json()["turns"][0]["conversationKey"] == 0
    assert detail.json()["modelCalls"][0]["turnKey"] == 0
    assert len(detail.json()["turns"]) == 1
    assert CANARY not in detail.text
    assert mismatched.status_code == 400
    assert mismatched.json() == {"detail": "invalid_cursor"}
    assert malformed_reference.status_code == 404
    assert malformed_reference.json() == {"detail": "conversation_not_found"}
    assert CANARY not in malformed_reference.text
    assert malformed_reference.headers["cache-control"] == "no-store"
    engine.dispose()


def test_pagination_handles_expire_without_disclosing_state() -> None:
    now = [0.0]
    store = PaginationStore(clock=lambda: now[0])
    session, _record = store.create(
        fingerprint="list-fingerprint",
        query_fingerprint="query-fingerprint",
        rows=[("private-database-id", {"provider": "codex"})],
    )
    cursor = store.next_cursor(session, 0)
    now[0] = 301.0

    with pytest.raises(ReportingError) as expired:
        store.page(cursor, fingerprint="list-fingerprint")

    assert expired.value.code == "pagination_expired"
    assert "private-database-id" not in str(expired.value)


def test_pagination_session_limit_fails_before_retaining_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PaginationStore()
    monkeypatch.setattr(reporting_api_module, "MAX_PAGINATION_SESSION_BYTES", 1)

    with pytest.raises(ReportingError) as limited:
        store.create(
            fingerprint="list-fingerprint",
            query_fingerprint="query-fingerprint",
            rows=[("private-database-id", {"provider": "codex"})],
        )

    assert limited.value.code == "reporting_limit_exceeded"
    assert store._sessions == {}
    assert store._references == {}


def test_database_deadline_is_reduced_to_a_fixed_timeout_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instants = iter((0.0, 15.0))
    monkeypatch.setattr(reporting_api_module.time, "monotonic", lambda: next(instants))

    with (
        pytest.raises(ReportingError) as timed_out,
        ReportingRuntime._slot(threading.BoundedSemaphore(1), 15.0),
    ):
        raise RuntimeError(CANARY)

    assert timed_out.value.code == "reporting_timeout"
    assert CANARY not in str(timed_out.value)


@pytest.mark.anyio
async def test_postgresql_reporting_matches_sqlite_contract_when_configured(
    tmp_path: Path,
) -> None:
    database_url = os.environ.get("TEST_POSTGRESQL_URL")
    if database_url is None:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    schema_name = f"reporting_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    scoped_engine = None
    schema_created = False
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema_name))
        schema_created = True
        scoped_url = make_url(database_url).update_query_dict(
            {"options": f"-csearch_path={schema_name}"}
        )
        scoped_engine = create_engine(scoped_url)
        ingest_snapshot(
            scoped_engine,
            _snapshot(
                "codex",
                external_id="postgres-conversation",
                machine="postgres-machine",
                project="postgres-project",
                model="postgres-model",
                started_at="2026-08-10T00:00:00Z",
            ),
        )
        transport = httpx.ASGITransport(
            app=create_app(scoped_engine, read_token=READ_VALUE)
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://collector.test"
        ) as client:
            dashboard = await client.post(
                "/api/v1/reporting/dashboard",
                json=_query(models=["postgres-model"]),
                headers=READ_HEADERS,
            )
            page = await client.post(
                "/api/v1/reporting/conversations",
                json={"query": _query(), "pageSize": 50},
                headers=READ_HEADERS,
            )

        assert dashboard.status_code == page.status_code == 200
        assert dashboard.json()["conversations"][0]["project"] == "postgres-project"
        assert page.json()["items"][0]["provider"] == "codex"
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        if schema_created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema_name, cascade=True))
        admin_engine.dispose()
