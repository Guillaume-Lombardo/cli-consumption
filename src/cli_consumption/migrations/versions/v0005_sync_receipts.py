"""Persist opaque sync receipts for idempotent upload replay.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_receipts",
        sa.Column("idempotency_key", sa.String(36), primary_key=True),
        sa.Column(
            "ingestion_run_id",
            sa.String(36),
            sa.ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_sync_receipts_ingestion_run_id",
        "sync_receipts",
        ["ingestion_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sync_receipts_ingestion_run_id", table_name="sync_receipts")
    op.drop_table("sync_receipts")
