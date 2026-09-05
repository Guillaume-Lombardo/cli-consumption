from __future__ import annotations

import errno
import json
import math
import os
import tempfile
import time as time_module
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from cli_consumption.adapters.registry import ADAPTER_SPECS
from cli_consumption.dashboard_layouts import (
    DEFAULT_DASHBOARD_LAYOUT_V1,
    DashboardLayoutV1,
    revalidate_dashboard_layout,
)
from cli_consumption.reporting import (
    ExportWindow,
    ReportEstimate,
    ReportFilters,
    estimate_report,
    iter_report_rows,
    report_statement,
)
from cli_consumption.storage import initialize_database

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "visible_output_tokens",
    "unattributed_tokens",
    "total_tokens",
)
DASHBOARD_CONTRACT_VERSION = 1
MAX_DASHBOARD_RECORDS = 250_000
MAX_DASHBOARD_ESTIMATED_BYTES = 128 * 1024 * 1024
MAX_DASHBOARD_HTML_BYTES = 128 * 1024 * 1024
MAX_DASHBOARD_INDEX_BYTES = 128 * 1024 * 1024
PY_OBJECT_ALIGNMENT = 8
PY_STRING_BASE_BYTES = 72
PY_INTEGER_BYTES = 32
PY_TUPLE_3_BYTES = 88
PY_MAPPING_ENTRY_BYTES = 160
PY_SET_ENTRY_BYTES = 128
PY_SORTED_REFERENCE_BYTES = 16
PY_CONTEXT_BASE_BYTES = 1_024
DASHBOARD_SECTIONS = (
    ("conversations", "conversations"),
    ("turns", "turns"),
    ("model_calls", "modelCalls"),
    ("tool_calls", "toolCalls"),
    ("work_items", "workItems"),
    ("context_samples", "contextSamples"),
    ("turn_settings", "turnSettings"),
    ("compaction_events", "compactions"),
    ("subagents", "subagents"),
    ("ingestion_runs", "ingestionRuns"),
)


@cache
def _react_dashboard_script() -> str:
    """Load the production React runtime embedded in offline exports."""
    return (
        files("cli_consumption")
        .joinpath("dashboard_react.js")
        .read_text(encoding="utf-8")
    )


@cache
def _react_dashboard_styles() -> str:
    """Load the compiled Tailwind stylesheet embedded in offline exports."""
    return (
        files("cli_consumption")
        .joinpath("dashboard_react.css")
        .read_text(encoding="utf-8")
    )


@cache
def _inter_font_license_notice() -> str:
    """Load and HTML-escape the bundled Inter OFL notice without network URLs."""
    license_text = (
        files("cli_consumption")
        .joinpath("INTER_FONT_LICENSE.txt")
        .read_text(encoding="utf-8")
    )
    return escape(
        license_text.replace("https://", "").replace("http://", ""), quote=False
    )


class DashboardLimitError(RuntimeError):
    """A privacy-safe dashboard generation limit failure."""


def generate_dashboard(
    engine: Engine,
    output: Path,
    *,
    share_safe: bool = False,
    window: ExportWindow | None = None,
    filters: ReportFilters | None = None,
    timeout_seconds: float | None = None,
    layout: DashboardLayoutV1 | None = None,
) -> None:
    validated_layout = revalidate_dashboard_layout(
        DEFAULT_DASHBOARD_LAYOUT_V1 if layout is None else layout
    )
    initialize_database(engine)
    with _dashboard_snapshot(engine, timeout_seconds=timeout_seconds) as connection:
        _enforce_estimate(_estimate_selection(connection, window, filters))
        context = _dashboard_context(
            connection,
            share_safe=share_safe,
            window=window or ExportWindow(),
            filters=filters or ReportFilters(),
        )
        _atomic_write(
            output,
            lambda handle: _stream_dashboard(
                handle,
                connection,
                context,
                validated_layout,
            ),
        )


