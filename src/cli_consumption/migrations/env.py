from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise RuntimeError("A database connection is required for schema migrations")
    context.configure(
        connection=connection,
        target_metadata=None,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
