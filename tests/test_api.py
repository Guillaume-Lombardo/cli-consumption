from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.types import Message, Scope

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.api import (
    RequestSizeLimitMiddleware,
    SafeExceptionBoundary,
    create_app,
)
from cli_consumption.storage import create_database_engine, read_table


@pytest.mark.anyio
async def test_collector_requires_token_and_ingests_snapshot(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    snapshot = CodexAdapter().collect([("laptop", home)])
    snapshot.conversations[0]["started_at"] = "2026-08-25T12:00:00+02:00"
    engine = create_database_engine(tmp_path / "central.sqlite")
    transport = httpx.ASGITransport(app=create_app(engine, "test-token"))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        health = (await client.get("/health")).json()
        assert health["snapshot_schema_min"] == 1
        assert health["snapshot_schema_max"] == 1
        readiness = await client.get("/ready")
        assert readiness.status_code == 200
        assert readiness.json() == {"status": "ready"}
        assert readiness.headers["x-request-id"]
        capabilities = (await client.get("/api/v1/capabilities")).json()
        assert capabilities["max_request_bytes"] == 32 * 1024 * 1024
        assert capabilities["max_snapshot_records"] == 250_000
        assert (
            await client.post("/api/v1/snapshots", json=snapshot.to_dict())
        ).status_code == 401
        response = await client.post(
            "/api/v1/snapshots",
            json=snapshot.to_dict(),
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        assert response.json()["written"] == 1
        assert read_table(engine, "conversations")[0]["source_machine"] == "laptop"
        assert read_table(engine, "conversations")[0]["started_at"] == (
            "2026-08-25T10:00:00.000000+00:00"
        )
        assert read_table(engine, "work_items")[0]["kind"] == "command"
        assert read_table(engine, "context_samples")[0]["input_tokens"] == 100

        invalid = snapshot.to_dict()
        invalid["conversations"][0]["prompt"] = "privacy canary"
        response = await client.post(
            "/api/v1/snapshots",
            json=invalid,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
        assert "privacy canary" not in response.text

        invalid = snapshot.to_dict()
        invalid["work_items"][0]["kind"] = "privacy_canary"
        response = await client.post(
            "/api/v1/snapshots",
            json=invalid,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
        assert "privacy_canary" not in response.text

        legacy = snapshot.to_dict()
        legacy.pop("schema_version")
        response = await client.post(
            "/api/v1/snapshots",
            json=legacy,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200

        unsupported = snapshot.to_dict()
        unsupported["schema_version"] = 2
        response = await client.post(
            "/api/v1/snapshots",
            json=unsupported,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "unsupported_schema_version"}

        invalid = snapshot.to_dict()
        invalid["conversations"][0]["source_machine"] = "privacy canary\nsecret"
        response = await client.post(
            "/api/v1/snapshots",
            json=invalid,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_snapshot"}
        assert "privacy canary" not in response.text

        response = await client.post(
            "/api/v1/snapshots",
            content=b"{}",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Length": str(32 * 1024 * 1024 + 1),
            },
        )
        assert response.status_code == 413
        assert response.json() == {"detail": "request_too_large"}
        assert response.headers["x-request-id"]
    engine.dispose()


@pytest.mark.anyio
async def test_request_limit_counts_streamed_chunks() -> None:
    chunks: Iterator[Message] = iter(
        (
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
        )
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return next(chunks)

    async def send(message: Message) -> None:
        sent.append(message)

    async def consume_body(scope, receive, send) -> None:
        while (await receive()).get("more_body"):
            pass

    middleware = RequestSizeLimitMiddleware(consume_body, maximum=3)
    scope: Scope = {"type": "http", "method": "POST", "headers": []}
    await middleware(scope, receive, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


@pytest.mark.anyio
async def test_outer_exception_boundary_sends_only_one_response_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def receive() -> Message:
        return {"type": "http.disconnect"}

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"x-request-id", b"boundary-test")],
    }
    fresh_messages: list[Message] = []

    async def fail_before_start(_scope, _receive, _send) -> None:
        raise RuntimeError("privacy-canary-before-start")

    async def collect_fresh(message: Message) -> None:
        fresh_messages.append(message)

    with caplog.at_level(logging.ERROR, logger="cli_consumption.api"):
        await SafeExceptionBoundary(fail_before_start)(scope, receive, collect_fresh)

    starts = [
        message
        for message in fresh_messages
        if message["type"] == "http.response.start"
    ]
    assert len(starts) == 1
    assert starts[0]["status"] == 500
    assert (b"x-request-id", b"boundary-test") in starts[0]["headers"]
    assert b"internal_server_error" in fresh_messages[-1]["body"]
    assert "privacy-canary-before-start" not in "\n".join(caplog.messages)

    started_messages: list[Message] = []

    async def fail_after_start(_scope, _receive, send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        raise RuntimeError("privacy-canary-after-start")

    async def collect_started(message: Message) -> None:
        started_messages.append(message)

    await SafeExceptionBoundary(fail_after_start)(scope, receive, collect_started)

    starts = [
        message
        for message in started_messages
        if message["type"] == "http.response.start"
    ]
    assert len(starts) == 1
    assert starts[0]["status"] == 204


@pytest.mark.anyio
async def test_internal_errors_do_not_leak_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_database_engine(tmp_path / "central.sqlite")

    def fail(*_args: object) -> None:
        raise RuntimeError("privacy canary SQL detail")

    monkeypatch.setattr("cli_consumption.api.ingest_snapshot", fail)
    with caplog.at_level(logging.ERROR, logger="cli_consumption.api"):
        transport = httpx.ASGITransport(
            app=create_app(engine), raise_app_exceptions=False
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://collector.test"
        ) as client:
            response = await client.post(
                "/api/v1/snapshots?secret=privacy-canary-query",
                json={"provider": "privacy-canary-body"},
                headers={
                    "X-Request-ID": "safe-request-id",
                    "Authorization": "Bearer privacy-canary-token",
                },
            )

    assert response.status_code == 500
    assert response.json() == {"detail": "internal_server_error"}
    assert response.headers["x-request-id"] == "safe-request-id"
    log = json.loads(caplog.messages[-1])
    assert log == {
        "code": "internal_server_error",
        "event": "request_failed",
        "exception_type": "RuntimeError",
        "method": "POST",
        "request_id": "safe-request-id",
        "route": "/api/v1/snapshots",
    }
    combined = response.text + "\n".join(caplog.messages)
    assert "privacy-canary-query" not in combined
    assert "privacy-canary-body" not in combined
    assert "privacy-canary-token" not in combined
    assert "privacy canary SQL detail" not in combined
    engine.dispose()


@pytest.mark.anyio
async def test_readiness_is_generic_when_database_or_schema_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_database_engine(tmp_path / "central.sqlite")
    transport = httpx.ASGITransport(app=create_app(engine), raise_app_exceptions=False)
    with caplog.at_level(logging.WARNING, logger="cli_consumption.api"):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://collector.test"
        ) as client:
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE conversations"))
            schema_response = await client.get(
                "/ready?detail=privacy-canary-schema",
                headers={"X-Request-ID": "schema-check"},
            )

            def unavailable() -> None:
                raise ConnectionError("privacy-canary-database-url")

            monkeypatch.setattr(engine, "connect", unavailable)
            down_response = await client.get(
                "/ready", headers={"X-Request-ID": "database-check"}
            )
            live_response = await client.get("/health")

    assert schema_response.status_code == 503
    assert schema_response.json() == {"status": "not_ready"}
    assert schema_response.headers["x-request-id"] == "schema-check"
    assert down_response.status_code == 503
    assert down_response.json() == {"status": "not_ready"}
    assert down_response.headers["x-request-id"] == "database-check"
    assert live_response.status_code == 200
    assert live_response.json()["status"] == "ok"
    logs = [json.loads(message) for message in caplog.messages]
    assert [log["exception_type"] for log in logs] == [
        "Exception",
        "ConnectionError",
    ]
    assert {log["route"] for log in logs} == {"/ready"}
    assert "privacy-canary" not in "\n".join(caplog.messages)
    engine.dispose()


@pytest.mark.anyio
async def test_request_ids_are_bounded_and_readiness_is_concurrent(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "central.sqlite")
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        responses = await asyncio.gather(
            *(client.get("/ready") for _ in range(20)),
            client.get("/health", headers={"X-Request-ID": "valid.id-_1"}),
            client.get("/health", headers={"X-Request-ID": "x" * 65}),
            client.get("/health", headers={"X-Request-ID": "secret/value"}),
        )

    assert all(response.status_code == 200 for response in responses)
    generated = [response.headers["x-request-id"] for response in responses[:20]]
    assert len(set(generated)) == 20
    assert responses[20].headers["x-request-id"] == "valid.id-_1"
    assert responses[21].headers["x-request-id"] != "x" * 65
    assert responses[22].headers["x-request-id"] != "secret/value"
    assert all(1 <= len(value) <= 64 for value in generated)
    engine.dispose()


@pytest.mark.anyio
async def test_blocked_concurrent_readiness_probes_finish_within_deadline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "blocked.sqlite"
    engine = create_database_engine(database)
    transport = httpx.ASGITransport(app=create_app(engine))
    blocker = sqlite3.connect(database, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://collector.test"
        ) as client:
            live_started = time.monotonic()
            live_response = await client.get("/health")
            live_elapsed = time.monotonic() - live_started
            started = time.monotonic()
            responses = await asyncio.gather(*(client.get("/ready") for _ in range(20)))
            elapsed = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        engine.dispose()

    assert live_response.status_code == 200
    assert live_elapsed < 0.5
    assert {response.status_code for response in responses} == {503}
    assert elapsed < 3.5


@pytest.mark.anyio
async def test_postgresql_readiness_sets_local_timeouts_and_uses_one_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def scalar_one(self) -> int:
            return 1

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self) -> None:
            self.driver_statements: list[str] = []
            self.probes = 0

        def exec_driver_sql(self, statement: str) -> None:
            self.driver_statements.append(statement)

        def execute(self, _statement, _parameters) -> Result:
            self.probes += 1
            return Result()

    connection = Connection()

    class FakeEngine:
        def connect(self):
            return nullcontext(connection)

    monkeypatch.setattr("cli_consumption.api.initialize_database", lambda _engine: None)
    engine = cast(Engine, FakeEngine())
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert connection.driver_statements == [
        "SET LOCAL statement_timeout = 2000",
        "SET LOCAL lock_timeout = 1000",
    ]
    assert connection.probes == 1
