from __future__ import annotations

import tomllib
from pathlib import Path


def test_python_support_and_optional_dependencies_are_declared() -> None:
    project_root = Path(__file__).parents[1]
    configuration = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = configuration["project"]

    assert project["requires-python"] == ">=3.12"
    assert project["dependencies"] == [
        "alembic>=1.14",
        "pydantic>=2.10",
        "sqlalchemy>=2.0",
        "typer>=0.15",
    ]
    assert project["optional-dependencies"] == {
        "postgres": ["psycopg[binary]>=3.2"],
        "server": ["fastapi>=0.115", "uvicorn>=0.34"],
        "sync": ["httpx>=0.27"],
    }
    assert configuration["tool"]["ruff"]["target-version"] == "py312"
