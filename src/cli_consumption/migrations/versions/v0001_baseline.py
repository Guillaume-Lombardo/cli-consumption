"""Adopt the schema published by CLI Consumption 0.1.1.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _conversation() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("source_machine", sa.String(255), nullable=False),
        sa.Column("project", sa.String(512), nullable=False),
        sa.Column("project_source", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(64)),
        sa.Column("ended_at", sa.String(64)),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("models_json", sa.Text(), nullable=False),
        sa.Column("iterations", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("compactions", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("uncached_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reasoning_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("visible_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("unattributed_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
    )
    for column in (
        "provider",
        "external_id",
        "source_machine",
        "project",
        "started_at",
    ):
        op.create_index(f"ix_conversations_{column}", "conversations", [column])


def _child_table(
    name: str,
    columns: list[sa.Column[Any]],
    indexed: tuple[str, ...],
) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.String(1024), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(512),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *columns,
    )
    op.create_index(f"ix_{name}_conversation_id", name, ["conversation_id"])
    for column in indexed:
        op.create_index(f"ix_{name}_{column}", name, [column])


def _turns() -> None:
    _child_table(
        "turns",
        [
            sa.Column("external_id", sa.String(512), nullable=False),
            sa.Column("started_at", sa.String(64)),
            sa.Column("ended_at", sa.String(64)),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("duration_ms", sa.BigInteger()),
            sa.Column("time_to_first_token_ms", sa.BigInteger()),
            sa.Column("model_calls", sa.Integer(), nullable=False),
            sa.Column("tool_calls", sa.Integer(), nullable=False),
            sa.Column("input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("cache_write_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("uncached_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("output_tokens", sa.BigInteger(), nullable=False),
            sa.Column("reasoning_output_tokens", sa.BigInteger(), nullable=False),
            sa.Column("visible_output_tokens", sa.BigInteger(), nullable=False),
            sa.Column("unattributed_tokens", sa.BigInteger(), nullable=False),
            sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        ],
        ("started_at",),
    )


def _model_calls() -> None:
    _child_table(
        "model_calls",
        [
            sa.Column("turn_id", sa.String(1024)),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("timestamp", sa.String(64)),
            sa.Column("model", sa.String(255), nullable=False),
            sa.Column("input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("cache_write_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("uncached_input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("output_tokens", sa.BigInteger(), nullable=False),
            sa.Column("reasoning_output_tokens", sa.BigInteger(), nullable=False),
            sa.Column("visible_output_tokens", sa.BigInteger(), nullable=False),
            sa.Column("unattributed_tokens", sa.BigInteger(), nullable=False),
            sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        ],
        ("turn_id", "timestamp", "model"),
    )


def _tool_calls() -> None:
    _child_table(
        "tool_calls",
        [
            sa.Column("turn_id", sa.String(1024)),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("timestamp", sa.String(64)),
            sa.Column("tool_name", sa.String(512), nullable=False),
            sa.Column("outer_tool_name", sa.String(512), nullable=False),
        ],
        ("turn_id", "timestamp", "tool_name"),
    )


def _work_items() -> None:
    _child_table(
        "work_items",
        [
            sa.Column("turn_id", sa.String(1024)),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("tool_name", sa.String(512)),
            sa.Column("started_at_ms", sa.BigInteger()),
            sa.Column("completed_at_ms", sa.BigInteger()),
            sa.Column("duration_ms", sa.BigInteger()),
            sa.Column("status", sa.String(32), nullable=False),
        ],
        ("turn_id", "kind", "tool_name", "status"),
    )


def _context_samples() -> None:
    _child_table(
        "context_samples",
        [
            sa.Column("turn_id", sa.String(1024)),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("timestamp", sa.String(64)),
            sa.Column("input_tokens", sa.BigInteger(), nullable=False),
            sa.Column("context_window_tokens", sa.BigInteger(), nullable=False),
        ],
        ("turn_id", "timestamp"),
    )


def _turn_settings() -> None:
    _child_table(
        "turn_settings",
        [
            sa.Column("turn_id", sa.String(1024), nullable=False),
            sa.Column("model", sa.String(255)),
            sa.Column("effort", sa.String(64)),
            sa.Column("collaboration_mode", sa.String(64)),
            sa.Column("service_tier", sa.String(64)),
            sa.Column("context_window_tokens", sa.BigInteger()),
        ],
        ("turn_id", "model", "effort", "collaboration_mode", "service_tier"),
    )


def _compaction_events() -> None:
    _child_table(
        "compaction_events",
        [
            sa.Column("turn_id", sa.String(1024)),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("timestamp", sa.String(64)),
        ],
        ("turn_id", "timestamp"),
    )


def _subagents() -> None:
    op.create_table(
        "subagents",
        sa.Column("id", sa.String(1024), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("source_machine", sa.String(255), nullable=False),
        sa.Column("parent_thread_id", sa.String(512), nullable=False),
        sa.Column("child_thread_id", sa.String(512), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger()),
        sa.Column("updated_at_ms", sa.BigInteger()),
        sa.Column("agent_nickname", sa.String(255), nullable=False),
        sa.Column("agent_role", sa.String(255), nullable=False),
        sa.Column("tokens_used", sa.BigInteger()),
    )
    for column in (
        "provider",
        "source_machine",
        "parent_thread_id",
        "child_thread_id",
        "status",
    ):
        op.create_index(f"ix_subagents_{column}", "subagents", [column])


def _ingestion_runs() -> None:
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("ingested_at", sa.String(64), nullable=False),
        sa.Column("conversations_received", sa.Integer(), nullable=False),
        sa.Column("conversations_written", sa.Integer(), nullable=False),
        sa.Column("conversations_skipped", sa.Integer(), nullable=False),
        sa.Column("malformed_records", sa.Integer(), nullable=False),
        sa.Column("duplicate_conversations", sa.Integer(), nullable=False),
    )
    op.create_index("ix_ingestion_runs_provider", "ingestion_runs", ["provider"])
    op.create_index("ix_ingestion_runs_ingested_at", "ingestion_runs", ["ingested_at"])


TABLE_CREATORS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("conversations", _conversation),
    ("turns", _turns),
    ("model_calls", _model_calls),
    ("tool_calls", _tool_calls),
    ("work_items", _work_items),
    ("context_samples", _context_samples),
    ("turn_settings", _turn_settings),
    ("compaction_events", _compaction_events),
    ("subagents", _subagents),
    ("ingestion_runs", _ingestion_runs),
)


def upgrade() -> None:
    context = op.get_context()
    existing = (
        set() if context.as_sql else set(inspect(op.get_bind()).get_table_names())
    )
    for name, create in TABLE_CREATORS:
        if name not in existing:
            create()


def downgrade() -> None:
    raise RuntimeError("The adopted baseline schema cannot be downgraded")
