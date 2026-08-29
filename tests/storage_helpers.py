from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from cli_consumption.storage import TABLES, initialize_database


def read_table(engine: Engine, table_name: str) -> list[dict[str, Any]]:
    """Return a stable, dictionary-shaped view of one exported storage table."""
    initialize_database(engine)
    model = TABLES.get(table_name)
    if model is None:
        raise ValueError(f"Unknown table: {table_name}")
    with Session(engine) as session:
        rows = (
            session.execute(select(model).order_by(*model.__table__.primary_key))
            .scalars()
            .all()
        )
        return [
            {
                column.name: getattr(row, column.name)
                for column in model.__table__.columns
            }
            for row in rows
        ]
