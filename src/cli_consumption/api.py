from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated, Any

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
from cli_consumption.storage import (
    create_postgresql_readiness_engine,
    ingest_snapshot,
    initialize_database,
)

MAX_REQUEST_BYTES = 32 * 1024 * 1024
REQUEST_ID_HEADER = b"x-request-id"
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
IDEMPOTENCY_KEY_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
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
    "sync_receipts",
)
READINESS_RESPONSE_TIMEOUT_SECONDS = 2.0
READINESS_CONNECT_TIMEOUT_SECONDS = 2
READINESS_STATEMENT_TIMEOUT_MS = 1_500
READINESS_LOCK_TIMEOUT_MS = 1_000
READINESS_SQL = text(
    "SELECT CASE WHEN COUNT(*) = 1 AND MIN(version_num) = :revision "
    "THEN 1 ELSE 0 END"
    + "".join(
        f" + 0 * (SELECT COUNT(*) FROM {table_name} WHERE 1 = 0)"
        for table_name in READINESS_TABLES
    )
    + " FROM alembic_version"
)
SAFE_ROUTES = frozenset(
    {
        "/health",
        "/ready",
        "/api/v1/capabilities",
        "/api/v1/snapshots",
    }
)
SAFE_EXCEPTION_TYPES = frozenset(
    {
        "Exception",
        "ConnectionError",
        "ReadinessBusy",
        "RuntimeError",
        "TimeoutError",
    }
)
logger = logging.getLogger("cli_consumption.api")


class RequestTooLarge(Exception):
    pass


def _request_id(scope: Scope) -> str:
    existing = scope.get("state", {}).get("request_id")
    if isinstance(existing, str) and REQUEST_ID_PATTERN.fullmatch(existing) is not None:
        return existing
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


def _safe_route(scope: Scope) -> str:
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path if route_path in SAFE_ROUTES else "unmatched"


def _safe_method(scope: Scope) -> str:
    method = scope.get("method")
    return method if method in {"GET", "POST"} else "OTHER"


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
    scope: Scope,
    code: str,
    error: Exception | None = None,
    exception_type: str | None = None,
) -> None:
    payload = {
        "code": code,
        "event": event,
        "method": _safe_method(scope),
        "request_id": _request_id(scope),
        "route": _safe_route(scope),
    }
    if error is not None:
        payload["exception_type"] = _safe_exception_type(error)
    elif exception_type is not None:
        payload["exception_type"] = (
            exception_type if exception_type in SAFE_EXCEPTION_TYPES else "Exception"
        )
    logger.log(level, json.dumps(payload, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class ReadinessOutcome:
    ready: bool
    exception_type: str | None = None


class ReadinessProbeRunner:
    """Bound readiness latency while allowing at most one abandoned daemon probe."""

    def __init__(self, engine: Engine, *, owns_engine: bool) -> None:
        self.engine = engine
        self.owns_engine = owns_engine
        self._lock = threading.Lock()
        self._active_count = 0
        self._closed = False
        self._max_active = 0

    @property
    def max_active_probes(self) -> int:
        with self._lock:
            return self._max_active

    async def run(self) -> ReadinessOutcome:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ReadinessOutcome] = loop.create_future()
        with self._lock:
            if self._closed or self._active_count:
                return ReadinessOutcome(False, "ReadinessBusy")
            self._active_count += 1
            self._max_active = max(self._max_active, self._active_count)

        def probe() -> None:
            try:
                outcome = _probe_database_readiness(self.engine)
            finally:
                with self._lock:
                    self._active_count -= 1
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._deliver, future, outcome)

        threading.Thread(
            target=probe,
            name="cli-consumption-readiness",
            daemon=True,
        ).start()
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=READINESS_RESPONSE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return ReadinessOutcome(False, "TimeoutError")

    @staticmethod
    def _deliver(
        future: asyncio.Future[ReadinessOutcome], outcome: ReadinessOutcome
    ) -> None:
        if not future.done():
            future.set_result(outcome)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        if self.owns_engine:
            try:
                self.engine.dispose()
            except Exception as error:
                logger.error(
                    json.dumps(
                        {
                            "event": "readiness_engine_dispose_failed",
                            "exception_type": _safe_exception_type(error),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )


def _probe_database_readiness(engine: Engine) -> ReadinessOutcome:
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql(
                    f"PRAGMA busy_timeout = {READINESS_STATEMENT_TIMEOUT_MS}"
                )
            ready_value = connection.execute(
                READINESS_SQL, {"revision": CURRENT_DATABASE_REVISION}
            ).scalar_one()
            if ready_value != 1:
                raise RuntimeError("unexpected database revision")
    except Exception as error:
        return ReadinessOutcome(False, _safe_exception_type(error))
    return ReadinessOutcome(True)


class SafeExceptionBoundary:
    """Keep re-raised application exceptions away from the ASGI server logger."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self.app, name)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as error:
            _log_event(
                logging.ERROR,
                event="request_failed",
                scope=scope,
                code="internal_server_error",
                error=error,
            )
            if not response_started:
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "internal_server_error"},
                    headers={"X-Request-ID": request_id},
                )
                await response(scope, receive, send)


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
            status_code=413,
            content={"detail": "request_too_large"},
        )
        await response(scope, receive, send)


def create_app(engine: Engine, api_token: str | None = None) -> SafeExceptionBoundary:
    initialize_database(engine)
    if engine.dialect.name == "postgresql":
        probe_engine = create_postgresql_readiness_engine(
            engine.url,
            connect_timeout_seconds=READINESS_CONNECT_TIMEOUT_SECONDS,
            statement_timeout_ms=READINESS_STATEMENT_TIMEOUT_MS,
            lock_timeout_ms=READINESS_LOCK_TIMEOUT_MS,
        )
        readiness = ReadinessProbeRunner(probe_engine, owns_engine=True)
    else:
        readiness = ReadinessProbeRunner(engine, owns_engine=False)
    app = FastAPI(title="CLI Consumption collector", version=__version__)
    app.state.readiness = readiness
    app.router.add_event_handler("shutdown", readiness.close)
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
    async def ready(request: Request) -> JSONResponse:
        outcome = await readiness.run()
        if not outcome.ready:
            _log_event(
                logging.WARNING,
                event="readiness_failed",
                scope=request.scope,
                code="database_unavailable",
                exception_type=outcome.exception_type,
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready"},
            )
        return JSONResponse(content={"status": "ready"})

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict[str, int | bool]:
        return {
            "snapshot_schema_min": MIN_SUPPORTED_SNAPSHOT_SCHEMA,
            "snapshot_schema_max": CURRENT_SNAPSHOT_SCHEMA,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "max_snapshot_records": MAX_SNAPSHOT_RECORDS,
            "idempotent_snapshot_uploads": True,
        }

    @app.post("/api/v1/snapshots", dependencies=[Depends(authorize)])
    def receive_snapshot(
        payload: SnapshotPayload,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=36,
                max_length=36,
                pattern=IDEMPOTENCY_KEY_PATTERN,
            ),
        ] = None,
    ) -> dict[str, int | str]:
        snapshot = Snapshot.from_dict(payload.model_dump())
        result = (
            ingest_snapshot(engine, snapshot, idempotency_key=idempotency_key)
            if idempotency_key is not None
            else ingest_snapshot(engine, snapshot)
        )
        return {
            "run_id": result.run_id,
            "received": result.received,
            "written": result.written,
            "skipped": result.skipped,
        }

    return SafeExceptionBoundary(app)
