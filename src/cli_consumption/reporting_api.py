from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import Engine
from starlette.background import BackgroundTask

from cli_consumption.dashboard import (
    DASHBOARD_CONTRACT_VERSION,
    MAX_DASHBOARD_ESTIMATED_BYTES,
    MAX_DASHBOARD_HTML_BYTES,
    MAX_DASHBOARD_RECORDS,
    DashboardLimitError,
    _dashboard_context,
    _dashboard_payload,
    _dashboard_snapshot,
    _enforce_estimate,
    _transform_row,
    build_dashboard_dataset,
    generate_dashboard,
)
from cli_consumption.reporting import (
    ExportWindow,
    ReportFilters,
    estimate_report,
    iter_report_rows,
    parse_export_window,
)

REPORTING_REQUEST_BYTES = 64 * 1024
MAX_FILTER_VALUES = 100
MAX_REPORTING_RECORDS = MAX_DASHBOARD_RECORDS
MAX_REPORTING_SCALAR_BYTES = MAX_DASHBOARD_ESTIMATED_BYTES
MAX_DASHBOARD_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_RESPONSE_BYTES = MAX_DASHBOARD_HTML_BYTES
MAX_CONVERSATION_PAGE_SIZE = 200
REPORTING_TIMEOUT_SECONDS = 15.0
EXPORT_TIMEOUT_SECONDS = 60.0
MAX_CONCURRENT_REPORTS = 4
MAX_CONCURRENT_EXPORTS = 1
PAGINATION_IDLE_SECONDS = 5 * 60
PAGINATION_MAX_SECONDS = 30 * 60
MAX_PAGINATION_SESSIONS = 32
MAX_PAGINATION_SESSION_BYTES = 32 * 1024 * 1024
CACHE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)


class WindowRequest(StrictRequest):
    since: StrictStr | None
    until: StrictStr | None

    @field_validator("since", "until")
    @classmethod
    def validate_bound(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value.encode("utf-8")) > 64 or "T" not in value:
            raise ValueError("invalid reporting timestamp")
        if any(ord(character) < 32 for character in value):
            raise ValueError("invalid reporting timestamp")
        parse_export_window(value)
        return value

    @model_validator(mode="after")
    def validate_order(self) -> WindowRequest:
        parse_export_window(self.since, self.until)
        return self

    def window(self) -> ExportWindow:
        return parse_export_window(self.since, self.until)


_FILTER_BYTE_LIMITS = {
    "providers": 64,
    "machines": 255,
    "projects": 512,
    "models": 255,
}


