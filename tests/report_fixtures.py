"""Synthetic multi-provider records for terminal report and cross-check tests."""

from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from cli_consumption.dashboard import build_dashboard_dataset
from cli_consumption.models import Snapshot
from cli_consumption.reporting import ExportWindow, ReportFilters
from cli_consumption.storage import create_database_engine, ingest_snapshot
from cli_consumption.usage_report import UsageQuery, aggregate_usage

CANARY = "report-canary-7f3a"
PATH_CANARY = "/home/report-canary-user/private/workspace"


def tokens(
    uncached: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    visible: int = 0,
    reasoning: int = 0,
    unattributed: int = 0,
) -> dict[str, int]:
    input_tokens = uncached + cache_read + cache_write
    output_tokens = visible + reasoning
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "uncached_input_tokens": uncached,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "visible_output_tokens": visible,
        "unattributed_tokens": unattributed,
        "total_tokens": input_tokens + output_tokens + unattributed,
    }


ZERO = tokens()


class _Builder:
    def __init__(self) -> None:
        self.snapshots: dict[str, Snapshot] = {}
        self.sequences: defaultdict[str, int] = defaultdict(int)

    def snapshot(self, provider: str) -> Snapshot:
        if provider not in self.snapshots:
            self.snapshots[provider] = Snapshot(provider=provider)
        return self.snapshots[provider]

    def conversation(
        self,
        provider: str,
        name: str,
        *,
        project: str,
        machine: str,
        models: list[str],
        started_at: str | None,
        ended_at: str | None,
        event_count: int = 10,
    ) -> str:
        conversation_id = f"{provider}:{CANARY}-{name}"
        self.snapshot(provider).conversations.append(
            {
                "id": conversation_id,
                "provider": provider,
                "external_id": f"{CANARY}-{name}",
                "source_machine": machine,
                "project": project,
                "project_source": "mapping",
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": None,
                "source": f"{CANARY}:{PATH_CANARY}",
                "models": models,
                "iterations": 0,
                "model_calls": 0,
                "tool_calls": 0,
                "compactions": 0,
                "event_count": event_count,
                "content_hash": "c" * 64,
                **ZERO,
            }
        )
        return conversation_id

    def turn(
        self,
        provider: str,
        conversation_id: str,
        name: str,
        *,
        started_at: str | None,
        status: str = "completed",
    ) -> str:
        turn_id = f"{conversation_id}:{name}"
        self.snapshot(provider).turns.append(
            {
                "id": turn_id,
                "conversation_id": conversation_id,
                "external_id": f"{CANARY}-{name}",
                "started_at": started_at,
                "ended_at": started_at if status != "in-progress" else None,
                "status": status,
                "duration_ms": 1_000 if status != "in-progress" else None,
                "time_to_first_token_ms": None,
                "model_calls": 0,
                "tool_calls": 0,
                **ZERO,
            }
        )
        return turn_id

    def call(
        self,
        provider: str,
        conversation_id: str,
        *,
        turn_id: str | None,
        timestamp: str | None,
        model: str,
        usage: dict[str, int],
    ) -> None:
        self.sequences[conversation_id] += 1
        sequence = self.sequences[conversation_id]
        self.snapshot(provider).model_calls.append(
            {
                "id": f"{conversation_id}:call-{sequence}",
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "sequence": sequence,
                "timestamp": timestamp,
                "model": model,
                **usage,
            }
        )

    def tool(self, provider: str, conversation_id: str, *, timestamp: str) -> None:
        self.sequences[conversation_id] += 1
        sequence = self.sequences[conversation_id]
        self.snapshot(provider).tool_calls.append(
            {
                "id": f"{conversation_id}:tool-{sequence}",
                "conversation_id": conversation_id,
                "turn_id": None,
                "sequence": sequence,
                "timestamp": timestamp,
                "tool_name": "exec_command",
                "outer_tool_name": "exec_command",
            }
        )


