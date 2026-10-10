"""Plain-text rendering of usage reports for terminals and pipes.

Rendering is deliberately separate from aggregation: it consumes a finished
``UsageReport`` and uses only the standard library. Colors are emitted only for an
interactive terminal without ``NO_COLOR``; labels are stripped of control
characters so stored metadata cannot inject terminal escape sequences.
"""

from __future__ import annotations

import os
import shutil
import textwrap
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TextIO

from cli_consumption.usage_report import (
    BILLING_NOTICE,
    ReportView,
    UsageMetrics,
    UsageReport,
    UsageRow,
    resolve_timezone,
)

NOT_AVAILABLE = "n/a"
DEFAULT_WIDTH = 120
MINIMUM_WIDTH = 20
MINIMUM_LABEL_WIDTH = 10
MAXIMUM_TEXT_COLUMN_WIDTH = 24
COLUMN_GAP = "  "
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"
_FLAG_MARKERS = {
    "conversation-aggregate": "agg",
    "context-snapshot": "snap",
    "partial-tokens": "partial",
}
_FLAG_LEGEND = {
    "agg": (
        "agg: includes conversation-aggregate token counters, attributed to the "
        "conversation's period rather than to individual calls."
    ),
    "snap": (
        "snap: includes context-snapshot token counters (a context size, not "
        "cumulative usage)."
    ),
    "partial": "partial: some activity comes from providers without token counters.",
}
_VIEW_TITLES = {
    ReportView.DAILY: ("Daily usage", "Date"),
    ReportView.WEEKLY: ("Weekly usage", "Week of"),
    ReportView.MONTHLY: ("Monthly usage", "Month"),
    ReportView.SESSION: ("Usage by session", "Session"),
}


_Entry = tuple[str, UsageRow | None, UsageMetrics]


@dataclass(frozen=True, slots=True)
class _Column:
    header: str
    value: Callable[[UsageRow | None, UsageMetrics, bool], str]
    # Lower values are hidden first when the terminal is too narrow; None never hides.
    priority: int | None
    align_left: bool = False


def color_enabled(stream: TextIO, environ: Mapping[str, str] | None = None) -> bool:
    """Return whether ANSI styling is appropriate for this output stream."""
    environment = os.environ if environ is None else environ
    if environment.get("NO_COLOR", "") != "":
        return False
    if environment.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty and isatty())
    except (OSError, ValueError):
        return False


def terminal_width(fallback: int = DEFAULT_WIDTH) -> int:
    """Return the usable terminal width, honoring ``COLUMNS``."""
    return max(MINIMUM_WIDTH, shutil.get_terminal_size((fallback, 24)).columns)


