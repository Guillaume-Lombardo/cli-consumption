from __future__ import annotations

import io
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from report_fixtures import CANARY, PATH_CANARY, seed_report_database
from sqlalchemy import update
from sqlalchemy.engine import Engine

from cli_consumption.storage import Conversation, create_database_engine
from cli_consumption.terminal_report import (
    color_enabled,
    display_width,
    render_report,
    safe_label,
)
from cli_consumption.usage_report import (
    Breakdown,
    ReportView,
    UsageQuery,
    UsageReport,
    aggregate_usage,
    parse_report_window,
)

ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_database_engine(tmp_path / "usage.sqlite")
    seed_report_database(engine, hostile_project=True)
    # Legacy rows predate control-character validation; render them safely too.
    with engine.begin() as connection:
        connection.execute(
            update(Conversation)
            .where(Conversation.provider == "claude")
            .values(source_machine="desk\x1b]0;title\x07top")
        )
    yield engine
    engine.dispose()


def _report(engine: Engine, **kwargs: Any) -> UsageReport:
    return aggregate_usage(engine, UsageQuery(**kwargs))


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_color_only_for_interactive_terminals_without_no_color() -> None:
    assert color_enabled(_Stream(True), {"TERM": "xterm"})
    assert not color_enabled(_Stream(False), {"TERM": "xterm"})
    assert not color_enabled(_Stream(True), {"NO_COLOR": "1"})
    assert not color_enabled(_Stream(True), {"TERM": "dumb"})
    # An empty NO_COLOR does not disable colors, as the convention specifies.
    assert color_enabled(_Stream(True), {"NO_COLOR": ""})
    assert not color_enabled(io.StringIO(), {})


def test_plain_output_has_no_escape_sequences_even_for_hostile_labels(
    engine: Engine,
) -> None:
    for view in ReportView:
        for breakdown in (None, Breakdown.PROJECT, Breakdown.MACHINE):
            text = render_report(
                _report(engine, view=view, breakdown=breakdown), width=160
            )
            assert "\x1b" not in text
            assert "\x9b" not in text
            assert "\x07" not in text
            assert "\u202e" not in text
    assert "alpha?31m?" in render_report(
        _report(engine, breakdown=Breakdown.PROJECT), width=160
    )
    assert "desk?]0;title?top" in render_report(
        _report(engine, breakdown=Breakdown.MACHINE), width=160
    )


def test_color_output_differs_only_by_styling(engine: Engine) -> None:
    report = _report(engine, breakdown=Breakdown.PROVIDER)
    plain = render_report(report, width=140)
    styled = render_report(report, width=140, color=True)

    assert ANSI.search(styled)
    assert ANSI.sub("", styled) == plain
    # Stored labels never contribute escape sequences of their own.
    hostile = render_report(
        _report(engine, breakdown=Breakdown.MACHINE), width=140, color=True
    )
    assert "\x1b]" not in hostile
    assert set(ANSI.findall(hostile)) <= {"\x1b[0m", "\x1b[1m", "\x1b[2m"}


@pytest.mark.parametrize("width", [40, 60, 80, 100, 160])
@pytest.mark.parametrize("view", list(ReportView))
def test_output_fits_the_terminal_width(
    engine: Engine, width: int, view: ReportView
) -> None:
    text = render_report(
        _report(engine, view=view, breakdown=Breakdown.MODEL), width=width
    )

    assert max(len(line) for line in text.splitlines()) <= width
    assert "Total" in text
    assert "not billing data" in text


def test_narrow_output_compacts_numbers_and_names_hidden_columns(
    engine: Engine,
) -> None:
    wide = render_report(_report(engine), width=200)
    narrow = render_report(_report(engine), width=60)

    assert "92,505" in wide
    assert "Hidden columns" not in wide
    assert "92.5K" in narrow
    assert "Hidden columns: Cache write" in narrow


def test_unavailable_and_flagged_semantics_are_explicit(engine: Engine) -> None:
    text = render_report(_report(engine), width=200)
    lines = {line.split()[0]: line for line in text.splitlines() if line.strip()}

    unavailable = lines["2026-08-21"]
    assert unavailable.count("n/a") == 7
    assert " 0 " not in unavailable.split("n/a")[0]
    assert lines["2026-08-12"].rstrip().endswith("agg")
    assert lines["2026-08-20"].rstrip().endswith("snap")
    assert lines["Total"].rstrip().endswith("agg,snap,partial")
    assert "n/a: the provider records no token counters" in text
    assert "conversation-aggregate" in text
    assert "context-snapshot" in text


def test_rendering_never_includes_identifiers_or_paths(engine: Engine) -> None:
    for view in ReportView:
        for breakdown in (None, *Breakdown):
            for share_safe in (False, True):
                text = render_report(
                    _report(
                        engine, view=view, breakdown=breakdown, share_safe=share_safe
                    ),
                    width=200,
                )
                assert CANARY not in text
                assert PATH_CANARY not in text
                assert "/home/" not in text


