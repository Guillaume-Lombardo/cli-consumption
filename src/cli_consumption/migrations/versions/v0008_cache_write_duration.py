"""Record the one-hour share of cache-write input tokens per model call.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing rows keep null: their providers' duration split was never recorded,
    # so no split is fabricated for them.
    with op.batch_alter_table("model_calls") as batch:
        batch.add_column(
            sa.Column("cache_write_1h_input_tokens", sa.BigInteger(), nullable=True)
        )


def downgrade() -> None:
    # The cache-write total stays in cache_write_input_tokens; only its duration
    # split is discarded.
    with op.batch_alter_table("model_calls") as batch:
        batch.drop_column("cache_write_1h_input_tokens")
