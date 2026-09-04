"""Persist the singleton dashboard presentation layout.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_layouts",
        sa.Column("owner_key", sa.String(32), primary_key=True),
        sa.Column("layout_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    # Layout preferences are presentation-only and may be safely discarded.
    op.drop_table("dashboard_layouts")