def test_share_safe_rendering_pseudonymizes_labels(engine: Engine) -> None:
    text = render_report(
        _report(
            engine,
            view=ReportView.SESSION,
            breakdown=Breakdown.MODEL,
            share_safe=True,
            window=parse_report_window("2026-08-01T10:15:00Z", None),
        ),
        width=200,
    )

    assert "share-safe" in text
    assert "2026-08-01 to now (UTC days" in text
    for private in ("alpha", "beta", "gamma", "laptop", "desktop", "gpt-x"):
        assert private not in text
    assert "project-1" in text
    assert "model-1" in text
    assert "23:30" not in text


def test_window_title_uses_the_report_timezone(engine: Engine) -> None:
    window = parse_report_window("2026-08-04", "2026-08-05", "Europe/Paris")
    text = render_report(
        _report(engine, window=window, timezone="Europe/Paris"), width=200
    )

    assert text.splitlines()[0] == (
        "Daily usage, timezone Europe/Paris, 2026-08-04 00:00 to 2026-08-06 00:00 "
        "(end exclusive)"
    )


def test_open_ended_window_titles(engine: Engine) -> None:
    until_only = render_report(
        _report(engine, window=parse_report_window(None, "2026-08-05")), width=200
    )
    since_only = render_report(
        _report(engine, window=parse_report_window("2026-08-05", None)), width=200
    )

    assert "the beginning to 2026-08-06 00:00 (end exclusive)" in until_only
    assert "2026-08-05 00:00 to now (end exclusive)" in since_only


def test_empty_report_is_explicit(tmp_path: Path) -> None:
    engine = create_database_engine(tmp_path / "empty.sqlite")
    try:
        text = render_report(aggregate_usage(engine, UsageQuery()), width=80)
    finally:
        engine.dispose()

    assert "No usage recorded for this selection." in text
    assert "not billing data" in text


def test_safe_label_replaces_control_and_format_characters() -> None:
    assert safe_label("a\x1b[2Jb‮c\nd") == "a?[2Jb?c?d"
    assert safe_label("projet-été") == "projet-été"


def _cells(line: str) -> int:
    return sum(display_width(character) for character in line)


def test_display_width_counts_terminal_cells() -> None:
    assert display_width("abc") == 3
    assert display_width("项目") == 4
    assert display_width("e\u0301te\u0301") == 3
    assert display_width("\uff46\uff55\uff4c\uff4c") == 8


@pytest.fixture
def wide_label_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_database_engine(tmp_path / "wide.sqlite")
    seed_report_database(engine)
    with engine.begin() as connection:
        connection.execute(
            update(Conversation)
            .where(Conversation.provider == "codex")
            .values(project="项目" * 10, source_machine="maché́ine-" * 4)
        )
    yield engine
    engine.dispose()


@pytest.mark.parametrize("width", [20, 25, 30, 40, 60, 80, 120])
@pytest.mark.parametrize("view", list(ReportView))
@pytest.mark.parametrize("breakdown", [None, Breakdown.PROJECT, Breakdown.MACHINE])
def test_wide_and_combining_labels_fit_display_cells(
    wide_label_engine: Engine,
    width: int,
    view: ReportView,
    breakdown: Breakdown | None,
) -> None:
    text = render_report(
        _report(wide_label_engine, view=view, breakdown=breakdown), width=width
    )

    assert max(_cells(line) for line in text.splitlines()) <= width
    assert "92" in text
    assert "not billing data" in " ".join(text.split())


@pytest.mark.parametrize("width", range(20, 41))
def test_minimum_widths_fit_display_cells(engine: Engine, width: int) -> None:
    text = render_report(
        _report(engine, view=ReportView.SESSION, breakdown=Breakdown.MODEL),
        width=width,
    )

    assert max(_cells(line) for line in text.splitlines()) <= width
    assert "Total" in text


def test_wide_labels_keep_columns_aligned(wide_label_engine: Engine) -> None:
    text = render_report(_report(wide_label_engine, view=ReportView.SESSION), width=200)
    lines = text.splitlines()
    header = next(line for line in lines if line.startswith("Session"))
    total_end = _cells(header[: header.index("Total") + len("Total")])
    for line in lines:
        if line.startswith("#"):
            prefix = line[: len(line) - len(line.lstrip())]
            assert prefix == ""
            cells = 0
            for position, character in enumerate(line):
                cells += display_width(character)
                if cells == total_end:
                    assert line[position] in "0123456789,KMB.a"
                    break
            else:
                pytest.fail("Total column is misaligned")


def test_long_breakdown_labels_do_not_hide_numeric_columns(
    wide_label_engine: Engine,
) -> None:
    text = render_report(
        _report(wide_label_engine, breakdown=Breakdown.MACHINE), width=80
    )
    header = text.splitlines()[1]

    assert "  - mach" in text
    assert all(column in header for column in ("Input", "Total", "Cache %"))


def test_stacked_layout_breaks_values_at_separators(engine: Engine) -> None:
    text = render_report(_report(engine), width=20)

    assert "  Notes: agg,snap," in text.splitlines()
    assert "  partial" in text.splitlines()