def build_dashboard_dataset(
    engine: Engine,
    *,
    share_safe: bool = False,
    window: ExportWindow | None = None,
    filters: ReportFilters | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Build one bounded dashboard dataset from a coherent database snapshot."""
    initialize_database(engine)
    active_window = window or ExportWindow()
    active_filters = filters or ReportFilters()
    with _dashboard_snapshot(engine, timeout_seconds=timeout_seconds) as connection:
        _enforce_estimate(
            _estimate_selection(connection, active_window, active_filters)
        )
        return _dashboard_payload(
            engine,
            share_safe=share_safe,
            window=active_window,
            filters=active_filters,
            _connection=connection,
        )


def _preflight_dashboard(
    engine: Engine,
    *,
    window: ExportWindow | None = None,
    filters: ReportFilters | None = None,
) -> None:
    initialize_database(engine)
    with _dashboard_snapshot(engine) as connection:
        estimate = _estimate_selection(connection, window, filters)
    _enforce_estimate(estimate)


def _estimate_selection(
    connection: Connection,
    window: ExportWindow | None,
    filters: ReportFilters | None,
) -> ReportEstimate:
    if filters is None or filters == ReportFilters():
        return estimate_report(connection, window)
    return estimate_report(connection, window, filters=filters)


def _enforce_estimate(estimate: ReportEstimate) -> None:
    if estimate.records > MAX_DASHBOARD_RECORDS:
        raise DashboardLimitError("dashboard_record_limit_exceeded")
    if estimate.scalar_bytes > MAX_DASHBOARD_ESTIMATED_BYTES:
        raise DashboardLimitError("dashboard_estimated_size_limit_exceeded")


@contextmanager
def _dashboard_snapshot(
    engine: Engine,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[Connection]:
    with engine.connect() as original_connection:
        connection = original_connection
        if connection.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN DEFERRED")
            driver_connection = connection.connection.driver_connection
            set_progress_handler = getattr(
                driver_connection,
                "set_progress_handler",
                None,
            )
            if timeout_seconds is not None and set_progress_handler is not None:
                deadline = time_module.monotonic() + timeout_seconds
                set_progress_handler(
                    lambda: int(time_module.monotonic() >= deadline),
                    10_000,
                )
            try:
                yield connection
            finally:
                if timeout_seconds is not None and set_progress_handler is not None:
                    set_progress_handler(None, 0)
                connection.rollback()
            return
        with connection.begin():
            if timeout_seconds is not None and connection.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "SET LOCAL statement_timeout = "
                    f"{max(1, int(timeout_seconds * 1_000))}"
                )
            yield connection


def _atomic_write(output: Path, writer: Callable[[Any], None]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
        _fsync_directory(output.parent)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _atomic_write_text(output: Path, content: str) -> None:
    def write(handle: TextIO) -> None:
        handle.write(content)

    _atomic_write(output, write)


def _fsync_directory(directory: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP}:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class _DashboardContext:
    window: ExportWindow
    filters: ReportFilters
    share_safe: bool
    conversation_keys: dict[str, int]
    external_keys: dict[tuple[str, str, str], int]
    turn_keys: dict[str, int]
    projects: dict[str, str]
    machines: dict[str, str]
    models: dict[str, str]
    roles: dict[str, str]
    token_semantics: dict[str, str]


class _IndexBudget:
    def __init__(self) -> None:
        self.used = PY_CONTEXT_BASE_BYTES

    def _add(self, size: int) -> None:
        self.used += size
        if self.used > MAX_DASHBOARD_INDEX_BYTES:
            raise DashboardLimitError("dashboard_index_limit_exceeded")

    def charge_conversation(
        self,
        identifier: str,
        external: tuple[str, str, str],
    ) -> None:
        self._add(
            PY_MAPPING_ENTRY_BYTES
            + _python_string_size(identifier)
            + PY_INTEGER_BYTES
            + PY_MAPPING_ENTRY_BYTES
            + PY_TUPLE_3_BYTES
            + sum(_python_string_size(value) for value in external)
        )

    def charge_turn(self, identifier: str) -> None:
        self._add(
            PY_MAPPING_ENTRY_BYTES + _python_string_size(identifier) + PY_INTEGER_BYTES
        )

    def charge_alias(self, source: str, alias: str) -> None:
        self._add(
            PY_MAPPING_ENTRY_BYTES
            + _python_string_size(source)
            + _python_string_size(alias)
        )

    def charge_model_candidate(self, value: str) -> None:
        self._add(PY_SET_ENTRY_BYTES + _python_string_size(value))

    def charge_model_sort(self, entries: int) -> None:
        self._add(entries * PY_SORTED_REFERENCE_BYTES)

    def charge_model_alias(self, alias: str) -> None:
        self._add(PY_MAPPING_ENTRY_BYTES + _python_string_size(alias))

    def charge_token_semantics(self, entries: int) -> None:
        self._add(entries * PY_MAPPING_ENTRY_BYTES)


def _python_string_size(value: str) -> int:
    raw = PY_STRING_BASE_BYTES + len(value.encode("utf-8"))
    return (raw + PY_OBJECT_ALIGNMENT - 1) // PY_OBJECT_ALIGNMENT * PY_OBJECT_ALIGNMENT


def _dashboard_context(
    connection: Connection,
    *,
    share_safe: bool,
    window: ExportWindow,
    filters: ReportFilters,
) -> _DashboardContext:
    budget = _IndexBudget()
    conversation_keys: dict[str, int] = {}
    external_keys: dict[tuple[str, str, str], int] = {}
    turn_keys: dict[str, int] = {}
    model_names: set[str] = set()

    for index, row in enumerate(
        iter_report_rows(connection, "conversations", window, filters=filters)
    ):
        identifier = str(row["id"])
        external = (
            str(row["provider"]),
            str(row["source_machine"]),
            str(row["external_id"]),
        )
        budget.charge_conversation(identifier, external)
        conversation_keys[identifier] = index
        external_keys[external] = index
        if share_safe:
            for model in json.loads(row["models_json"]):
                _add_bounded_model(model_names, str(model), budget)
    for index, row in enumerate(
        iter_report_rows(connection, "turns", window, filters=filters)
    ):
        identifier = str(row["id"])
        budget.charge_turn(identifier)
        turn_keys[identifier] = index
    if share_safe:
        for model in _distinct_report_values(
            connection, "model_calls", "model", window, filters
        ):
            _add_bounded_model(model_names, model, budget)
        for model in _distinct_report_values(
            connection, "turn_settings", "model", window, filters
        ):
            _add_bounded_model(model_names, model, budget)
    token_semantics: dict[str, str] = {
        spec.name: spec.token_semantics for spec in ADAPTER_SPECS
    }
    budget.charge_token_semantics(len(token_semantics))
    return _DashboardContext(
        window=window,
        filters=filters,
        share_safe=share_safe,
        conversation_keys=conversation_keys,
        external_keys=external_keys,
        turn_keys=turn_keys,
        projects=(
            _distinct_aliases(
                connection,
                "conversations",
                "project",
                "project",
                window,
                filters,
                budget,
            )
            if share_safe
            else {}
        ),
        machines=(
            _distinct_aliases(
                connection,
                "conversations",
                "source_machine",
                "machine",
                window,
                filters,
                budget,
            )
            if share_safe
            else {}
        ),
        models=_model_aliases(model_names, budget) if share_safe else {},
        roles=(
            _distinct_aliases(
                connection,
                "subagents",
                "agent_role",
                "role",
                window,
                filters,
                budget,
                normalize_empty="unspecified",
            )
            if share_safe
            else {}
        ),
        token_semantics=token_semantics,
    )


def _add_bounded_model(values: set[str], value: str, budget: _IndexBudget) -> None:
    if value not in values:
        budget.charge_model_candidate(value)
        values.add(value)


def _distinct_report_values(
    connection: Connection,
    table_name: str,
    column_name: str,
    window: ExportWindow,
    filters: ReportFilters,
) -> Iterator[str]:
    selected = (
        report_statement(connection, table_name, window, filters=filters)
        .order_by(None)
        .subquery()
    )
    column = selected.c[column_name]
    statement = select(column).where(column.is_not(None)).distinct().order_by(column)
    result = connection.execution_options(stream_results=True, yield_per=1_000).execute(
        statement
    )
    for value in result.scalars().yield_per(1_000):
        yield str(value)


def _distinct_aliases(
    connection: Connection,
    table_name: str,
    column_name: str,
    prefix: str,
    window: ExportWindow,
    filters: ReportFilters,
    budget: _IndexBudget,
    *,
    normalize_empty: str | None = None,
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for source in _distinct_report_values(
        connection, table_name, column_name, window, filters
    ):
        normalized = source or normalize_empty
        if normalized is None or normalized in aliases:
            continue
        alias = f"{prefix}-{len(aliases) + 1}"
        budget.charge_alias(normalized, alias)
        aliases[normalized] = alias
    return aliases


def _model_aliases(values: set[str], budget: _IndexBudget) -> dict[str, str]:
    aliases: dict[str, str] = {}
    budget.charge_model_sort(len(values))
    for source in sorted(values):
        alias = f"model-{len(aliases) + 1}"
        budget.charge_model_alias(alias)
        aliases[source] = alias
    return aliases


def _dashboard_payload(
    engine: Engine,
    *,
    share_safe: bool = False,
    window: ExportWindow | None = None,
    filters: ReportFilters | None = None,
    _connection: Connection | None = None,
) -> dict[str, Any]:
    """Materialize a small payload for tests and direct library callers."""
    active_window = window or ExportWindow()
    if _connection is None:
        initialize_database(engine)
        with _dashboard_snapshot(engine) as connection:
            return _dashboard_payload(
                engine,
                share_safe=share_safe,
                window=active_window,
                filters=filters,
                _connection=connection,
            )
    context = _dashboard_context(
        _connection,
        share_safe=share_safe,
        window=active_window,
        filters=filters or ReportFilters(),
    )
    payload: dict[str, Any] = {
        "contractVersion": DASHBOARD_CONTRACT_VERSION,
        "meta": _dashboard_metadata(context),
    }
    for table_name, section_name in DASHBOARD_SECTIONS:
        payload[section_name] = [
            _transform_row(table_name, row, context)
            for row in iter_report_rows(
                _connection,
                table_name,
                active_window,
                filters=context.filters,
            )
        ]
    return payload


class _BudgetedWriter:
    def __init__(self, handle: TextIO) -> None:
        self.handle = handle
        self.written = 0

    def write(self, value: str) -> None:
        size = len(value.encode("utf-8"))
        if self.written + size > MAX_DASHBOARD_HTML_BYTES:
            raise DashboardLimitError("dashboard_html_limit_exceeded")
        self.handle.write(value)
        self.written += size


def _stream_dashboard(
    handle: TextIO,
    connection: Connection,
    context: _DashboardContext,
    layout: DashboardLayoutV1,
) -> None:
    prefix, suffix = _react_document_parts()
    writer = _BudgetedWriter(handle)
    writer.write(prefix)
    writer.write(f'{{"contractVersion":{DASHBOARD_CONTRACT_VERSION},"meta":')
    writer.write(_encode_json(_dashboard_metadata(context)))
    for table_name, section_name in DASHBOARD_SECTIONS:
        writer.write(f',"{section_name}":[')
        first = True
        for row in iter_report_rows(
            connection,
            table_name,
            context.window,
            filters=context.filters,
        ):
            encoded = _encode_json(_transform_row(table_name, row, context))
            if not first:
                writer.write(",")
            writer.write(encoded)
            first = False
        writer.write("]")
    writer.write("}")
    writer.write(";globalThis.__CLI_CONSUMPTION_LAYOUT__=")
    writer.write(_encode_json(layout.model_dump(mode="json")))
    writer.write(suffix)


def _react_document_parts() -> tuple[str, str]:
    marker = "__CLI_CONSUMPTION_STREAMED_PAYLOAD__"
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CLI Consumption</title>
  <script id="inter-font-license" type="text/plain">{_inter_font_license_notice()}</script>
  <style>{_react_dashboard_styles()}</style>
</head>
<body>
  <div id="root"></div>
  <script>globalThis.__CLI_CONSUMPTION_DATASET__={marker};</script>
  <script>{_react_dashboard_script()}</script>
</body>
</html>"""
    prefix, separator, suffix = document.partition(marker)
    if not separator:
        raise RuntimeError("dashboard_template_marker_missing")
    return prefix, suffix


def _encode_json(value: Any) -> str:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    return (
        encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )


def _dashboard_metadata(context: _DashboardContext) -> dict[str, Any]:
    metadata: dict[str, Any] = {"shareSafe": context.share_safe}
    if context.window.bounded:
        metadata["exportWindow"] = context.window.metadata(
            day_precision=context.share_safe
        )
    return metadata


def _transform_row(
    table_name: str,
    row: dict[str, Any],
    context: _DashboardContext,
) -> dict[str, Any]:
    def timestamp(value: Any) -> Any:
        return _round_timestamp(value) if context.share_safe else value

    def epoch(value: Any) -> Any:
        return _round_epoch_day(value) if context.share_safe else value

    def tool(value: Any) -> Any:
        if not context.share_safe or value is None:
            return value
        return _tool_category(str(value))

    def alias(mapping: dict[str, str], value: Any, prefix: str) -> str:
        text = str(value)
        if not context.share_safe:
            return text
        return mapping.get(text, f"{prefix}-unmapped")

    def tokens() -> dict[str, int]:
        return {field: int(row[field]) for field in TOKEN_FIELDS}

    if table_name == "conversations":
        return {
            "key": context.conversation_keys[str(row["id"])],
            "provider": row["provider"],
            "tokenSemantics": context.token_semantics.get(
                str(row["provider"]), "unavailable"
            ),
            "machine": alias(context.machines, row["source_machine"], "machine"),
            "project": alias(context.projects, row["project"], "project"),
            "startedAt": timestamp(row["started_at"]),
            "endedAt": timestamp(row["ended_at"]),
            "durationSeconds": row["duration_seconds"],
            "models": [
                alias(context.models, model, "model")
                for model in json.loads(row["models_json"])
            ],
            "turns": row["iterations"],
            "modelCalls": row["model_calls"],
            "toolCalls": row["tool_calls"],
            "compactions": row["compactions"],
            **tokens(),
        }
    conversation_key = context.conversation_keys.get(str(row.get("conversation_id")))
    turn_key = context.turn_keys.get(str(row.get("turn_id")))
    if table_name == "turns":
        return {
            "key": context.turn_keys[str(row["id"])],
            "conversationKey": conversation_key,
            "startedAt": timestamp(row["started_at"]),
            "endedAt": timestamp(row["ended_at"]),
            "status": row["status"],
            "durationMs": row["duration_ms"],
            "ttftMs": row["time_to_first_token_ms"],
            "modelCalls": row["model_calls"],
            "toolCalls": row["tool_calls"],
            **tokens(),
        }
    if table_name == "model_calls":
        return {
            "conversationKey": conversation_key,
            "turnKey": turn_key,
            "timestamp": timestamp(row["timestamp"]),
            "model": alias(context.models, row["model"], "model"),
            **tokens(),
        }
    if table_name == "tool_calls":
        return {
            "conversationKey": conversation_key,
            "turnKey": turn_key,
            "sequence": row["sequence"],
            "timestamp": timestamp(row["timestamp"]),
            "tool": tool(row["tool_name"]),
        }
    if table_name == "work_items":
        return {
            "conversationKey": conversation_key,
            "turnKey": turn_key,
            "kind": row["kind"],
            "tool": tool(row["tool_name"]),
            "startedAtMs": epoch(row["started_at_ms"]),
            "durationMs": row["duration_ms"],
            "status": row["status"],
        }
    if table_name == "context_samples":
        return {
            "conversationKey": conversation_key,
            "turnKey": turn_key,
            "timestamp": timestamp(row["timestamp"]),
            "inputTokens": row["input_tokens"],
            "contextWindowTokens": row["context_window_tokens"],
        }
    if table_name == "turn_settings":
        return {
            "conversationKey": conversation_key,
            "turnKey": turn_key,
            "model": (
                alias(context.models, row["model"], "model") if row["model"] else None
            ),
            "effort": row["effort"],
            "mode": row["collaboration_mode"],
            "tier": row["service_tier"],
            "contextWindowTokens": row["context_window_tokens"],
        }
    if table_name == "compaction_events":
        return {
            "conversationKey": conversation_key,
            "turnKey": turn_key,
            "timestamp": timestamp(row["timestamp"]),
        }
    if table_name == "subagents":
        common = (str(row["provider"]), str(row["source_machine"]))
        return {
            "conversationKey": context.external_keys.get(
                (*common, str(row["parent_thread_id"]))
            ),
            "childConversationKey": context.external_keys.get(
                (*common, str(row["child_thread_id"]))
            ),
            "provider": row["provider"],
            "machine": alias(context.machines, row["source_machine"], "machine"),
            "status": row["status"],
            "createdAtMs": epoch(row["created_at_ms"]),
            "updatedAtMs": epoch(row["updated_at_ms"]),
            "role": alias(
                context.roles,
                row["agent_role"] or "unspecified",
                "role",
            ),
            "tokens": row["tokens_used"],
        }
    if table_name == "ingestion_runs":
        return {
            "provider": row["provider"],
            "ingestedAt": timestamp(row["ingested_at"]),
            "received": row["conversations_received"],
            "written": row["conversations_written"],
            "skipped": row["conversations_skipped"],
            "malformed": row["malformed_records"],
            "duplicates": row["duplicate_conversations"],
        }
    raise ValueError("unknown_dashboard_table")


def _round_timestamp(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{instant.date().isoformat()}T00:00:00+00:00"


def _round_epoch_day(value: Any) -> Any:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        return None
    try:
        instant = datetime.fromtimestamp(value / 1000, UTC)
    except (OSError, OverflowError, ValueError):
        return None
    rounded = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(rounded.timestamp() * 1000)


def _tool_category(name: str) -> str:
    if name in {
        "spawn_agent",
        "wait_agent",
        "list_agents",
        "send_message",
        "followup_task",
        "interrupt_agent",
    }:
        return "Agent coordination"
    if name in {"exec_command", "write_stdin", "wait"}:
        return "Shell and processes"
    if name in {"apply_patch", "view_image"}:
        return "Files and workspace"
    if name == "web__run":
        return "Web"
    if name in {"update_plan", "create_goal", "get_goal", "update_goal"}:
        return "Planning"
    if "imagegen" in name:
        return "Media"
    if name.startswith("mcp__") or "mcp_resource" in name:
        return "Integrations"
    return "Other"
