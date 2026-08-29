"""Add internal state for serialized subagent-scope replacement.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subagent_scopes",
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("source_machine", sa.String(255), primary_key=True),
        sa.Column("lock_version", sa.BigInteger(), nullable=False),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO subagent_scopes (provider, source_machine, lock_version)
            SELECT provider, source_machine, 0 FROM conversations
            UNION
            SELECT provider, source_machine, 0 FROM subagents
            """
        )
    )


def downgrade() -> None:
    op.drop_table("subagent_scopes")
