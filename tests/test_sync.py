from __future__ import annotations

from typing import Any

from cli_consumption.models import Snapshot
from cli_consumption.sync import send_snapshot


class FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, int | str]:
        return {"run_id": "run-1", "received": 0, "written": 0, "skipped": 0}


def test_send_snapshot_posts_to_versioned_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        observed.update(url=url, **kwargs)
        return FakeResponse()

    class CapabilitiesResponse(FakeResponse):
        def json(self) -> dict[str, int | str]:
            return {"snapshot_schema_min": 1, "snapshot_schema_max": 1}

    monkeypatch.setattr("cli_consumption.sync.httpx.post", fake_post)
    monkeypatch.setattr(
        "cli_consumption.sync.httpx.get", lambda *args, **kwargs: CapabilitiesResponse()
    )

    result = send_snapshot(
        Snapshot(provider="codex"), "https://collector.test/", "token"
    )

    assert result["run_id"] == "run-1"
    assert observed["url"] == "https://collector.test/api/v1/snapshots"
    assert observed["headers"] == {"Authorization": "Bearer token"}
    assert observed["json"]["provider"] == "codex"
    assert observed["json"]["schema_version"] == 1


def test_send_snapshot_rejects_incompatible_collector(monkeypatch) -> None:
    class IncompatibleResponse(FakeResponse):
        def json(self) -> dict[str, int | str]:
            return {"snapshot_schema_min": 2, "snapshot_schema_max": 3}

    monkeypatch.setattr(
        "cli_consumption.sync.httpx.get", lambda *args, **kwargs: IncompatibleResponse()
    )

    try:
        send_snapshot(Snapshot(provider="codex"), "https://collector.test")
    except ValueError as error:
        assert str(error) == "Collector does not support this snapshot schema"
    else:
        raise AssertionError("incompatible schema was accepted")
