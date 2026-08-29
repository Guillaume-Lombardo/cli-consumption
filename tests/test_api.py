from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from starlette.types import Message, Scope

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.api import RequestSizeLimitMiddleware, create_app
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
async def test_internal_errors_do_not_leak_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_database_engine(tmp_path / "central.sqlite")

    def fail(*_args: object) -> None:
        raise RuntimeError("privacy canary SQL detail")

    monkeypatch.setattr("cli_consumption.api.ingest_snapshot", fail)
    transport = httpx.ASGITransport(app=create_app(engine), raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        response = await client.post("/api/v1/snapshots", json={"provider": "codex"})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal_server_error"}
    assert "privacy canary" not in response.text
    engine.dispose()