def report_snapshots(*, hostile_project: bool = False) -> list[Snapshot]:
    """Return deterministic records covering every token semantic and edge case."""
    build = _Builder()
    alpha = "alpha\x9b31m\u202e" if hostile_project else "alpha"

    # Additive provider: a completed turn just before UTC midnight, an open turn,
    # an unattached call, and a call outside a later window.
    codex = build.conversation(
        "codex",
        "codex-1",
        project=alpha,
        machine="laptop",
        models=["gpt-x", "gpt-y"],
        started_at="2026-08-03T23:30:00+00:00",
        ended_at="2026-08-05T10:00:00+00:00",
    )
    closed = build.turn(
        "codex", codex, "turn-1", started_at="2026-08-03T23:30:00+00:00"
    )
    open_turn = build.turn(
        "codex",
        codex,
        "turn-2",
        started_at="2026-08-04T08:00:00+00:00",
        status="in-progress",
    )
    late = build.turn("codex", codex, "turn-3", started_at="2026-08-05T09:00:00+00:00")
    build.call(
        "codex",
        codex,
        turn_id=closed,
        timestamp="2026-08-03T23:40:00+00:00",
        model="gpt-x",
        usage=tokens(1_000, 4_000, 500, 300, 200),
    )
    build.call(
        "codex",
        codex,
        turn_id=open_turn,
        timestamp="2026-08-04T08:05:00+00:00",
        model="gpt-x",
        usage=tokens(900, 0, 0, 100),
    )
    build.call(
        "codex",
        codex,
        turn_id=None,
        timestamp="2026-08-04T12:00:00+00:00",
        model="gpt-y",
        usage=tokens(2_000, 1_000, 0, 400, 100, 5),
    )
    build.call(
        "codex",
        codex,
        turn_id=late,
        timestamp="2026-08-05T09:10:00+00:00",
        model="gpt-y",
        usage=tokens(10_000, 30_000, 0, 1_000),
    )
    build.call(
        "codex",
        codex,
        turn_id=None,
        timestamp=None,
        model="gpt-y",
        usage=tokens(77_777),
    )

    claude = build.conversation(
        "claude",
        "claude-1",
        project="beta",
        machine="desktop",
        models=["claude-a"],
        started_at="2026-08-10T09:00:00+00:00",
        ended_at="2026-08-10T10:00:00+00:00",
    )
    claude_turn = build.turn(
        "claude", claude, "turn-1", started_at="2026-08-10T09:00:00+00:00"
    )
    build.call(
        "claude",
        claude,
        turn_id=claude_turn,
        timestamp="2026-08-10T09:01:00+00:00",
        model="claude-a",
        usage=tokens(500, 20_000, 3_000, 800, 0),
    )
    build.tool("claude", claude, timestamp="2026-08-10T09:02:00+00:00")

    copilot = build.conversation(
        "copilot",
        "copilot-1",
        project="beta",
        machine="laptop",
        models=["copilot-m"],
        started_at="2026-08-12T15:00:00+00:00",
        ended_at="2026-08-12T16:00:00+00:00",
    )
    build.turn("copilot", copilot, "turn-1", started_at="2026-08-12T15:00:00+00:00")
    build.call(
        "copilot",
        copilot,
        turn_id=None,
        timestamp=None,
        model="copilot-m",
        usage=tokens(6_000, 2_000, 0, 700, 0),
    )

    crush = build.conversation(
        "crush",
        "crush-1",
        project="gamma",
        machine="desktop",
        models=["crush-m"],
        started_at="2026-08-20T08:00:00+00:00",
        ended_at=None,
    )
    crush_turn = build.turn(
        "crush",
        crush,
        "turn-1",
        started_at="2026-08-20T08:00:00+00:00",
        status="in-progress",
    )
    build.call(
        "crush",
        crush,
        turn_id=crush_turn,
        timestamp="2026-08-20T08:30:00+00:00",
        model="crush-m",
        usage=tokens(9_000, 0, 0, 0),
    )

    cursor = build.conversation(
        "cursor",
        "cursor-1",
        project="gamma",
        machine="laptop",
        models=["unknown"],
        started_at="2026-08-21T11:00:00+00:00",
        ended_at="2026-08-21T11:30:00+00:00",
    )
    cursor_turn = build.turn(
        "cursor", cursor, "turn-1", started_at="2026-08-21T11:00:00+00:00"
    )
    build.call(
        "cursor",
        cursor,
        turn_id=cursor_turn,
        timestamp="2026-08-21T11:01:00+00:00",
        model="unknown",
        usage=ZERO,
    )

    # Dateless conversations: one with an undated turn, one without any activity.
    undated = build.conversation(
        "codex",
        "codex-undated",
        project=alpha,
        machine="laptop",
        models=["gpt-x"],
        started_at=None,
        ended_at=None,
    )
    build.turn("codex", undated, "turn-1", started_at=None)
    build.conversation(
        "codex",
        "codex-empty",
        project=alpha,
        machine="laptop",
        models=[],
        started_at=None,
        ended_at=None,
    )
    return [
        Snapshot.from_dict(snapshot.to_dict()) for snapshot in build.snapshots.values()
    ]