def display_width(text: str) -> int:
    """Return the number of terminal cells ``text`` occupies."""
    width = 0
    for character in text:
        if unicodedata.combining(character) or unicodedata.category(character) in {
            "Mn",
            "Me",
            "Cf",
        }:
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _truncate(text: str, width: int) -> str:
    """Shorten ``text`` to ``width`` cells, marking the cut with ``~``."""
    if display_width(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for character in text:
        size = display_width(character)
        if used + size > width - 1:
            break
        kept.append(character)
        used += size
    return "".join(kept) + "~"


def _pad(text: str, width: int, *, left: bool) -> str:
    padding = " " * max(0, width - display_width(text))
    return text + padding if left else padding + text


def _chunks(text: str, width: int) -> list[str]:
    """Split ``text`` into pieces of at most ``width`` cells.

    Pieces end after the last comma or space that fits, or at the width limit when
    a value has no separator.
    """
    pieces: list[str] = []
    current: list[str] = []
    for character in text:
        if (
            current
            and display_width("".join(current)) + display_width(character) > width
        ):
            cut = max(
                (index + 1 for index, item in enumerate(current) if item in ", "),
                default=len(current),
            )
            pieces.append("".join(current[:cut]).rstrip())
            current = current[cut:]
            while current and current[0] == " ":
                current.pop(0)
        current.append(character)
    pieces.append("".join(current))
    return pieces


def safe_label(value: object) -> str:
    """Replace control and format characters that could alter the terminal."""
    return "".join(
        "?"
        if unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        else character
        for character in str(value)
    )


def render_report(report: UsageReport, *, width: int, color: bool = False) -> str:
    """Render a report as an aligned table that fits ``width`` when possible."""
    title, period_header = _VIEW_TITLES[report.view]
    width = max(MINIMUM_WIDTH, width)
    lines = [_style(line, _BOLD, color) for line in _wrap(_title(report, title), width)]
    if not report.rows:
        lines.extend(_wrap("No usage recorded for this selection.", width))
        lines.extend(_style(line, _DIM, color) for line in _wrap(BILLING_NOTICE, width))
        return "\n".join(lines) + "\n"

    entries = _entries(report)
    columns = _columns(report, period_header)
    compact = False
    hidden: list[str] = []
    table = _layout(entries, columns, compact)
    if _table_width(table) > width:
        compact = True
        table = _layout(entries, columns, compact)
    droppable = sorted(
        (column for column in columns if column.priority is not None),
        key=lambda column: column.priority or 0,
    )
    while _table_width(table) > width and droppable:
        dropped = droppable.pop(0)
        hidden.append(dropped.header)
        columns = [column for column in columns if column is not dropped]
        table = _layout(entries, columns, compact)
    table = _fit_label(table, width)
    if _table_width(table) > width:
        lines.extend(_stacked(entries, _columns(report, period_header), width, color))
        lines.extend(
            _style(line, _DIM, color)
            for note in _notes(report, [])
            for line in _wrap(note, width)
        )
        return "\n".join(lines) + "\n"

    widths = [
        max(display_width(row[index]) for row in table) for index in range(len(columns))
    ]
    separator = "-" * (sum(widths) + len(COLUMN_GAP) * (len(widths) - 1))
    for index, cells in enumerate(table):
        line = COLUMN_GAP.join(
            _pad(cell, widths[position], left=columns[position].align_left)
            for position, cell in enumerate(cells)
        ).rstrip()
        if index == 0:
            lines.append(_style(line, _BOLD, color))
            lines.append(separator)
        elif index == len(table) - 1:
            lines.append(separator)
            lines.append(_style(line, _BOLD, color))
        else:
            lines.append(line)
    lines.extend(
        _style(line, _DIM, color)
        for note in _notes(report, hidden)
        for line in _wrap(note, width)
    )
    return "\n".join(lines) + "\n"


def _title(report: UsageReport, title: str) -> str:
    window = report.window
    if not window.bounded:
        scope = "all recorded activity"
    elif report.share_safe:
        bounds = window.metadata(day_precision=True)
        since = (bounds["since"] or "")[:10] or "the beginning"
        until = (bounds["until"] or "")[:10] or "now"
        scope = f"{since} to {until} (UTC days, end exclusive)"
    else:
        zone = resolve_timezone(report.timezone)

        def local(value: datetime | None, missing: str) -> str:
            if value is None:
                return missing
            return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")

        scope = (
            f"{local(window.since, 'the beginning')} to "
            f"{local(window.until, 'now')} (end exclusive)"
        )
    profile = ", share-safe labels" if report.share_safe else ""
    return f"{title}, timezone {report.timezone}, {scope}{profile}"


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_on_hyphens=False) or [""]


def _stacked(
    entries: list[_Entry], columns: list[_Column], width: int, color: bool
) -> list[str]:
    """Render one labelled block per entry when no table layout fits."""
    lines: list[str] = []
    for label, row, metrics in entries:
        indent = " " * (len(label) - len(label.lstrip()) + 2)
        heading = _truncate(label, width)
        lines.append(_style(heading, _BOLD, color) if label == "Total" else heading)
        for column in columns[1:]:
            value = column.value(row, metrics, True)
            if not value:
                continue
            text = f"{column.header}: {value}"
            pieces = _chunks(text, width - len(indent))
            lines.append(indent + pieces[0])
            lines.extend(indent + piece for piece in pieces[1:])
    return lines


def _entries(report: UsageReport) -> list[_Entry]:
    entries: list[_Entry] = []
    for row in report.rows:
        entries.append((_row_label(row), row, row.metrics))
        entries.extend(
            (
                f"  - {_truncate(safe_label(item.label), MAXIMUM_TEXT_COLUMN_WIDTH)}",
                None,
                item.metrics,
            )
            for item in row.breakdown
        )
    entries.append(("Total", None, report.totals))
    return entries


def _row_label(row: UsageRow) -> str:
    if row.session is not None:
        started = row.session.started_at
        if started is None:
            moment = "undated"
        elif len(started) > 10:
            moment = f"{started[:10]} {started[11:16]}"
        else:
            moment = started
        return f"#{row.session.number} {moment}"
    return row.period or "undated"


def _tokens(name: str) -> Callable[[UsageRow | None, UsageMetrics, bool], str]:
    def value(_: UsageRow | None, metrics: UsageMetrics, compact: bool) -> str:
        if metrics.tokens is None:
            return NOT_AVAILABLE
        return _number(getattr(metrics.tokens, name), compact)

    return value


