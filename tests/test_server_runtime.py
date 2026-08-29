from __future__ import annotations

import asyncio
import io
import logging
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from cli_consumption.api import MAX_REQUEST_BYTES, create_app
from cli_consumption.storage import create_database_engine


@pytest.mark.anyio
async def test_real_uvicorn_never_receives_application_exception_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "UVICORN_EXCEPTION_PRIVACY_CANARY"
    engine = create_database_engine(tmp_path / "central.sqlite")

    def fail(*_args: object) -> None:
        raise RuntimeError(canary)

    monkeypatch.setattr("cli_consumption.api.ingest_snapshot", fail)
    config = uvicorn.Config(
        create_app(engine),
        host="127.0.0.1",
        port=0,
        access_log=False,
        lifespan="off",
        log_config=None,
    )
    server = uvicorn.Server(config)
    uvicorn_output = io.StringIO()
    uvicorn_handler = logging.StreamHandler(uvicorn_output)
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addHandler(uvicorn_handler)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        with caplog.at_level(logging.ERROR, logger="cli_consumption.api"):
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"http://127.0.0.1:{port}/api/v1/snapshots?secret={canary}",
                    json={"provider": "codex"},
                    headers={"X-Request-ID": "uvicorn-boundary"},
                )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()
        uvicorn_logger.removeHandler(uvicorn_handler)
        engine.dispose()

    captured = capsys.readouterr()
    combined = "\n".join(
        (
            response.text,
            uvicorn_output.getvalue(),
            captured.out,
            captured.err,
            *caplog.messages,
        )
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "internal_server_error"}
    assert response.headers["x-request-id"] == "uvicorn-boundary"
    assert canary not in combined
    assert "Exception in ASGI application" not in combined


@pytest.mark.anyio
async def test_oversized_request_is_compatible_and_has_request_id(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "central.sqlite")
    transport = httpx.ASGITransport(app=create_app(engine))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://collector.test"
        ) as client:
            response = await client.post(
                "/api/v1/snapshots",
                content=b"{}",
                headers={
                    "Content-Length": str(MAX_REQUEST_BYTES + 1),
                    "X-Request-ID": "oversized-request",
                },
            )
    finally:
        engine.dispose()

    assert response.status_code == 413
    assert response.json() == {"detail": "request_too_large"}
    assert response.headers["x-request-id"] == "oversized-request"