def seed_report_database(engine: Engine, *, hostile_project: bool = False) -> None:
    for snapshot in report_snapshots(hostile_project=hostile_project):
        ingest_snapshot(engine, snapshot)


CROSSCHECK_FIXTURE = Path(__file__).parent / "fixtures" / "usage_report_crosscheck.json"
# name, since, until, report filters, dashboard filter, share-safe
CROSSCHECK_CASES: tuple[
    tuple[str, str | None, str | None, ReportFilters, dict[str, str], bool], ...
] = (
    ("all-activity", None, None, ReportFilters(), {}, False),
    (
        "utc-window",
        "2026-08-04T00:00:00+00:00",
        "2026-08-13T00:00:00+00:00",
        ReportFilters(),
        {},
        False,
    ),
    (
        "intraday-window-provider",
        "2026-08-04T06:00:00+00:00",
        None,
        ReportFilters(providers=("codex",)),
        {"provider": "codex"},
        False,
    ),
    (
        "until-only-window",
        None,
        "2026-08-11T00:00:00+00:00",
        ReportFilters(),
        {},
        False,
    ),
    (
        "model-filter",
        None,
        None,
        ReportFilters(models=("gpt-y",)),
        {"model": "gpt-y"},
        False,
    ),
    (
        "project-machine-filter",
        None,
        None,
        ReportFilters(projects=("beta",), machines=("laptop",)),
        {"project": "beta", "machine": "laptop"},
        False,
    ),
    ("share-safe", None, None, ReportFilters(), {}, True),
)


def crosscheck_payload(engine: Engine) -> dict[str, Any]:
    """Pair each dashboard dataset with the terminal report totals it must match."""
    cases = []
    for name, since, until, filters, dashboard_filter, share_safe in CROSSCHECK_CASES:
        window = ExportWindow(
            since=None if since is None else datetime.fromisoformat(since),
            until=None if until is None else datetime.fromisoformat(until),
        )
        dataset = build_dashboard_dataset(
            engine, share_safe=share_safe, window=window, filters=filters
        )
        # Ingestion runs carry wall-clock timestamps and never enter token metrics.
        dataset["ingestionRuns"] = []
        report = aggregate_usage(
            engine,
            UsageQuery(window=window, filters=filters, share_safe=share_safe),
        )
        totals = report.totals
        assert totals.tokens is not None
        rate = totals.cache_rate
        cases.append(
            {
                "name": name,
                "dashboardFilter": {
                    "provider": dashboard_filter.get("provider", ""),
                    "machine": dashboard_filter.get("machine", ""),
                    "project": dashboard_filter.get("project", ""),
                    "model": dashboard_filter.get("model", ""),
                },
                "dataset": dataset,
                "expected": {
                    "conversations": totals.conversations,
                    "turns": totals.turns,
                    "calls": totals.calls,
                    "inputTokens": totals.tokens.input,
                    "cacheReadTokens": totals.tokens.cache_read,
                    "cacheWriteTokens": totals.tokens.cache_write,
                    "outputTokens": totals.tokens.output,
                    "reasoningTokens": totals.tokens.reasoning,
                    "totalTokens": totals.tokens.total,
                    "cacheRatePercent": None if rate is None else round(100 * rate, 6),
                },
            }
        )
    return {
        "description": (
            "Generated by tests/report_fixtures.py from synthetic records. The "
            "analytics tests check that dashboard calculations over each dataset "
            "equal the terminal report totals."
        ),
        "cases": cases,
    }


def render_crosscheck_fixture(directory: Path) -> str:
    engine = create_database_engine(directory / "crosscheck.sqlite")
    try:
        seed_report_database(engine)
        payload = crosscheck_payload(engine)
    finally:
        engine.dispose()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        CROSSCHECK_FIXTURE.write_text(
            render_crosscheck_fixture(Path(temporary)), encoding="utf-8"
        )
