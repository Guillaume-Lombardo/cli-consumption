from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.api import create_app
from cli_consumption.storage import create_database_engine, read_table


@pytest.mark.anyio
async def test_collector_requires_token_and_ingests_snapshot(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    snapshot = CodexAdapter().collect([("laptop", home)])
    engine = create_database_engine(tmp_path / "central.sqlite")
    transport = httpx.ASGITransport(app=create_app(engine, "test-token"))

    async with httpx.AsyncClient(
        transport=transport, base_url="http://collector.test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (
            await client.post("/api/v1/snapshots", json=snapshot.to_dict())
        ).status_code == 401
        response = await client.post(
            "/api/v1/snapshots",
            json=snapshot.to_dict(),
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        assert response.json()["written"] == 1
        assert read_table(engine, "conversations")[0]["source_machine"] == "laptop"

        invalid = snapshot.to_dict()
        invalid["conversations"][0]["prompt"] = "privacy canary"
        response = await client.post(
            "/api/v1/snapshots",
            json=invalid,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 422
        assert "privacy canary" not in response.text
    engine.dispose()
