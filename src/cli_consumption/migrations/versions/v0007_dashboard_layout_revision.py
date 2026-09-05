"""Add bounded optimistic concurrency to dashboard layouts.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dashboard_layouts") as batch:
        batch.add_column(
            sa.Column(
                "revision",
                sa.BigInteger(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch.create_check_constraint(
            "ck_dashboard_layouts_revision",
            "revision >= 1 AND revision <= 9223372036854775807",
        )
    with op.batch_alter_table("dashboard_layouts") as batch:
        batch.alter_column("revision", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("dashboard_layouts") as batch:
        batch.drop_constraint("ck_dashboard_layouts_revision", type_="check")
        batch.drop_column("revision")