class FiltersRequest(StrictRequest):
    providers: list[StrictStr] = Field(
        default_factory=list, max_length=MAX_FILTER_VALUES
    )
    machines: list[StrictStr] = Field(
        default_factory=list, max_length=MAX_FILTER_VALUES
    )
    projects: list[StrictStr] = Field(
        default_factory=list, max_length=MAX_FILTER_VALUES
    )
    models: list[StrictStr] = Field(default_factory=list, max_length=MAX_FILTER_VALUES)

    @field_validator("providers", "machines", "projects", "models")
    @classmethod
    def validate_values(cls, values: list[str], info: Any) -> list[str]:
        limit = _FILTER_BYTE_LIMITS[info.field_name]
        if len(values) != len(set(values)):
            raise ValueError("duplicate reporting filter")
        for value in values:
            if (
                not value
                or len(value.encode("utf-8")) > limit
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("invalid reporting filter")
        return sorted(values)

    def filters(self) -> ReportFilters:
        return ReportFilters(
            providers=tuple(self.providers),
            machines=tuple(self.machines),
            projects=tuple(self.projects),
            models=tuple(self.models),
        )


class DashboardQuery(StrictRequest):
    version: Literal[1]
    window: WindowRequest
    filters: FiltersRequest
    profile: Literal["detailed", "share-safe"]


class FilterQuery(StrictRequest):
    version: Literal[1]
    window: WindowRequest
    filters: FiltersRequest


class ConversationListRequest(StrictRequest):
    query: DashboardQuery
    sort: Literal[
        "startedAt",
        "endedAt",
        "provider",
        "machine",
        "project",
        "durationSeconds",
        "totalTokens",
    ] = "startedAt"
    direction: Literal["asc", "desc"] = "desc"
    page_size: Annotated[
        StrictInt,
        Field(alias="pageSize", ge=1, le=MAX_CONVERSATION_PAGE_SIZE),
    ] = 50
    cursor: Annotated[StrictStr | None, Field(min_length=32, max_length=64)] = None


class ConversationDetailRequest(StrictRequest):
    query: DashboardQuery
    conversation_ref: Annotated[
        StrictStr,
        Field(alias="conversationRef", max_length=128),
    ]


class ReportingError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(slots=True)
class _PageRecord:
    reference: str
    identifier: str
    summary: dict[str, Any]


@dataclass(slots=True)
class _PageSession:
    created_at: float
    accessed_at: float
    fingerprint: str
    query_fingerprint: str
    records: list[_PageRecord]


@dataclass(frozen=True, slots=True)
class _Cursor:
    session: str
    offset: int


@dataclass(slots=True)
class _Reference:
    identifier: str
    query_fingerprint: str
    created_at: float
    accessed_at: float


class PaginationStore:
    """Keep opaque, bounded pagination membership outside the database."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, _PageSession] = {}
        self._cursors: dict[str, _Cursor] = {}
        self._references: dict[str, _Reference] = {}

    def create(
        self,
        *,
        fingerprint: str,
        query_fingerprint: str,
        rows: list[tuple[str, dict[str, Any]]],
    ) -> tuple[str, _PageSession]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            if len(self._sessions) >= MAX_PAGINATION_SESSIONS:
                raise ReportingError("reporting_busy", 503)
            session_handle = secrets.token_hex(16)
            session_bytes = sum(
                len(identifier.encode("utf-8"))
                + len(
                    json.dumps(
                        summary,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                )
                for identifier, summary in rows
            )
            if session_bytes > MAX_PAGINATION_SESSION_BYTES:
                raise ReportingError("reporting_limit_exceeded", 413)
            records = []
            references: dict[str, _Reference] = {}
            for identifier, summary in rows:
                reference = secrets.token_hex(16)
                records.append(_PageRecord(reference, identifier, summary))
                references[reference] = _Reference(
                    identifier,
                    query_fingerprint,
                    now,
                    now,
                )
            session = _PageSession(
                created_at=now,
                accessed_at=now,
                fingerprint=fingerprint,
                query_fingerprint=query_fingerprint,
                records=records,
            )
            self._sessions[session_handle] = session
            self._references.update(references)
            return session_handle, session

    def page(
        self,
        cursor: str,
        *,
        fingerprint: str,
    ) -> tuple[str, _PageSession, int]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            position = self._cursors.get(cursor)
            if position is None:
                raise ReportingError("pagination_expired", 410)
            session = self._sessions.get(position.session)
            if session is None:
                raise ReportingError("pagination_expired", 410)
            if session.fingerprint != fingerprint:
                raise ReportingError("invalid_cursor", 400)
            session.accessed_at = now
            return position.session, session, position.offset

    def next_cursor(self, session: str, offset: int) -> str:
        handle = secrets.token_hex(16)
        with self._lock:
            self._cursors[handle] = _Cursor(session, offset)
        return handle

    def resolve_reference(self, reference: str, query_fingerprint: str) -> str:
        now = self._clock()
        with self._lock:
            self._expire(now)
            record = self._references.get(reference)
            if record is None or record.query_fingerprint != query_fingerprint:
                raise ReportingError("conversation_not_found", 404)
            record.accessed_at = now
            return record.identifier

    def _expire(self, now: float) -> None:
        expired_sessions = {
            handle
            for handle, session in self._sessions.items()
            if now - session.accessed_at > PAGINATION_IDLE_SECONDS
            or now - session.created_at > PAGINATION_MAX_SECONDS
        }
        for handle in expired_sessions:
            del self._sessions[handle]
        self._cursors = {
            handle: cursor
            for handle, cursor in self._cursors.items()
            if cursor.session in self._sessions
        }
        self._references = {
            handle: reference
            for handle, reference in self._references.items()
            if now - reference.accessed_at <= PAGINATION_IDLE_SECONDS
            and now - reference.created_at <= PAGINATION_MAX_SECONDS
        }


class ReportingRuntime:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.pagination = PaginationStore()
        self._reports = threading.BoundedSemaphore(MAX_CONCURRENT_REPORTS)
        self._exports = threading.BoundedSemaphore(MAX_CONCURRENT_EXPORTS)

    @contextmanager
    def report_slot(self) -> Iterator[None]:
        with self._slot(self._reports, REPORTING_TIMEOUT_SECONDS):
            yield

    @contextmanager
    def export_slot(self) -> Iterator[None]:
        with self._slot(self._exports, EXPORT_TIMEOUT_SECONDS):
            yield

    @staticmethod
    @contextmanager
    def _slot(
        semaphore: threading.BoundedSemaphore, timeout_seconds: float
    ) -> Iterator[None]:
        if not semaphore.acquire(blocking=False):
            raise ReportingError("reporting_busy", 503)
        started = time.monotonic()
        try:
            yield
            if time.monotonic() - started > timeout_seconds:
                raise ReportingError("reporting_timeout", 503)
        except ReportingError:
            raise
        except Exception:
            if time.monotonic() - started >= timeout_seconds * 0.95:
                raise ReportingError("reporting_timeout", 503) from None
            raise
        finally:
            semaphore.release()

    def dashboard(self, query: DashboardQuery) -> dict[str, Any]:
        with self.report_slot():
            window = query.window.window()
            payload = build_dashboard_dataset(
                self.engine,
                share_safe=query.profile == "share-safe",
                window=window,
                filters=query.filters.filters(),
                timeout_seconds=REPORTING_TIMEOUT_SECONDS,
            )
            payload["contractVersion"] = DASHBOARD_CONTRACT_VERSION
            payload["window"] = window.metadata(
                day_precision=query.profile == "share-safe"
            )
            payload["profile"] = query.profile
            payload["filters"] = _filter_options_from_payload(payload)
            return payload

    def filters(self, query: FilterQuery) -> dict[str, Any]:
        with (
            self.report_slot(),
            _dashboard_snapshot(
                self.engine,
                timeout_seconds=REPORTING_TIMEOUT_SECONDS,
            ) as connection,
        ):
            window = query.window.window()
            filters = query.filters.filters()
            _enforce_estimate(estimate_report(connection, window, filters=filters))
            providers: set[str] = set()
            machines: set[str] = set()
            projects: set[str] = set()
            models: set[str] = set()
            for row in iter_report_rows(
                connection, "conversations", window, filters=filters
            ):
                providers.add(str(row["provider"]))
                machines.add(str(row["source_machine"]))
                projects.add(str(row["project"]))
                for model in json.loads(row["models_json"]):
                    models.add(str(model))
            return {
                "contractVersion": DASHBOARD_CONTRACT_VERSION,
                "filters": {
                    "providers": sorted(providers),
                    "machines": sorted(machines),
                    "projects": sorted(projects),
                    "models": sorted(models),
                },
            }

    def conversations(self, request: ConversationListRequest) -> dict[str, Any]:
        query_fingerprint = _fingerprint(request.query)
        fingerprint = _conversation_list_fingerprint(request)
        if request.cursor is None:
            rows = self._conversation_rows(request)
            session_handle, session = self.pagination.create(
                fingerprint=fingerprint,
                query_fingerprint=query_fingerprint,
                rows=rows,
            )
            offset = 0
        else:
            session_handle, session, offset = self.pagination.page(
                request.cursor,
                fingerprint=fingerprint,
            )
        stop = min(offset + request.page_size, len(session.records))
        items = []
        for record in session.records[offset:stop]:
            items.append({**record.summary, "conversationRef": record.reference})
        next_cursor = (
            self.pagination.next_cursor(session_handle, stop)
            if stop < len(session.records)
            else None
        )
        return {
            "contractVersion": DASHBOARD_CONTRACT_VERSION,
            "items": items,
            "nextCursor": next_cursor,
        }

    def _conversation_rows(
        self, request: ConversationListRequest
    ) -> list[tuple[str, dict[str, Any]]]:
        with (
            self.report_slot(),
            _dashboard_snapshot(
                self.engine,
                timeout_seconds=REPORTING_TIMEOUT_SECONDS,
            ) as connection,
        ):
            window = request.query.window.window()
            filters = request.query.filters.filters()
            _enforce_estimate(estimate_report(connection, window, filters=filters))
            context = _dashboard_context(
                connection,
                share_safe=request.query.profile == "share-safe",
                window=window,
                filters=filters,
            )
            rows = []
            for row in iter_report_rows(
                connection, "conversations", window, filters=filters
            ):
                summary = _transform_row("conversations", row, context)
                summary.pop("key")
                rows.append((str(row["id"]), summary))
        field = {
            "totalTokens": "total_tokens",
        }.get(request.sort, request.sort)

        def key(item: tuple[str, dict[str, Any]]) -> tuple[bool, Any, str]:
            value = item[1].get(field)
            return value is None, value if value is not None else "", item[0]

        return sorted(rows, key=key, reverse=request.direction == "desc")

    def conversation(self, request: ConversationDetailRequest) -> dict[str, Any]:
        query_fingerprint = _fingerprint(request.query)
        identifier = self.pagination.resolve_reference(
            request.conversation_ref,
            query_fingerprint,
        )
        with (
            self.report_slot(),
            _dashboard_snapshot(
                self.engine,
                timeout_seconds=REPORTING_TIMEOUT_SECONDS,
            ) as connection,
        ):
            window = request.query.window.window()
            filters = request.query.filters.filters()
            _enforce_estimate(estimate_report(connection, window, filters=filters))
            context = _dashboard_context(
                connection,
                share_safe=request.query.profile == "share-safe",
                window=window,
                filters=filters,
            )
            conversation_key = context.conversation_keys.get(identifier)
            if conversation_key is None:
                raise ReportingError("conversation_not_found", 404)
            payload = _dashboard_payload(
                self.engine,
                share_safe=request.query.profile == "share-safe",
                window=window,
                filters=filters,
                _connection=connection,
            )
        return _conversation_detail(payload, conversation_key)

    def export(self, query: DashboardQuery) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="cli-consumption-report-",
            suffix=".html",
        )
        os.close(descriptor)
        path = Path(raw_path)
        try:
            with self.export_slot():
                generate_dashboard(
                    self.engine,
                    path,
                    share_safe=query.profile == "share-safe",
                    window=query.window.window(),
                    filters=query.filters.filters(),
                    timeout_seconds=EXPORT_TIMEOUT_SECONDS,
                )
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def install_reporting_routes(
    app: FastAPI,
    engine: Engine,
    *,
    authorize_read: Callable[..., None],
    authorize_export: Callable[..., None],
) -> ReportingRuntime:
    runtime = ReportingRuntime(engine)
    app.state.reporting = runtime

    @app.exception_handler(ReportingError)
    async def reporting_error(_request: Any, error: ReportingError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.code},
            headers=CACHE_HEADERS,
        )

    @app.exception_handler(DashboardLimitError)
    async def reporting_limit(
        _request: Any, _error: DashboardLimitError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={"detail": "reporting_limit_exceeded"},
            headers=CACHE_HEADERS,
        )

    @app.post(
        "/api/v1/reporting/dashboard",
        dependencies=[Depends(authorize_read)],
    )
    def dashboard(query: DashboardQuery) -> Response:
        return _bounded_json(runtime.dashboard(query))

    @app.post(
        "/api/v1/reporting/filters",
        dependencies=[Depends(authorize_read)],
    )
    def filters(query: FilterQuery) -> Response:
        return _bounded_json(runtime.filters(query))

    @app.post(
        "/api/v1/reporting/conversations",
        dependencies=[Depends(authorize_read)],
    )
    def conversations(query: ConversationListRequest) -> Response:
        return _bounded_json(runtime.conversations(query))

    @app.post(
        "/api/v1/reporting/conversation",
        dependencies=[Depends(authorize_read)],
    )
    def conversation(query: ConversationDetailRequest) -> Response:
        return _bounded_json(runtime.conversation(query))

    @app.post(
        "/api/v1/reporting/export",
        dependencies=[Depends(authorize_export)],
    )
    def export(query: DashboardQuery) -> FileResponse:
        path = runtime.export(query)
        return FileResponse(
            path,
            media_type="text/html; charset=utf-8",
            filename="cli-consumption-dashboard.html",
            headers=CACHE_HEADERS,
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    return runtime


def _bounded_json(payload: dict[str, Any]) -> Response:
    content = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(content) > MAX_DASHBOARD_RESPONSE_BYTES:
        raise ReportingError("reporting_response_too_large", 413)
    return Response(
        content=content,
        media_type="application/json",
        headers=CACHE_HEADERS,
    )


def _fingerprint(value: BaseModel) -> str:
    canonical = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"cli-consumption:reporting:v1\0" + canonical).hexdigest()


def _conversation_list_fingerprint(request: ConversationListRequest) -> str:
    canonical = json.dumps(
        {
            "query": request.query.model_dump(mode="json", by_alias=True),
            "sort": request.sort,
            "direction": request.direction,
            "pageSize": request.page_size,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(
        b"cli-consumption:reporting-list:v1\0" + canonical
    ).hexdigest()


def _filter_options_from_payload(payload: dict[str, Any]) -> dict[str, list[str]]:
    conversations = payload["conversations"]
    return {
        "providers": sorted({str(row["provider"]) for row in conversations}),
        "machines": sorted({str(row["machine"]) for row in conversations}),
        "projects": sorted({str(row["project"]) for row in conversations}),
        "models": sorted(
            {str(model) for row in conversations for model in row.get("models", [])}
        ),
    }


def _conversation_detail(
    payload: dict[str, Any], conversation_key: int
) -> dict[str, Any]:
    conversation = dict(
        next(
            item for item in payload["conversations"] if item["key"] == conversation_key
        )
    )
    conversation["key"] = 0
    turns = [
        dict(item)
        for item in payload["turns"]
        if item["conversationKey"] == conversation_key
    ]
    turn_keys = {item["key"]: index for index, item in enumerate(turns)}
    for turn in turns:
        turn["key"] = turn_keys[turn["key"]]
        turn["conversationKey"] = 0

    detail: dict[str, Any] = {
        "contractVersion": DASHBOARD_CONTRACT_VERSION,
        "conversation": conversation,
        "turns": turns,
    }
    for section in (
        "modelCalls",
        "toolCalls",
        "workItems",
        "contextSamples",
        "turnSettings",
        "compactions",
    ):
        rows = []
        for source in payload[section]:
            if source["conversationKey"] != conversation_key:
                continue
            item = dict(source)
            item["conversationKey"] = 0
            if "turnKey" in item:
                item["turnKey"] = turn_keys.get(item["turnKey"])
            rows.append(item)
        detail[section] = rows
    detail["subagents"] = []
    for source in payload["subagents"]:
        if (
            source["conversationKey"] != conversation_key
            and source["childConversationKey"] != conversation_key
        ):
            continue
        item = dict(source)
        item["conversationKey"] = (
            0 if item["conversationKey"] == conversation_key else None
        )
        item["childConversationKey"] = (
            0 if item["childConversationKey"] == conversation_key else None
        )
        detail["subagents"].append(item)
    return detail
