from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
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
from cli_consumption.schema import CURRENT_DATABASE_REVISION
from cli_consumption.storage import ingest_snapshot, initialize_database

MAX_REQUEST_BYTES = 32 * 1024 * 1024
REQUEST_ID_HEADER = b"x-request-id"
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
READINESS_TABLES = (
    "conversations",
    "turns",
    "model_calls",
    "tool_calls",
    "work_items",
    "context_samples",
    "turn_settings",
    "compaction_events",
    "subagents",
    "subagent_scopes",
    "ingestion_runs",
)
SAFE_ROUTES = frozenset(
    {
        "/health",
        "/ready",
        "/api/v1/capabilities",
        "/api/v1/snapshots",
    }
)
logger = logging.getLogger("cli_consumption.api")


class RequestTooLarge(Exception):
    pass


def _request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() != REQUEST_ID_HEADER:
            continue
        try:
            candidate = value.decode("ascii")
        except UnicodeDecodeError:
            break
        if REQUEST_ID_PATTERN.fullmatch(candidate) is not None:
            return candidate
        break
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """Attach one bounded, non-sensitive correlation identifier to every response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [
                    (name, value)
                    for name, value in headers
                    if name.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _safe_route(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path if route_path in SAFE_ROUTES else "unmatched"


def _safe_method(request: Request) -> str:
    return request.method if request.method in {"GET", "POST"} else "OTHER"


def _safe_exception_type(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "TimeoutError"
    if isinstance(error, ConnectionError):
        return "ConnectionError"
    if isinstance(error, RuntimeError):
        return "RuntimeError"
    return "Exception"


def _log_event(
    level: int,
    *,
    event: str,
    request: Request,
    code: str,
    error: Exception | None = None,
) -> None:
    payload = {
        "code": code,
        "event": event,
        "method": _safe_method(request),
        "request_id": request.state.request_id,
        "route": _safe_route(request),
    }
    if error is not None:
        payload["exception_type"] = _safe_exception_type(error)
    logger.log(level, json.dumps(payload, sort_keys=True, separators=(",", ":")))


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
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, error: RequestValidationError
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
    async def invalid_snapshot(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "invalid_snapshot"})

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        _log_event(
            logging.ERROR,
            event="request_failed",
            request=request,
            code="internal_server_error",
            error=error,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_server_error"},
            headers={"X-Request-ID": request.state.request_id},
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

    @app.get("/ready")
    def ready(request: Request) -> JSONResponse:
        try:
            with engine.connect() as connection:
                revisions = (
                    connection.execute(
                        text("SELECT version_num FROM alembic_version LIMIT 2")
                    )
                    .scalars()
                    .all()
                )
                if revisions != [CURRENT_DATABASE_REVISION]:
                    raise RuntimeError("unexpected database revision")
                for table_name in READINESS_TABLES:
                    connection.exec_driver_sql(
                        f"SELECT 1 FROM {table_name} WHERE 1 = 0"
                    )
        except Exception as error:
            _log_event(
                logging.WARNING,
                event="readiness_failed",
                request=request,
                code="database_unavailable",
                error=error,
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready"},
            )
        return JSONResponse(content={"status": "ready"})

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
