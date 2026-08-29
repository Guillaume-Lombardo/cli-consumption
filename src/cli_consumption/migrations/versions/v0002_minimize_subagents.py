"""Minimize and normalize persisted subagent metadata.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE subagents
            SET status = CASE
                WHEN lower(status) IN (
                    'completed', 'done', 'finished', 'success', 'succeeded'
                ) THEN 'completed'
                WHEN lower(status) IN ('failed', 'error', 'errored') THEN 'failed'
                WHEN lower(status) IN (
                    'aborted', 'canceled', 'cancelled', 'killed', 'terminated'
                ) THEN 'aborted'
                WHEN lower(status) IN (
                    'active', 'in-progress', 'in_progress', 'pending', 'queued',
                    'running', 'starting'
                ) THEN 'in-progress'
                ELSE 'unknown'
            END,
            agent_role = CASE
                WHEN agent_role IS NULL OR trim(agent_role) = '' THEN 'unspecified'
                WHEN lower(agent_role) IN ('worker', 'general') THEN 'worker'
                WHEN lower(agent_role) IN (
                    'explore', 'explorer', 'research', 'researcher'
                ) THEN 'research'
                WHEN lower(agent_role) IN ('review', 'reviewer') THEN 'review'
                WHEN lower(agent_role) IN ('test', 'tester') THEN 'test'
                WHEN lower(agent_role) IN ('planner', 'planning') THEN 'planning'
                WHEN lower(agent_role) IN (
                    'other', 'unspecified', 'worker', 'research', 'review', 'test',
                    'planning'
                ) THEN lower(agent_role)
                ELSE 'other'
            END
            """
        )
    )
    context = op.get_context()
    columns = (
        {"agent_nickname"}
        if context.as_sql
        else {
            column["name"] for column in inspect(op.get_bind()).get_columns("subagents")
        }
    )
    if "agent_nickname" in columns:
        with op.batch_alter_table("subagents") as batch:
            batch.drop_column("agent_nickname")


def downgrade() -> None:
    with op.batch_alter_table("subagents") as batch:
        batch.add_column(
            sa.Column(
                "agent_nickname",
                sa.String(255),
                nullable=False,
                server_default="",
            )
        )
