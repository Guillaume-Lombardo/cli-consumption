"""Generate the public demo dashboard from deterministic synthetic records only."""

from __future__ import annotations

import argparse
import tempfile
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from cli_consumption.dashboard import generate_dashboard
from cli_consumption.models import Snapshot
from cli_consumption.storage import create_database_engine, ingest_snapshot

SYNTHETIC_MARKER = "cli-consumption-public-demo"
PROVIDERS = ("codex", "claude", "copilot", "crush", "cursor")


def _tokens(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": 0,
        "uncached_input_tokens": input_tokens - cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "visible_output_tokens": output_tokens - reasoning_output_tokens,
        "unattributed_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }


def synthetic_snapshots() -> list[Snapshot]:
    """Return a small, provider-neutral fixture with no source-file dependency."""
    rows = (
        (
            "codex",
            1,
            "2026-08-03",
            "demo-api",
            "demo-laptop",
            "gpt-5.4",
            28000,
            19000,
            4200,
            1100,
            "completed",
        ),
        (
            "codex",
            2,
            "2026-08-08",
            "demo-api",
            "demo-laptop",
            "gpt-5.4",
            41000,
            33000,
            6100,
            1800,
            "completed",
        ),
        (
            "codex",
            3,
            "2026-08-14",
            "demo-web",
            "demo-runner",
            "gpt-5.4-mini",
            17000,
            8000,
            2900,
            500,
            "aborted",
        ),
        (
            "claude",
            4,
            "2026-08-06",
            "demo-web",
            "demo-laptop",
            "claude-sonnet-demo",
            32000,
            21000,
            5300,
            900,
            "completed",
        ),
        (
            "claude",
            5,
            "2026-08-18",
            "demo-worker",
            "demo-runner",
            "claude-sonnet-demo",
            22000,
            12000,
            3700,
            600,
            "completed",
        ),
        (
            "copilot",
            6,
            "2026-08-11",
            "demo-api",
            "demo-laptop",
            "copilot-demo",
            26000,
            14000,
            3900,
            0,
            "completed",
        ),
        (
            "crush",
            7,
            "2026-08-20",
            "demo-worker",
            "demo-runner",
            "crush-demo",
            19000,
            7000,
            2600,
            0,
            "completed",
        ),
        (
            "cursor",
            8,
            "2026-08-23",
            "demo-web",
            "demo-laptop",
            "unknown",
            0,
            0,
            0,
            0,
            "in-progress",
        ),
    )
    snapshots: dict[str, Snapshot] = {
        provider: Snapshot(provider=provider) for provider in PROVIDERS
    }
    sequences: defaultdict[str, int] = defaultdict(int)
    for (
        provider,
        number,
        day,
        project,
        machine,
        model,
        input_tokens,
        cached_tokens,
        output_tokens,
        reasoning_tokens,
        status,
    ) in rows:
        snapshot = snapshots[provider]
        conversation_id = f"{provider}:demo-{number}"
        turn_id = f"{conversation_id}:turn-1"
        started_at = f"{day}T09:00:00+00:00"
        ended_at = None if status == "in-progress" else f"{day}T09:42:00+00:00"
        totals = _tokens(input_tokens, cached_tokens, output_tokens, reasoning_tokens)
        additive = provider in {"codex", "claude"}
        child_totals = totals if additive else _tokens(0, 0, 0, 0)
        snapshot.conversations.append(
            {
                "id": conversation_id,
                "provider": provider,
                "external_id": f"demo-{number}",
                "source_machine": machine,
                "project": project,
                "project_source": "mapping",
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": None if ended_at is None else 2520.0,
                "source": SYNTHETIC_MARKER,
                "models": [model],
                "iterations": 1,
                "model_calls": 1 if additive else 0,
                "tool_calls": 2,
                "compactions": 1 if number in {2, 5} else 0,
                "event_count": 8 + number,
                "content_hash": f"{number}" * 64,
                **totals,
            }
        )
        snapshot.turns.append(
            {
                "id": turn_id,
                "conversation_id": conversation_id,
                "external_id": "turn-1",
                "started_at": started_at,
                "ended_at": ended_at,
                "status": status,
                "duration_ms": None if ended_at is None else 2_520_000,
                "time_to_first_token_ms": 720 + number * 45 if additive else None,
                "model_calls": 1 if additive else 0,
                "tool_calls": 2,
                **child_totals,
            }
        )
        if additive:
            snapshot.model_calls.append(
                {
                    "id": f"{conversation_id}:call-1",
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "sequence": 1,
                    "timestamp": f"{day}T09:03:00+00:00",
                    "model": model,
                    **totals,
                }
            )
        for tool_name in ("exec_command", "apply_patch"):
            sequences[conversation_id] += 1
            sequence = sequences[conversation_id]
            snapshot.tool_calls.append(
                {
                    "id": f"{conversation_id}:tool-{sequence}",
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "sequence": sequence,
                    "timestamp": f"{day}T09:{8 + sequence:02d}:00+00:00",
                    "tool_name": tool_name,
                    "outer_tool_name": tool_name,
                }
            )
        snapshot.work_items.append(
            {
                "id": f"{conversation_id}:work-1",
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "sequence": 1,
                "kind": "file-change",
                "tool_name": "apply_patch",
                "started_at_ms": 1_785_000_000_000 + number * 100_000,
                "completed_at_ms": 1_785_000_060_000 + number * 100_000,
                "duration_ms": 60_000,
                "status": "completed" if status != "aborted" else "failed",
            }
        )
        if additive:
            snapshot.context_samples.append(
                {
                    "id": f"{conversation_id}:context-1",
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "sequence": 1,
                    "timestamp": f"{day}T09:20:00+00:00",
                    "input_tokens": input_tokens,
                    "context_window_tokens": 64_000,
                }
            )
            snapshot.turn_settings.append(
                {
                    "id": f"{conversation_id}:setting-1",
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "model": model,
                    "effort": "high" if number % 2 == 0 else "medium",
                    "collaboration_mode": "default",
                    "service_tier": "standard",
                    "context_window_tokens": 64_000,
                }
            )
        if number in {2, 5}:
            snapshot.compaction_events.append(
                {
                    "id": f"{conversation_id}:compaction-1",
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "sequence": 1,
                    "timestamp": f"{day}T09:30:00+00:00",
                }
            )

    snapshots["codex"].subagents.append(
        {
            "id": "codex:demo-laptop:demo-2",
            "provider": "codex",
            "source_machine": "demo-laptop",
            "parent_thread_id": "demo-1",
            "child_thread_id": "demo-2",
            "status": "completed",
            "created_at_ms": 1_785_000_000_000,
            "updated_at_ms": 1_785_000_060_000,
            "agent_role": "worker",
            "tokens_used": 47_100,
        }
    )
    return [Snapshot.from_dict(snapshot.to_dict()) for snapshot in snapshots.values()]


def build_demo(output: Path) -> None:
    """Create a self-contained HTML dashboard without reading provider files."""
    with tempfile.TemporaryDirectory(prefix="cli-consumption-demo-") as directory:
        engine = create_database_engine(Path(directory) / "demo.sqlite")
        try:
            for index, snapshot in enumerate(synthetic_snapshots(), start=1):
                result = ingest_snapshot(engine, snapshot)
                with engine.begin() as connection:
                    connection.execute(
                        text("UPDATE ingestion_runs SET id = :stable WHERE id = :run"),
                        {
                            "stable": f"00000000-0000-4000-8000-{index:012d}",
                            "run": result.run_id,
                        },
                    )
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE ingestion_runs SET "
                        "ingested_at = '2026-08-30T00:00:00.000000+00:00'"
                    )
                )
            generate_dashboard(engine, output)
            with output.open("a", encoding="utf-8") as handle:
                handle.write("\n")
        finally:
            engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("dashboard.html"),
    )
    build_demo(parser.parse_args().output)


if __name__ == "__main__":
    main()
