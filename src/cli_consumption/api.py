from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from cli_consumption import __version__
from cli_consumption.models import Snapshot
from cli_consumption.storage import ingest_snapshot, initialize_database


class SnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    conversations: list[dict[str, Any]] = Field(default_factory=list)
    turns: list[dict[str, Any]] = Field(default_factory=list)
    model_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    subagents: list[dict[str, Any]] = Field(default_factory=list)
    malformed_records: int = 0
    duplicate_conversations: int = 0


def create_app(engine: Engine, api_token: str | None = None) -> FastAPI:
    initialize_database(engine)
    app = FastAPI(title="CLI Consumption collector", version=__version__)

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if api_token is None:
            return
        expected = f"Bearer {api_token}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/api/v1/snapshots", dependencies=[Depends(authorize)])
    def receive_snapshot(payload: SnapshotPayload) -> dict[str, int | str]:
        try:
            result = ingest_snapshot(engine, Snapshot.from_dict(payload.model_dump()))
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        return {
            "run_id": result.run_id,
            "received": result.received,
            "written": result.written,
            "skipped": result.skipped,
        }

    return app