def _count(name: str) -> Callable[[UsageRow | None, UsageMetrics, bool], str]:
    def value(_: UsageRow | None, metrics: UsageMetrics, compact: bool) -> str:
        return _number(getattr(metrics, name), compact)

    return value


def _cache_rate(_: UsageRow | None, metrics: UsageMetrics, __: bool) -> str:
    rate = metrics.cache_rate
    return NOT_AVAILABLE if rate is None else f"{100 * rate:.1f}%"


def _notes(report: UsageReport, hidden: list[str]) -> list[str]:
    markers: set[str] = set()
    unavailable = False
    metrics = [report.totals]
    for row in report.rows:
        metrics.append(row.metrics)
        metrics.extend(item.metrics for item in row.breakdown)
    for item in metrics:
        markers.update(_markers(item))
        unavailable = unavailable or item.tokens is None
    notes = [
        "Input includes cache read and cache write tokens; Output includes "
        "reasoning tokens. Cache % is cache read divided by input."
    ]
    if unavailable:
        notes.append(
            "n/a: the provider records no token counters; this is not a zero value."
        )
    notes.extend(
        _FLAG_LEGEND[marker]
        for marker in ("agg", "snap", "partial")
        if marker in markers
    )
    if hidden:
        notes.append(
            "Hidden columns: "
            + ", ".join(hidden)
            + ". Widen the terminal or use --json for every value."
        )
    notes.append(BILLING_NOTICE)
    return notes


def _markers(metrics: UsageMetrics) -> list[str]:
    return [_FLAG_MARKERS[flag] for flag in metrics.flags if flag in _FLAG_MARKERS]


def _columns(report: UsageReport, period_header: str) -> list[_Column]:
    columns = [
        _Column(period_header, lambda *_: "", None, align_left=True),
    ]
    if report.view is ReportView.SESSION:
        columns.extend(
            [
                _Column(
                    "Provider",
                    lambda row, *_: (
                        ""
                        if row is None or row.session is None
                        else _truncate(
                            safe_label(row.session.provider), MAXIMUM_TEXT_COLUMN_WIDTH
                        )
                    ),
                    6,
                    align_left=True,
                ),
                _Column(
                    "Project",
                    lambda row, *_: (
                        ""
                        if row is None or row.session is None
                        else _truncate(
                            safe_label(row.session.project), MAXIMUM_TEXT_COLUMN_WIDTH
                        )
                    ),
                    5,
                    align_left=True,
                ),
            ]
        )
    columns.extend(
        [
            _Column("Input", _tokens("input"), 10),
            _Column("Cache read", _tokens("cache_read"), 7),
            _Column("Cache write", _tokens("cache_write"), 1),
            _Column("Output", _tokens("output"), 9),
            _Column("Reasoning", _tokens("reasoning"), 2),
            _Column("Total", _tokens("total"), None),
            _Column("Cache %", _cache_rate, 11),
            _Column("Convs", _count("conversations"), 4),
            _Column("Turns", _count("turns"), 8),
            _Column("Calls", _count("calls"), 3),
            _Column(
                "Notes",
                lambda _, metrics, __: ",".join(_markers(metrics)),
                None,
                align_left=True,
            ),
        ]
    )
    return columns


def _layout(
    entries: list[_Entry],
    columns: list[_Column],
    compact: bool,
) -> list[list[str]]:
    table = [[column.header for column in columns]]
    for label, row, metrics in entries:
        cells = [label]
        cells.extend(column.value(row, metrics, compact) for column in columns[1:])
        table.append(cells)
    return table


def _table_width(table: list[list[str]]) -> int:
    widths = [
        max(display_width(row[index]) for row in table)
        for index in range(len(table[0]))
    ]
    return sum(widths) + len(COLUMN_GAP) * (len(widths) - 1)


def _fit_label(table: list[list[str]], width: int) -> list[list[str]]:
    excess = _table_width(table) - width
    if excess <= 0:
        return table
    label_width = max(display_width(row[0]) for row in table)
    target = max(MINIMUM_LABEL_WIDTH, label_width - excess)
    if target >= label_width:
        return table
    return [[_truncate(cell, target), *rest] for cell, *rest in table]


def _number(value: int, compact: bool) -> str:
    if not compact or abs(value) < 10_000:
        return f"{value:,}"
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= divisor:
            return f"{value / divisor:.1f}{suffix}"
    return str(value)  # pragma: no cover - unreachable after the threshold check


def _style(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color and text else text
