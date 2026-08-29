"""Canonicalize stored timestamps for direct indexed comparisons.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

BATCH_SIZE = 1_000
TIMESTAMP_COLUMNS = {
    "conversations": ("started_at", "ended_at"),
    "turns": ("started_at", "ended_at"),
    "model_calls": ("timestamp",),
    "tool_calls": ("timestamp",),
    "context_samples": ("timestamp",),
    "compaction_events": ("timestamp",),
    "ingestion_runs": ("ingested_at",),
}


def _canonical_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise RuntimeError("stored timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("stored timestamp is invalid")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _canonicalize_column(table_name: str, column_name: str) -> None:
    connection = op.get_bind()
    table = sa.table(
        table_name,
        sa.column("id", sa.String()),
        sa.column(column_name, sa.String()),
    )
    last_id: str | None = None
    while True:
        statement = (
            sa.select(table.c.id, table.c[column_name])
            .where(table.c[column_name].is_not(None))
            .order_by(table.c.id)
            .limit(BATCH_SIZE)
        )
        if last_id is not None:
            statement = statement.where(table.c.id > last_id)
        rows = connection.execute(statement).all()
        if not rows:
            return
        updates = []
        for row_id, value in rows:
            canonical = _canonical_timestamp(value)
            if canonical != value:
                updates.append({"_row_id": row_id, "_timestamp": canonical})
        if updates:
            connection.execute(
                sa.update(table)
                .where(table.c.id == sa.bindparam("_row_id"))
                .values({column_name: sa.bindparam("_timestamp")}),
                updates,
            )
        last_id = rows[-1][0]


def upgrade() -> None:
    context = op.get_context()
    if not context.as_sql:
        for table_name, columns in TIMESTAMP_COLUMNS.items():
            for column_name in columns:
                _canonicalize_column(table_name, column_name)
        indexes = {
            item["name"] for item in inspect(op.get_bind()).get_indexes("conversations")
        }
        if "ix_conversations_ended_at" in indexes:
            return
    op.create_index(
        "ix_conversations_ended_at", "conversations", ["ended_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_ended_at", table_name="conversations")
