from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cli_consumption import __version__
from cli_consumption.models import (
    CURRENT_SNAPSHOT_SCHEMA,
    MAX_SNAPSHOT_RECORDS,
    MIN_SUPPORTED_SNAPSHOT_SCHEMA,
    Snapshot,
    SnapshotPayload,
    SnapshotValidationError,
)
from cli_consumption.storage import ingest_snapshot, initialize_database

MAX_REQUEST_BYTES = 32 * 1024 * 1024


class RequestTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Reject oversized bodies even when Transfer-Encoding is chunked."""

    def __init__(self, app: ASGIApp, maximum: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.maximum = maximum

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.maximum:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.maximum:
                    raise RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "request_too_large"},
        )
        await response(scope, receive, send)


def create_app(engine: Engine, api_token: str | None = None) -> FastAPI:
    initialize_database(engine)
    app = FastAPI(title="CLI Consumption collector", version=__version__)
    app.add_middleware(RequestSizeLimitMiddleware)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: object, error: RequestValidationError
    ) -> JSONResponse:
        code = (
            "unsupported_schema_version"
            if any(
                item.get("loc") == ("body", "schema_version") for item in error.errors()
            )
            else "invalid_snapshot"
        )
        return JSONResponse(status_code=422, content={"detail": code})

    @app.exception_handler(SnapshotValidationError)
    async def invalid_snapshot(_request: object, _error: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "invalid_snapshot"})

    @app.exception_handler(Exception)
    async def internal_error(_request: object, _error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"detail": "internal_server_error"}
        )

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if api_token is None:
            return
        expected = f"Bearer {api_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
            )

    @app.get("/health")
    def health() -> dict[str, int | str]:
        return {
            "status": "ok",
            "version": __version__,
            "snapshot_schema_min": MIN_SUPPORTED_SNAPSHOT_SCHEMA,
            "snapshot_schema_max": CURRENT_SNAPSHOT_SCHEMA,
        }

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict[str, int]:
        return {
            "snapshot_schema_min": MIN_SUPPORTED_SNAPSHOT_SCHEMA,
            "snapshot_schema_max": CURRENT_SNAPSHOT_SCHEMA,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_snapshot_records": MAX_SNAPSHOT_RECORDS,
        }

    @app.post("/api/v1/snapshots", dependencies=[Depends(authorize)])
    def receive_snapshot(payload: SnapshotPayload) -> dict[str, int | str]:
        result = ingest_snapshot(engine, Snapshot.from_dict(payload.model_dump()))
        return {
            "run_id": result.run_id,
            "received": result.received,
            "written": result.written,
            "skipped": result.skipped,
        }

    return app
