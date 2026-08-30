from __future__ import annotations

import errno
import json
import math
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from cli_consumption.adapters.registry import ADAPTER_SPECS
from cli_consumption.reporting import (
    ExportWindow,
    ReportEstimate,
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


class DashboardLimitError(RuntimeError):
    """A privacy-safe dashboard generation limit failure."""


def generate_dashboard(
    engine: Engine,
    output: Path,
    *,
    share_safe: bool = False,
    window: ExportWindow | None = None,
) -> None:
    initialize_database(engine)
    with _dashboard_snapshot(engine) as connection:
        _enforce_estimate(estimate_report(connection, window))
        context = _dashboard_context(
            connection,
            share_safe=share_safe,
            window=window or ExportWindow(),
        )
        _atomic_write(
            output,
            lambda handle: _stream_dashboard(handle, connection, context),
        )


def _preflight_dashboard(
    engine: Engine,
    *,
    window: ExportWindow | None = None,
) -> None:
    initialize_database(engine)
    with _dashboard_snapshot(engine) as connection:
        estimate = estimate_report(connection, window)
    _enforce_estimate(estimate)


def _enforce_estimate(estimate: ReportEstimate) -> None:
    if estimate.records > MAX_DASHBOARD_RECORDS:
        raise DashboardLimitError("dashboard_record_limit_exceeded")
    if estimate.scalar_bytes > MAX_DASHBOARD_ESTIMATED_BYTES:
        raise DashboardLimitError("dashboard_estimated_size_limit_exceeded")


@contextmanager
def _dashboard_snapshot(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as original_connection:
        connection = original_connection
        if connection.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN DEFERRED")
            try:
                yield connection
            finally:
                connection.rollback()
            return
        with connection.begin():
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
) -> _DashboardContext:
    budget = _IndexBudget()
    conversation_keys: dict[str, int] = {}
    external_keys: dict[tuple[str, str, str], int] = {}
    turn_keys: dict[str, int] = {}
    model_names: set[str] = set()

    for index, row in enumerate(iter_report_rows(connection, "conversations", window)):
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
    for index, row in enumerate(iter_report_rows(connection, "turns", window)):
        identifier = str(row["id"])
        budget.charge_turn(identifier)
        turn_keys[identifier] = index
    if share_safe:
        for model in _distinct_report_values(
            connection, "model_calls", "model", window
        ):
            _add_bounded_model(model_names, model, budget)
        for model in _distinct_report_values(
            connection, "turn_settings", "model", window
        ):
            _add_bounded_model(model_names, model, budget)
    token_semantics: dict[str, str] = {
        spec.name: spec.token_semantics for spec in ADAPTER_SPECS
    }
    budget.charge_token_semantics(len(token_semantics))
    return _DashboardContext(
        window=window,
        share_safe=share_safe,
        conversation_keys=conversation_keys,
        external_keys=external_keys,
        turn_keys=turn_keys,
        projects=(
            _distinct_aliases(
                connection, "conversations", "project", "project", window, budget
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
) -> Iterator[str]:
    selected = (
        report_statement(connection, table_name, window).order_by(None).subquery()
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
    budget: _IndexBudget,
    *,
    normalize_empty: str | None = None,
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for source in _distinct_report_values(connection, table_name, column_name, window):
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
                _connection=connection,
            )
    context = _dashboard_context(
        _connection,
        share_safe=share_safe,
        window=active_window,
    )
    payload: dict[str, Any] = {"meta": _dashboard_metadata(context)}
    for table_name, section_name in DASHBOARD_SECTIONS:
        payload[section_name] = [
            _transform_row(table_name, row, context)
            for row in iter_report_rows(_connection, table_name, active_window)
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
) -> None:
    prefix, suffix = _document_parts()
    writer = _BudgetedWriter(handle)
    writer.write(prefix)
    writer.write("{")
    writer.write('"meta":')
    writer.write(_encode_json(_dashboard_metadata(context)))
    for table_name, section_name in DASHBOARD_SECTIONS:
        writer.write(f',"{section_name}":[')
        first = True
        for row in iter_report_rows(connection, table_name, context.window):
            encoded = _encode_json(_transform_row(table_name, row, context))
            if not first:
                writer.write(",")
            writer.write(encoded)
            first = False
        writer.write("]")
    writer.write("}")
    writer.write(suffix)


def _document_parts() -> tuple[str, str]:
    marker = "__CLI_CONSUMPTION_STREAMED_PAYLOAD__"
    prefix, separator, suffix = _document(marker).partition(marker)
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


def _document(payload: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CLI Consumption</title>
  <style>
    :root {{ color-scheme:light; --bg:#f7f8fa; --ink:#172033; --muted:#64748b; --accent:#087f6d; --blue:#2563eb; --violet:#7c3aed; --amber:#b45309; --red:#be123c; --line:#d7dde7; --control:#fff; --track:#e3e8ef; --hover:#edf1f6; }}
    @media (prefers-color-scheme:dark) {{ :root:not([data-theme]) {{ color-scheme:dark; --bg:#09111d; --ink:#edf4fc; --muted:#94a3b8; --accent:#5eead4; --blue:#60a5fa; --violet:#a78bfa; --amber:#fbbf24; --red:#fb7185; --line:#26364a; --control:#111c2b; --track:#1b2a3d; --hover:#142235; }} }}
    :root[data-theme="dark"] {{ color-scheme:dark; --bg:#09111d; --ink:#edf4fc; --muted:#94a3b8; --accent:#5eead4; --blue:#60a5fa; --violet:#a78bfa; --amber:#fbbf24; --red:#fb7185; --line:#26364a; --control:#111c2b; --track:#1b2a3d; --hover:#142235; }}
    :root[data-theme="light"] {{ color-scheme:light; --bg:#f7f8fa; --ink:#172033; --muted:#64748b; --accent:#087f6d; --blue:#2563eb; --violet:#7c3aed; --amber:#b45309; --red:#be123c; --line:#d7dde7; --control:#fff; --track:#e3e8ef; --hover:#edf1f6; }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif }}
    main {{ max-width:1440px; margin:auto; padding:30px 24px 64px }}
    h1 {{ margin:0; font-size:clamp(30px,4vw,48px); letter-spacing:-.04em; white-space:nowrap }}
    h2 {{ margin:0; font-size:18px }} h3 {{ margin:0 0 12px; font-size:14px; color:var(--muted) }}
    .header {{ display:grid; grid-template-columns:auto minmax(280px,1fr) auto; gap:28px; align-items:center }}
    .header-copy {{ max-width:760px }}
    .eyebrow {{ color:var(--accent); text-transform:uppercase; letter-spacing:.15em; font-size:12px; font-weight:800 }}
    .subtitle,.muted,.definition {{ color:var(--muted) }} .subtitle {{ margin:4px 0 0 }}
    .theme-toggle {{ border:1px solid var(--line); background:var(--control); color:var(--ink); border-radius:999px; padding:9px 13px; cursor:pointer; font:inherit; white-space:nowrap }}
    .theme-toggle:hover {{ background:var(--hover) }}
    .filters,.cards,.grid,.metric-grid,.quality-grid {{ display:grid; gap:14px }}
    .filters {{ grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); margin:28px 0 14px }}
    .custom-dates {{ display:none; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:14px }} .custom-dates.visible {{ display:grid }}
    .cards {{ grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); margin:18px 0 }}
    .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); margin-bottom:14px }}
    .metric-grid,.quality-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)) }}
    .panel,.card {{ border:0; background:var(--bg) }}
    select,input {{ border:1px solid var(--line); background:var(--control); border-radius:12px }}
    .panel {{ padding:18px 0; overflow:auto }} .panel.full {{ margin-bottom:14px }} .card {{ padding:14px 0 }}
    .panel-head {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:17px }}
    .card-label {{ display:flex; align-items:center; gap:6px; color:var(--muted) }}
    .card strong {{ display:block; font-size:27px; letter-spacing:-.03em }} label {{ color:var(--muted) }}
    .info {{ position:relative; width:18px; height:18px; border:1px solid var(--line); border-radius:50%; background:transparent; color:var(--muted); padding:0; cursor:help; font:700 11px/16px Inter,ui-sans-serif,system-ui,sans-serif }}
    .info::after {{ content:attr(data-tooltip); position:absolute; z-index:5; left:50%; bottom:calc(100% + 8px); width:min(280px,70vw); padding:9px 11px; border:1px solid var(--line); border-radius:9px; background:var(--control); color:var(--ink); font:12px/1.4 Inter,ui-sans-serif,system-ui,sans-serif; text-align:left; text-transform:none; letter-spacing:0; box-shadow:0 8px 24px rgb(0 0 0 / .16); opacity:0; pointer-events:none; transform:translate(-50%,4px); transition:opacity .12s,transform .12s }}
    .info:hover::after,.info:focus-visible::after {{ opacity:1; transform:translate(-50%,0) }}
    .delta {{ font-size:12px; margin-top:4px }} .delta.better {{ color:var(--accent) }} .delta.worse {{ color:var(--red) }} .delta.neutral {{ color:var(--muted) }}
    .badge {{ display:none; width:max-content; margin-top:10px; padding:5px 9px; border:1px solid var(--accent); border-radius:99px; color:var(--accent); font-size:11px; text-transform:uppercase; letter-spacing:.08em }} .badge.visible {{ display:block }}
    label {{ display:grid; gap:6px; font-size:11px; text-transform:uppercase; letter-spacing:.08em }}
    select,input {{ color:var(--ink); padding:10px 12px; width:100% }}
    table {{ width:100%; border-collapse:collapse; white-space:nowrap }} th,td {{ padding:9px 8px; border-bottom:1px solid var(--line); text-align:left }}
    th {{ color:var(--muted); font-size:11px; text-transform:uppercase; cursor:pointer }} tbody tr:hover {{ background:var(--hover) }}
    .bars {{ display:grid; gap:10px }} .bar {{ display:grid; grid-template-columns:minmax(110px,1.2fr) 4fr auto; gap:10px; align-items:center }}
    .track {{ height:11px; display:flex; background:var(--track); border-radius:99px; overflow:hidden }} .fill {{ height:100%; background:var(--blue) }}
    .seg-cache {{ background:var(--blue) }} .seg-uncached {{ background:var(--violet) }} .seg-visible {{ background:var(--accent) }} .seg-reasoning {{ background:var(--amber) }} .seg-other {{ background:var(--red) }}
    .legend {{ display:flex; flex-wrap:wrap; gap:12px; margin:0 0 15px; color:var(--muted); font-size:12px }} .legend i {{ display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px }}
    .stat {{ padding:12px 0; border:0; background:var(--bg) }} .stat b {{ display:block; font-size:20px }}
    .status {{ display:flex; height:13px; border-radius:99px; overflow:hidden; background:var(--track); margin:12px 0 }} .completed {{ background:var(--accent) }} .aborted {{ background:var(--red) }} .progress {{ background:var(--amber) }}
    .empty {{ color:var(--muted); padding:26px 0 }} .definition {{ font-size:12px; margin:12px 0 0 }} footer {{ color:var(--muted); margin-top:26px }}
    details {{ border-top:1px solid var(--line); margin-top:18px; padding-top:14px }} summary {{ cursor:pointer; color:var(--muted) }}
    @media (max-width:850px) {{ .header {{ grid-template-columns:1fr auto; gap:12px }} .header-copy {{ grid-column:1 / -1; grid-row:2 }} .grid {{ grid-template-columns:1fr }} .bar {{ grid-template-columns:minmax(90px,1fr) 2fr auto }} }}
  </style>
</head>
<body><main>
  <header class="header"><h1>CLI Consumption</h1><div class="header-copy"><div class="eyebrow">Local-first AI CLI observability</div><p class="subtitle">Understand activity, responsiveness, token composition, and workflows without exporting prompts, responses, or tool arguments.</p></div><button class="theme-toggle" id="themeToggle" type="button" aria-label="Switch color theme">Theme</button></header>
  <div class="badge" id="privacyBadge">Share-safe dashboard</div>
  <section class="filters">
    <label>Period<select id="period"><option value="all">All history</option><option value="7">Latest 7 days</option><option value="30">Latest 30 days</option><option value="90">Latest 90 days</option><option value="custom">Custom range</option></select></label>
    <label>Provider<select id="provider"></select></label>
    <label>Machine<select id="machine"></select></label>
    <label>Project<select id="project"></select></label>
    <label>Model<select id="model"></select></label>
  </section>
  <section class="custom-dates" id="customDates"><label>From<input type="date" id="from"></label><label>To<input type="date" id="to"></label></section>
  <section class="cards" id="cards"></section>
  <section class="grid">
    <article class="panel"><div class="panel-head"><h2>Activity and token composition</h2><span class="muted" id="activityCaption"></span></div><div class="legend"><span><i class="seg-cache"></i>Cached input</span><span><i class="seg-uncached"></i>Uncached input</span><span><i class="seg-visible"></i>Visible output</span><span><i class="seg-reasoning"></i>Reasoning</span><span><i class="seg-other"></i>Unattributed</span></div><div class="bars" id="activity"></div></article>
    <article class="panel"><div class="panel-head"><h2>Tools</h2><span class="muted">names only</span></div><h3>Top tools</h3><div class="bars" id="tools"></div><h3 style="margin-top:20px">Categories</h3><div class="bars" id="toolCategories"></div><h3 style="margin-top:20px">Frequent transitions</h3><div class="bars" id="toolTransitions"></div></article>
  </section>
  <section class="grid">
    <article class="panel"><div class="panel-head"><h2>Models</h2><span class="muted">tokens and efficiency</span></div><div id="models"></div></article>
    <article class="panel"><div class="panel-head"><h2>Turn performance</h2><span class="muted">p50 / p75 / p95</span></div><div id="performance"></div><p class="definition">Duration is provider-reported. Turn rate is closed turns per active hour: overlapping intervals are merged in detailed reports, while share-safe reports sum turn durations. It is an activity measure, not a productivity or quality score.</p></article>
  </section>
  <section class="grid">
    <article class="panel"><div class="panel-head"><h2>Workflow complexity</h2><span class="muted">turns, context, delegation</span></div><div class="metric-grid" id="workflow"></div><h3 style="margin-top:20px">Subagent roles</h3><div class="bars" id="agentRoles"></div></article>
    <article class="panel"><div class="panel-head"><h2>Turn outcomes</h2><span class="muted">technical completion</span></div><div id="outcomes"></div><p class="definition">Completed means the provider closed the turn; it does not measure task quality or success.</p></article>
  </section>
  <section class="grid">
    <article class="panel"><div class="panel-head"><h2>Context pressure</h2><span class="muted">latest-call input / context window</span></div><div class="metric-grid" id="contextPressure"></div><h3 style="margin-top:20px">Turn configurations</h3><div class="bars" id="turnConfigurations"></div></article>
    <article class="panel"><div class="panel-head"><h2>Technical work items</h2><span class="muted">content-free intervals</span></div><div class="metric-grid" id="workReliability"></div><h3 style="margin-top:20px">Time by category</h3><div class="bars" id="workKinds"></div></article>
  </section>
  <article class="panel full"><div class="panel-head"><h2>Cohort comparison</h2><label style="min-width:190px">Break down by<select id="cohortDimension"><option value="project">Project</option><option value="model">Model</option><option value="effort">Reasoning effort</option><option value="mode">Collaboration mode</option><option value="delegation">Delegation</option><option value="compaction">Compaction</option></select></label></div><div id="cohorts"></div><p class="definition">Cohort differences are correlations. They do not establish productivity, quality, or causality.</p></article>
  <article class="panel full"><div class="panel-head"><h2>Data quality</h2><span class="muted">coverage and ingestion health</span></div><div class="quality-grid" id="quality"></div></article>
  <article class="panel full"><div class="panel-head"><h2>Conversation explorer</h2><span class="muted" id="conversationCount"></span></div><div id="table"></div><div id="conversationDetail"></div></article>
  <details><summary>Metric definitions and privacy notes</summary><p class="definition">Token events are local usage metadata, not billing data. Additive counters are summed for selected closed turns, including unassigned events from providers without reliable turns. Conversation aggregates and latest-context snapshots are included in full when their conversation overlaps the selected period; they cannot be attributed precisely to that period or compared as additive usage. Providers with unavailable token semantics contribute no token counters. Cache rate is cached input divided by input. Reasoning share is reasoning output divided by output. Context pressure uses the latest model-call input tokens divided by the reported model context window; it is not cumulative spend. Tokens per turn use completed and aborted additive turn totals. Subagent tokens may overlap parent totals. Detailed exports retain operationally sensitive project, machine, model, tool, role, and timestamp metadata. Share-safe exports pseudonymize labels, group tools, round dates to days, and omit CSV files.</p></details>
  <footer>Generated as a self-contained file. No network request is made.</footer>
</main>
<script>
const data={payload};
const $=id=>document.getElementById(id), number=new Intl.NumberFormat(), compact=new Intl.NumberFormat(undefined,{{notation:'compact',maximumFractionDigits:1}});
const fmt=n=>number.format(Math.round(Number(n)||0)), short=n=>compact.format(Number(n)||0), pct=n=>`${{(Number(n)||0).toFixed(1)}}%`;
const convByKey=Object.fromEntries(data.conversations.map(c=>[c.key,c]));
const settingByTurn=Object.fromEntries(data.turnSettings.filter(s=>s.turnKey!==null).map(s=>[s.turnKey,s]));
const validDate=v=>v&&Number.isFinite(Date.parse(v)), day=v=>validDate(v)?v.slice(0,10):'unknown';
const total=(rows,key)=>rows.reduce((n,r)=>n+(Number(r[key])||0),0);
const ratio=(a,b)=>b?100*a/b:0;
let currentSlice=null,tableSort={{key:'startedAt',direction:-1}};
function options(id,values){{const e=$(id),current=e.value;e.innerHTML='<option value="">All</option>'+[...new Set(values.filter(Boolean))].sort().map(v=>`<option value="${{escapeHtml(v)}}">${{escapeHtml(v)}}</option>`).join('');e.value=current;}}
function escapeHtml(v){{return String(v).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
function percentile(values,p){{const xs=values.map(Number).filter(Number.isFinite).sort((a,b)=>a-b);if(!xs.length)return null;const i=(xs.length-1)*p,lo=Math.floor(i),hi=Math.ceil(i);return xs[lo]+(xs[hi]-xs[lo])*(i-lo);}}
function rangeFor(period){{const dates=[...data.conversations.map(c=>c.startedAt),...data.modelCalls.map(c=>c.timestamp)].filter(validDate).map(Date.parse);if(!dates.length)return null;const exportStart=validDate(data.meta.exportWindow?.since)?new Date(data.meta.exportWindow.since):null,exclusiveEnd=validDate(data.meta.exportWindow?.until)?new Date(data.meta.exportWindow.until):null,exportEnd=exclusiveEnd?new Date(exclusiveEnd-1):null;let max=new Date(Math.max(...dates));max.setUTCHours(23,59,59,999);if(exportEnd&&exportEnd<max)max=exportEnd;let start=period==='all'?exportStart:null,end=max;if(period==='custom'){{const f=$('from').value,t=$('to').value;start=f?new Date(`${{f}}T00:00:00Z`):exportStart;end=t?new Date(`${{t}}T23:59:59.999Z`):max;}}else if(period!=='all'){{start=new Date(max);start.setUTCDate(start.getUTCDate()-Number(period)+1);start.setUTCHours(0,0,0,0);}}if(exportStart&&(!start||start<exportStart))start=exportStart;if(exportEnd&&end>exportEnd)end=exportEnd;if(!start)return{{start:null,end,previous:null}};const width=end-start,previous={{start:new Date(start-width-1),end:new Date(start-1)}};return{{start,end,previous:exportStart&&previous.start<exportStart?null:previous}};}}
function inRange(value,range){{if(!range||!validDate(value))return !range||!range.start;const time=Date.parse(value);return(!range.start||time>=range.start)&&time<=range.end;}}
function conversationInRange(c,range){{if(!range)return true;if(!validDate(c.startedAt)&&!validDate(c.endedAt))return false;const start=validDate(c.startedAt)?Date.parse(c.startedAt):-Infinity,end=validDate(c.endedAt)?Date.parse(c.endedAt):Infinity;return(!range.start||end>=range.start)&&(!range.end||start<=range.end);}}
function selected(){{return{{provider:$('provider').value,machine:$('machine').value,project:$('project').value,model:$('model').value,range:rangeFor($('period').value)}};}}
function baseConversations(f){{return data.conversations.filter(c=>(!f.provider||c.provider===f.provider)&&(!f.machine||c.machine===f.machine)&&(!f.project||c.project===f.project)&&(!f.model||c.models.includes(f.model)));}}
function slice(f,range=f.range){{const base=baseConversations(f),keys=new Set(base.map(c=>c.key));let calls=data.modelCalls.filter(c=>{{if(!keys.has(c.conversationKey)||f.model&&c.model!==f.model)return false;const conversation=convByKey[c.conversationKey],semantics=conversation?.tokenSemantics;if(semantics==='conversation-aggregate'||semantics==='context-snapshot')return conversationInRange(conversation,range);if(validDate(c.timestamp))return inRange(c.timestamp,range);return false;}});const modelTurns=f.model?new Set(calls.map(c=>c.turnKey).filter(k=>k!==null)):null;const turns=data.turns.filter(t=>keys.has(t.conversationKey)&&inRange(t.startedAt,range)&&(!modelTurns||modelTurns.has(t.key)));const allowedTurns=new Set(turns.map(t=>t.key));calls=calls.filter(c=>{{const semantics=convByKey[c.conversationKey]?.tokenSemantics;return semantics==='conversation-aggregate'||semantics==='context-snapshot'||c.turnKey===null||allowedTurns.has(c.turnKey);}});const tools=data.toolCalls.filter(t=>keys.has(t.conversationKey)&&inRange(t.timestamp,range)&&(!f.model||t.turnKey!==null&&allowedTurns.has(t.turnKey)));const work=data.workItems.filter(w=>keys.has(w.conversationKey)&&(!f.model||w.turnKey!==null&&allowedTurns.has(w.turnKey))&&inEpochRange(w.startedAtMs,range));const contexts=data.contextSamples.filter(c=>keys.has(c.conversationKey)&&inRange(c.timestamp,range)&&(!f.model||c.turnKey!==null&&allowedTurns.has(c.turnKey)));const settings=data.turnSettings.filter(s=>keys.has(s.conversationKey)&&s.turnKey!==null&&allowedTurns.has(s.turnKey));const compactions=data.compactions.filter(c=>keys.has(c.conversationKey)&&inRange(c.timestamp,range)&&(!f.model||c.turnKey!==null&&allowedTurns.has(c.turnKey)));const activeConversationKeys=new Set([...turns.map(t=>t.conversationKey),...calls.map(c=>c.conversationKey),...tools.map(t=>t.conversationKey)]);const conversations=base.filter(c=>(conversationInRange(c,range)||activeConversationKeys.has(c.key))&&(!f.model||calls.some(x=>x.conversationKey===c.key)));const activeKeys=new Set(conversations.map(c=>c.key));const subagents=data.subagents.filter(s=>s.conversationKey!==null&&activeKeys.has(s.conversationKey)&&inEpochRange(s.createdAtMs,range));return{{conversations,turns,calls,tools,work,contexts,settings,compactions,subagents}};}}
function inEpochRange(value,range){{if(value===null||value===undefined)return !range||!range.start;const time=Number(value);return(!range||!range.start||time>=range.start.getTime())&&(!range||time<=range.end.getTime());}}
function activeMs(turns){{if(data.meta.shareSafe)return total(turns,'durationMs');const groups={{}};turns.forEach(t=>{{const c=convByKey[t.conversationKey];if(!c||!validDate(t.startedAt)||!validDate(t.endedAt))return;(groups[c.machine]??=[]).push([Date.parse(t.startedAt),Date.parse(t.endedAt)]);}});let sum=0;Object.values(groups).forEach(intervals=>{{intervals.sort((a,b)=>a[0]-b[0]);let current=null;intervals.forEach(([start,end])=>{{if(!current)current=[start,end];else if(start<=current[1])current[1]=Math.max(current[1],end);else{{sum+=current[1]-current[0];current=[start,end];}}}});if(current)sum+=current[1]-current[0];}});return sum;}}
function maxConcurrent(turns){{if(data.meta.shareSafe)return null;const byMachine={{}};turns.forEach(t=>{{const c=convByKey[t.conversationKey];if(!c||!validDate(t.startedAt)||!validDate(t.endedAt))return;(byMachine[c.machine]??=[]).push([Date.parse(t.startedAt),1],[Date.parse(t.endedAt),-1]);}});let peak=0;Object.values(byMachine).forEach(points=>{{points.sort((a,b)=>a[0]-b[0]||a[1]-b[1]);let active=0;points.forEach(([,change])=>{{active+=change;peak=Math.max(peak,active);}});}});return peak;}}
function semanticTokenCalls(s){{const closedKeys=new Set(s.turns.filter(t=>t.status==='completed'||t.status==='aborted').map(t=>t.key)),filtered=s.calls.filter(c=>{{const semantics=convByKey[c.conversationKey]?.tokenSemantics;if(semantics==='unavailable')return false;if(semantics==='conversation-aggregate'||semantics==='context-snapshot')return true;return semantics==='additive'&&(c.turnKey===null||closedKeys.has(c.turnKey));}});s.calls=filtered;return filtered;}}
function metrics(s){{const closed=s.turns.filter(t=>t.status==='completed'||t.status==='aborted'),tokenCalls=semanticTokenCalls(s),durations=closed.map(t=>t.durationMs).filter(x=>x!==null),ttfts=closed.map(t=>t.ttftMs).filter(x=>x!==null),additiveTurns=closed.filter(t=>convByKey[t.conversationKey]?.tokenSemantics==='additive'),turnTokens=additiveTurns.map(t=>t.total_tokens),turnTools=closed.map(t=>t.toolCalls),input=total(tokenCalls,'input_tokens'),cached=total(tokenCalls,'cached_input_tokens'),output=total(tokenCalls,'output_tokens'),tokens=total(tokenCalls,'total_tokens'),active=activeMs(closed),pressures=s.contexts.map(c=>100*Number(c.inputTokens)/Number(c.contextWindowTokens)).filter(Number.isFinite),activeDays=new Set(s.turns.map(t=>day(t.startedAt)).filter(x=>x!=='unknown')).size;return{{turns:s.turns.length,completed:s.turns.filter(t=>t.status==='completed').length,aborted:s.turns.filter(t=>t.status==='aborted').length,tokens,tokensPerTurn:percentile(turnTokens,.5),toolsPerTurn:percentile(turnTools,.5),cacheRate:ratio(cached,input),durationP50:percentile(durations,.5),durationP75:percentile(durations,.75),durationP95:percentile(durations,.95),ttftP50:percentile(ttfts,.5),ttftP75:percentile(ttfts,.75),ttftP95:percentile(ttfts,.95),tokenP75:percentile(turnTokens,.75),tokenP95:percentile(turnTokens,.95),toolP75:percentile(turnTools,.75),toolP95:percentile(turnTools,.95),abortRate:ratio(s.turns.filter(t=>t.status==='aborted').length,closed.length),reasoningShare:ratio(total(tokenCalls,'reasoning_output_tokens'),output),activeMs:active,throughput:active?3600000*closed.length/active:0,pressureP50:percentile(pressures,.5),pressureP95:percentile(pressures,.95),activeDays}};}}
function delta(current,previous,preference='neutral'){{if(previous===null||previous===undefined||!Number.isFinite(previous)||previous===0)return'';const change=100*(current-previous)/Math.abs(previous),better=preference==='higher'?change>0:preference==='lower'?change<0:null,style=better===null?'neutral':better?'better':'worse';return`<div class="delta ${{style}}">${{change>=0?'+':''}}${{change.toFixed(1)}}% vs previous period</div>`;}}
function card(label,value,current,previous,tooltip,preference='neutral'){{return`<div class="card"><div class="card-label"><span>${{label}}</span><button class="info" type="button" aria-label="${{escapeHtml(label)}}: ${{escapeHtml(tooltip)}}" data-tooltip="${{escapeHtml(tooltip)}}" title="${{escapeHtml(tooltip)}}">i</button></div><strong>${{value}}</strong>${{delta(current,previous,preference)}}</div>`;}}
function stat(label,value,detail=''){{return`<div class="stat"><span class="muted">${{label}}</span><b>${{value}}</b>${{detail?`<small class="muted">${{detail}}</small>`:''}}</div>`;}}
function group(rows,key,value=null){{const out={{}};rows.forEach(r=>{{const label=r[key]||'unknown';out[label]=(out[label]||0)+(value?(Number(r[value])||0):1);}});return Object.entries(out).sort((a,b)=>b[1]-a[1]);}}
function toolCategory(name){{const categories=['Agent coordination','Shell and processes','Files and workspace','Web','Planning','Media','Integrations','Other'];if(categories.includes(name))return name;if(['spawn_agent','wait_agent','list_agents','send_message','followup_task','interrupt_agent'].includes(name))return'Agent coordination';if(['exec_command','write_stdin','wait'].includes(name))return'Shell and processes';if(['apply_patch','view_image'].includes(name))return'Files and workspace';if(name==='web__run')return'Web';if(['update_plan','create_goal','get_goal','update_goal'].includes(name))return'Planning';if(name.includes('imagegen'))return'Media';if(name.startsWith('mcp__')||name.includes('mcp_resource'))return'Integrations';return'Other';}}
function drawBars(id,rows,formatter=fmt){{const shown=rows.slice(0,12),max=Math.max(...shown.map(x=>x[1]),1);$(id).innerHTML=shown.map(([k,v])=>`<div class="bar"><span title="${{escapeHtml(k)}}">${{escapeHtml(k)}}</span><div class="track"><div class="fill" style="width:${{100*v/max}}%"></div></div><b>${{formatter(v)}}</b></div>`).join('')||'<div class="empty">No data.</div>';}}
function drawActivity(calls,range){{const totals={{}};calls.forEach(c=>{{const key=day(c.timestamp),row=totals[key]??={{cached:0,uncached:0,visible:0,reasoning:0,other:0,total:0}};row.cached+=Number(c.cached_input_tokens)||0;row.uncached+=Number(c.uncached_input_tokens)||0;row.visible+=Number(c.visible_output_tokens)||0;row.reasoning+=Number(c.reasoning_output_tokens)||0;row.other+=Number(c.unattributed_tokens)||0;row.total+=Number(c.total_tokens)||0;}});const observed=Object.keys(totals).filter(k=>k!=='unknown').sort(),end=range?.end||new Date(observed.length?`${{observed.at(-1)}}T23:59:59Z`:Date.now()),start=new Date(Math.max(range?.start?.getTime()||-Infinity,end.getTime()-30*86400000)),rows=[];start.setUTCHours(0,0,0,0);for(let cursor=new Date(start);cursor<=end;cursor.setUTCDate(cursor.getUTCDate()+1)){{const label=cursor.toISOString().slice(0,10);rows.push([label,totals[label]||{{cached:0,uncached:0,visible:0,reasoning:0,other:0,total:0}}]);}}const shown=rows.slice(-31),max=Math.max(...shown.map(([,v])=>v.total),1);$('activity').innerHTML=shown.map(([label,v])=>`<div class="bar"><span>${{escapeHtml(label)}}</span><div class="track">${{[['cache','cached'],['uncached','uncached'],['visible','visible'],['reasoning','reasoning'],['other','other']].map(([css,key])=>`<div class="seg-${{css}}" style="width:${{100*v[key]/max}}%" title="${{key}}: ${{fmt(v[key])}}"></div>`).join('')}}</div><b>${{short(v.total)}}</b></div>`).join('')||'<div class="empty">No data.</div>';$('activityCaption').textContent=shown.length?`${{shown.length}} calendar days shown`:'';}}
function renderModels(calls){{const rows={{}};calls.forEach(c=>{{const r=rows[c.model]??={{calls:0,tokens:0,input:0,cached:0,output:0,reasoning:0,turns:new Set}};r.calls++;r.tokens+=Number(c.total_tokens)||0;r.input+=Number(c.input_tokens)||0;r.cached+=Number(c.cached_input_tokens)||0;r.output+=Number(c.output_tokens)||0;r.reasoning+=Number(c.reasoning_output_tokens)||0;if(c.turnKey!==null)r.turns.add(c.turnKey);}});const entries=Object.entries(rows).sort((a,b)=>b[1].tokens-a[1].tokens);$('models').innerHTML=entries.length?`<table><thead><tr><th>Model</th><th>Tokens</th><th>Tokens / turn</th><th>Usage events</th><th>Cache</th><th>Reasoning</th></tr></thead><tbody>${{entries.map(([name,r])=>`<tr><td>${{escapeHtml(name)}}</td><td>${{fmt(r.tokens)}}</td><td>${{short(r.tokens/(r.turns.size||1))}}</td><td>${{fmt(r.calls)}}</td><td>${{pct(ratio(r.cached,r.input))}}</td><td>${{pct(ratio(r.reasoning,r.output))}}</td></tr>`).join('')}}</tbody></table>`:'<div class="empty">No data.</div>';}}
function toolTransitions(tools){{const byTurn={{}};tools.filter(t=>t.turnKey!==null).forEach(t=>(byTurn[t.turnKey]??=[]).push(t));const transitions=[];Object.values(byTurn).forEach(rows=>{{rows.sort((a,b)=>a.sequence-b.sequence);for(let i=1;i<rows.length;i++)transitions.push({{transition:`${{rows[i-1].tool}} → ${{rows[i].tool}}`}});}});return group(transitions,'transition');}}
function renderOutcomes(turns){{const counts={{completed:0,aborted:0,'in-progress':0}};turns.forEach(t=>counts[t.status]=(counts[t.status]||0)+1);const totalTurns=turns.length||1;$('outcomes').innerHTML=`<div class="status"><div class="completed" style="width:${{100*(counts.completed||0)/totalTurns}}%"></div><div class="aborted" style="width:${{100*(counts.aborted||0)/totalTurns}}%"></div><div class="progress" style="width:${{100*(counts['in-progress']||0)/totalTurns}}%"></div></div><div class="metric-grid">${{stat('Completed',fmt(counts.completed||0),pct(100*(counts.completed||0)/totalTurns))}}${{stat('Aborted',fmt(counts.aborted||0),pct(100*(counts.aborted||0)/totalTurns))}}${{stat('In progress',fmt(counts['in-progress']||0),pct(100*(counts['in-progress']||0)/totalTurns))}}${{stat('Closed turns',fmt((counts.completed||0)+(counts.aborted||0)))}}</div>`;}}
function renderPerformance(m){{$('performance').innerHTML=`<table><thead><tr><th>Metric</th><th>p50</th><th>p75</th><th>p95</th></tr></thead><tbody><tr><td>TTFT</td><td>${{formatDuration(m.ttftP50)}}</td><td>${{formatDuration(m.ttftP75)}}</td><td>${{formatDuration(m.ttftP95)}}</td></tr><tr><td>Turn duration</td><td>${{formatDuration(m.durationP50)}}</td><td>${{formatDuration(m.durationP75)}}</td><td>${{formatDuration(m.durationP95)}}</td></tr><tr><td>Additive tokens / turn</td><td>${{short(m.tokensPerTurn)}}</td><td>${{short(m.tokenP75)}}</td><td>${{short(m.tokenP95)}}</td></tr><tr><td>Tools / turn</td><td>${{fmt(m.toolsPerTurn)}}</td><td>${{fmt(m.toolP75)}}</td><td>${{fmt(m.toolP95)}}</td></tr></tbody></table>`;}}
function renderContext(s,m){{const high=s.contexts.filter(c=>100*Number(c.inputTokens)/Number(c.contextWindowTokens)>=80).length,covered=new Set(s.contexts.map(c=>c.turnKey).filter(k=>k!==null)).size;$('contextPressure').innerHTML=stat('Median pressure',pct(m.pressureP50))+stat('p95 pressure',pct(m.pressureP95))+stat('Samples at ≥80%',fmt(high))+stat('Turn coverage',pct(ratio(covered,s.turns.length)))+stat('Compactions',fmt(s.compactions.length),`${{(100*s.compactions.length/Math.max(s.turns.length,1)).toFixed(1)}} / 100 turns`)+stat('Reasoning share',pct(m.reasoningShare));const configs=[];s.settings.forEach(x=>{{if(x.effort)configs.push({{label:`effort: ${{x.effort}}`}});if(x.mode)configs.push({{label:`mode: ${{x.mode}}`}});if(x.tier)configs.push({{label:`tier: ${{x.tier}}`}});}});drawBars('turnConfigurations',group(configs,'label'));}}
function renderWork(s){{const timed=s.work.filter(w=>w.durationMs!==null),closed=s.work.filter(w=>w.status==='completed'||w.status==='failed'),failed=s.work.filter(w=>w.status==='failed'),durations=timed.map(w=>w.durationMs);$('workReliability').innerHTML=stat('Observed items',fmt(s.work.length))+stat('Duration coverage',pct(ratio(timed.length,s.work.length)))+stat('Technical failure rate',pct(ratio(failed.length,closed.length)))+stat('Median duration',formatDuration(percentile(durations,.5)),`p95 ${{formatDuration(percentile(durations,.95))}}`);drawBars('workKinds',group(timed,'kind','durationMs'),formatDuration);}}
function delegationStats(s){{const edges=s.subagents.filter(x=>x.childConversationKey!==null),children=new Set(edges.map(x=>x.childConversationKey)),closedChildren=[...children].filter(key=>{{const turns=s.turns.filter(t=>t.conversationKey===key);return turns.length&&turns.every(t=>t.status!=='in-progress');}}),fanouts={{}};edges.forEach(e=>fanouts[e.conversationKey]=(fanouts[e.conversationKey]||0)+1);const adjacency={{}};edges.forEach(e=>(adjacency[e.conversationKey]??=[]).push(e.childConversationKey));function depth(key,path=new Set()){{if(path.has(key))return 0;const next=new Set(path);next.add(key);return 1+Math.max(0,...(adjacency[key]||[]).map(child=>depth(child,next)));}}return{{mapped:edges.length,closed:closedChildren.length,maxFanout:Math.max(0,...Object.values(fanouts)),maxDepth:Math.max(0,...Object.keys(adjacency).map(Number).map(key=>depth(key)-1))}};}}
function cohortLabel(turn,dimension,s){{const conv=convByKey[turn.conversationKey],setting=settingByTurn[turn.key];if(dimension==='project')return conv?.project||'unknown';if(dimension==='model')return setting?.model||conv?.models?.join(', ')||'unknown';if(dimension==='effort')return setting?.effort||'unknown';if(dimension==='mode')return setting?.mode||'unknown';if(dimension==='delegation')return s.subagents.some(x=>x.conversationKey===turn.conversationKey)?'delegated':'not delegated';if(dimension==='compaction')return s.compactions.some(x=>x.conversationKey===turn.conversationKey)?'compacted':'not compacted';return'unknown';}}
function renderCohorts(s){{const dimension=$('cohortDimension').value,rows={{}},pressureByTurn={{}};s.contexts.forEach(c=>{{if(c.turnKey===null)return;(pressureByTurn[c.turnKey]??=[]).push(100*Number(c.inputTokens)/Number(c.contextWindowTokens));}});s.turns.filter(t=>t.status==='completed'||t.status==='aborted').forEach(t=>{{const label=cohortLabel(t,dimension,s),r=rows[label]??={{turns:[],durations:[],tokens:[],tools:[],pressures:[],aborted:0}};r.turns.push(t);if(t.durationMs!==null)r.durations.push(t.durationMs);r.tokens.push(t.total_tokens);r.tools.push(t.toolCalls);r.pressures.push(...(pressureByTurn[t.key]||[]));if(t.status==='aborted')r.aborted++;}});const entries=Object.entries(rows).filter(([,r])=>!data.meta.shareSafe||r.turns.length>=5).sort((a,b)=>b[1].turns.length-a[1].turns.length);$('cohorts').innerHTML=entries.length?`<table><thead><tr><th>Cohort</th><th>Closed turns</th><th>Median duration</th><th>Median tokens</th><th>Tools / turn</th><th>Context p95</th><th>Abort rate</th></tr></thead><tbody>${{entries.map(([label,r])=>`<tr><td>${{escapeHtml(label)}}</td><td>${{fmt(r.turns.length)}}</td><td>${{formatDuration(percentile(r.durations,.5))}}</td><td>${{short(percentile(r.tokens,.5))}}</td><td>${{(total(r.tools.map(value=>({{value}})),'value')/r.turns.length).toFixed(1)}}</td><td>${{pct(percentile(r.pressures,.95))}}</td><td>${{pct(ratio(r.aborted,r.turns.length))}}</td></tr>`).join('')}}</tbody></table>`:'<div class="empty">No cohort has enough data for this selection.</div>';}}
function renderQuality(s,f){{const durationCoverage=ratio(s.turns.filter(t=>t.durationMs!==null).length,s.turns.length),ttftCoverage=ratio(s.turns.filter(t=>t.ttftMs!==null).length,s.turns.length),unknown=ratio(s.calls.filter(c=>!c.model||c.model==='unknown').length,s.calls.length),unattributed=ratio(total(s.calls,'unattributed_tokens'),total(s.calls,'total_tokens')),unmapped=ratio(s.subagents.filter(x=>x.childConversationKey===null).length,s.subagents.length),tokenSemantics=[...new Set(s.conversations.map(c=>c.tokenSemantics))].sort().join(', ')||'unavailable',runs=data.ingestionRuns.filter(r=>(!f.provider||r.provider===f.provider)&&inRange(r.ingestedAt,f.range)),latest=runs.map(r=>r.ingestedAt).filter(Boolean).sort().at(-1),malformed=total(runs,'malformed'),duplicates=total(runs,'duplicates');$('quality').innerHTML=stat('Turn duration coverage',pct(durationCoverage))+stat('TTFT coverage',pct(ttftCoverage))+stat('Token semantics',tokenSemantics)+stat('Unknown model events',pct(unknown))+stat('Unattributed tokens',pct(unattributed))+stat('Unmapped child threads',pct(unmapped))+stat('Malformed records',fmt(malformed))+stat('Deduplicated conversations',fmt(duplicates))+stat('Latest matching ingestion',latest?new Date(latest).toLocaleString():'Unknown');}}
function sortExplorer(key){{if(tableSort.key===key)tableSort.direction*=-1;else tableSort={{key,direction:key==='startedAt'?-1:1}};if(currentSlice)renderTable(currentSlice.conversations,currentSlice.turns);}}
function renderTable(conversations,turns){{const statuses={{}};turns.forEach(t=>{{const s=statuses[t.conversationKey]??={{completed:0,aborted:0,progress:0}};if(t.status==='completed')s.completed++;else if(t.status==='aborted')s.aborted++;else s.progress++;}});const outcome=c=>{{const s=statuses[c.key];return s?.aborted?'contains abort':s?.progress?'in progress':s?.completed?'technically closed':'unknown';}},value=(c,key)=>key==='models'?c.models.join(', '):key==='outcome'?outcome(c):c[key];const rows=conversations.slice().sort((a,b)=>{{const av=value(a,tableSort.key)??'',bv=value(b,tableSort.key)??'';return tableSort.direction*(typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv)));}});$('conversationCount').textContent=`${{rows.length}} conversations`;$('conversationDetail').innerHTML='';$('table').innerHTML=rows.length?`<table><thead><tr><th onclick="sortExplorer('startedAt')">Started</th><th onclick="sortExplorer('provider')">Provider</th><th onclick="sortExplorer('machine')">Machine</th><th onclick="sortExplorer('project')">Project</th><th onclick="sortExplorer('models')">Models</th><th onclick="sortExplorer('turns')">Turns</th><th onclick="sortExplorer('outcome')">Technical status</th><th onclick="sortExplorer('compactions')">Compactions</th><th onclick="sortExplorer('total_tokens')">Tokens</th><th onclick="sortExplorer('durationSeconds')">Wall duration</th></tr></thead><tbody>${{rows.map(c=>`<tr style="cursor:pointer" onclick="showConversation(${{c.key}})"><td>${{escapeHtml(c.startedAt||'')}}</td><td>${{escapeHtml(c.provider)}}</td><td>${{escapeHtml(c.machine)}}</td><td>${{escapeHtml(c.project)}}</td><td>${{escapeHtml(c.models.join(', '))}}</td><td>${{fmt(c.turns)}}</td><td>${{outcome(c)}}</td><td>${{fmt(c.compactions)}}</td><td>${{fmt(c.total_tokens)}}</td><td>${{formatDuration((Number(c.durationSeconds)||0)*1000)}}</td></tr>`).join('')}}</tbody></table>`:'<div class="empty">No conversations match these filters.</div>';}}
function showConversation(key){{if(!currentSlice)return;const c=convByKey[key],turns=currentSlice.turns.filter(t=>t.conversationKey===key),calls=semanticTokenCalls(currentSlice).filter(x=>x.conversationKey===key),tools=currentSlice.tools.filter(x=>x.conversationKey===key),work=currentSlice.work.filter(x=>x.conversationKey===key),contexts=currentSlice.contexts.filter(x=>x.conversationKey===key),closed=turns.filter(t=>t.status==='completed'||t.status==='aborted'),durations=closed.map(t=>t.durationMs).filter(x=>x!==null),ttfts=closed.map(t=>t.ttftMs).filter(x=>x!==null),pressures=contexts.map(x=>100*Number(x.inputTokens)/Number(x.contextWindowTokens));$('conversationDetail').innerHTML=`<details open><summary>${{escapeHtml(c.project)}} · ${{escapeHtml(c.startedAt||'')}}</summary><div class="quality-grid" style="margin-top:14px">${{stat('Turns in range',fmt(turns.length))}}${{stat('Tokens in range',fmt(total(calls,'total_tokens')))}}${{stat('Median TTFT',formatDuration(percentile(ttfts,.5)))}}${{stat('Median duration',formatDuration(percentile(durations,.5)))}}${{stat('Context p95',pct(percentile(pressures,.95)))}}${{stat('Technical work items',fmt(work.length))}}</div><h3 style="margin-top:16px">Tools in this conversation</h3><div class="bars">${{group(tools,'tool').slice(0,8).map(([name,count])=>`<div class="bar"><span>${{escapeHtml(name)}}</span><div class="track"><div class="fill" style="width:${{100*count/Math.max(tools.length,1)}}%"></div></div><b>${{fmt(count)}}</b></div>`).join('')||'<div class="empty">No tool calls.</div>'}}</div></details>`;}}
function formatDuration(ms){{if(ms===null||ms===undefined||!Number.isFinite(Number(ms)))return'—';const seconds=Number(ms)/1000;if(seconds<60)return`${{seconds.toFixed(1)}}s`;if(seconds<3600)return`${{(seconds/60).toFixed(1)}}m`;return`${{(seconds/3600).toFixed(1)}}h`;}}
function render(){{const f=selected(),s=slice(f),m=metrics(s),previous=f.range?.previous?metrics(slice(f,f.range.previous)):null,closed=s.turns.filter(t=>t.status==='completed'||t.status==='aborted'),delegation=delegationStats(s),peak=maxConcurrent(closed),compacted=new Set(s.compactions.map(c=>c.conversationKey)).size,delegated=new Set(s.subagents.map(x=>x.conversationKey)).size,subTokens=total(s.subagents,'tokens'),activeLabel=data.meta.shareSafe?'Summed turn time':'Active time',activeHelp=data.meta.shareSafe?'Sum of provider-reported durations for closed turns; exact overlap cannot be recovered from day-rounded timestamps.':'Union of provider-reported closed-turn intervals per machine, so overlapping turns are counted once.';currentSlice=s;$('cards').innerHTML=card('Closed turns',fmt(m.completed+m.aborted),m.completed+m.aborted,previous?previous.completed+previous.aborted:null,'Turns whose provider status is completed or aborted in the selected range.')+card('Active days',fmt(m.activeDays),m.activeDays,previous?.activeDays,'Distinct UTC calendar days containing at least one turn in the selected range.')+card('Total tokens',short(m.tokens),m.tokens,previous?.tokens,'Provider-reported token counters across all selected providers. Semantics can differ and this is not billing data.')+card('Median additive tokens / turn',short(m.tokensPerTurn),m.tokensPerTurn,previous?.tokensPerTurn,'Median total-token count only for providers whose counters are additive per turn.')+card('Median TTFT',formatDuration(m.ttftP50),m.ttftP50,previous?.ttftP50,'Median provider-reported time to first token across closed turns with TTFT data.','lower')+card('Median duration',formatDuration(m.durationP50),m.durationP50,previous?.durationP50,'Median provider-reported elapsed duration across completed and aborted turns with duration data.','lower')+card('Turn rate',m.throughput.toFixed(1)+'/h',m.throughput,previous?.throughput,'Completed and aborted turns divided by active hours. This is an activity rate, not a productivity or quality score.')+card('Context pressure p95',pct(m.pressureP95),m.pressureP95,previous?.pressureP95,'95th percentile of latest model-call input tokens divided by the reported context-window size.','lower')+card(activeLabel,formatDuration(m.activeMs),m.activeMs,previous?.activeMs,activeHelp);drawActivity(s.calls,f.range);renderPerformance(m);renderModels(s.calls);drawBars('tools',group(s.tools,'tool'));drawBars('toolCategories',group(s.tools.map(t=>({{category:toolCategory(t.tool)}})),'category'));drawBars('toolTransitions',toolTransitions(s.tools));$('workflow').innerHTML=stat('Turns using tools',pct(ratio(s.turns.filter(t=>t.toolCalls>0).length,s.turns.length)))+stat('Compacted conversations',pct(ratio(compacted,s.conversations.length)))+stat('Delegating conversations',pct(ratio(delegated,s.conversations.length)),`${{fmt(s.subagents.length)}} edges`)+stat('Mapped child threads',fmt(delegation.mapped),`${{fmt(delegation.closed)}} technically closed`)+stat('Max delegation fan-out',fmt(delegation.maxFanout))+stat('Max delegation depth',fmt(delegation.maxDepth))+stat('Reported subagent tokens',short(subTokens),'may overlap parent totals')+stat('Peak concurrent turns',peak===null?'Hidden':fmt(peak),data.meta.shareSafe?'exact times rounded':'per-machine peak');drawBars('agentRoles',group(s.subagents,'role'));renderOutcomes(s.turns);renderContext(s,m);renderWork(s);renderCohorts(s);renderQuality(s,f);renderTable(s.conversations,s.turns);}}
options('provider',data.conversations.map(c=>c.provider));options('machine',data.conversations.map(c=>c.machine));options('project',data.conversations.map(c=>c.project));options('model',data.modelCalls.map(c=>c.model));
const dates=data.conversations.map(c=>c.startedAt).filter(validDate).sort(),exportSince=data.meta.exportWindow?.since,exportUntil=data.meta.exportWindow?.until;if(exportSince)$('from').value=exportSince.slice(0,10);else if(dates.length)$('from').value=dates[0].slice(0,10);if(exportUntil){{$('to').value=new Date(Date.parse(exportUntil)-1).toISOString().slice(0,10);}}else if(dates.length)$('to').value=dates.at(-1).slice(0,10);
if(data.meta.shareSafe)$('privacyBadge').classList.add('visible');
const themeButton=$('themeToggle');let savedTheme=null;try{{savedTheme=localStorage.getItem('cli-consumption-theme');}}catch{{}}if(savedTheme==='light'||savedTheme==='dark')document.documentElement.dataset.theme=savedTheme;function currentTheme(){{return document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');}}function updateThemeButton(){{const theme=currentTheme(),next=theme==='dark'?'light':'dark';themeButton.textContent=theme==='dark'?'Light mode':'Dark mode';themeButton.setAttribute('aria-label',`Switch to ${{next}} mode`);}}themeButton.addEventListener('click',()=>{{const next=currentTheme()==='dark'?'light':'dark';document.documentElement.dataset.theme=next;try{{localStorage.setItem('cli-consumption-theme',next);}}catch{{}}updateThemeButton();}});updateThemeButton();
document.querySelectorAll('select,input').forEach(e=>e.addEventListener('change',()=>{{$('customDates').classList.toggle('visible',$('period').value==='custom');render();}}));render();
</script></body></html>"""
