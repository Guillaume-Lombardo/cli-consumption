from __future__ import annotations

from typing import Any

import pytest

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


@pytest.mark.parametrize(
    "endpoint",
    ("http://collector.test", "ftp://collector.test", "collector.test"),
)
def test_send_snapshot_rejects_insecure_or_invalid_remote_urls(endpoint: str) -> None:
    with pytest.raises(ValueError):
        send_snapshot(Snapshot(provider="codex"), endpoint, "token")


def test_send_snapshot_allows_loopback_or_explicit_trusted_http(monkeypatch) -> None:
    observed: list[str] = []

    class CapabilitiesResponse(FakeResponse):
        def json(self) -> dict[str, int | str]:
            return {"snapshot_schema_min": 1, "snapshot_schema_max": 1}

    def fake_get(url: str, **_kwargs: Any) -> FakeResponse:
        observed.append(url)
        return CapabilitiesResponse()

    def fake_post(url: str, **_kwargs: Any) -> FakeResponse:
        observed.append(url)
        return FakeResponse()

    monkeypatch.setattr("cli_consumption.sync.httpx.get", fake_get)
    monkeypatch.setattr("cli_consumption.sync.httpx.post", fake_post)

    send_snapshot(Snapshot(provider="codex"), "http://127.0.0.1:8765")
    send_snapshot(
        Snapshot(provider="codex"),
        "http://collector.internal",
        allow_insecure=True,
    )

    assert observed == [
        "http://127.0.0.1:8765/api/v1/capabilities",
        "http://127.0.0.1:8765/api/v1/snapshots",
        "http://collector.internal/api/v1/capabilities",
        "http://collector.internal/api/v1/snapshots",
    ]
