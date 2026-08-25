from __future__ import annotations

from typing import Any

from cli_consumption.models import Snapshot
from cli_consumption.sync import send_snapshot


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, int | str]:
        return {"run_id": "run-1", "received": 0, "written": 0, "skipped": 0}


def test_send_snapshot_posts_to_versioned_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        observed.update(url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr("cli_consumption.sync.httpx.post", fake_post)

    result = send_snapshot(
        Snapshot(provider="codex"), "https://collector.test/", "token"
    )

    assert result["run_id"] == "run-1"
    assert observed["url"] == "https://collector.test/api/v1/snapshots"
    assert observed["headers"] == {"Authorization": "Bearer token"}
    assert observed["json"]["provider"] == "codex"
