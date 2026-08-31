from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from cli_consumption.models import Snapshot
from cli_consumption.sync import (
    IdempotencyUnsupportedError,
    SyncClient,
    snapshot_idempotency_key,
)

RUN_ID = "12345678-1234-4abc-8def-123456789abc"


def response(status: int, payload: object) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://collector.test"),
    )


class FakeClient:
    def __init__(self, *, idempotency: bool | None = False) -> None:
        self.idempotency = idempotency
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_handler: Callable[[str], httpx.Response] | None = None
        self.post_handler: Callable[..., httpx.Response] | None = None

    def get(self, url: str) -> httpx.Response:
        self.get_calls.append(url)
        if self.get_handler is not None:
            return self.get_handler(url)
        payload: dict[str, int | bool] = {
            "snapshot_schema_min": 1,
            "snapshot_schema_max": 1,
        }
        if self.idempotency is not None:
            payload["idempotent_snapshot_uploads"] = self.idempotency
        return response(200, payload)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.post_calls.append((url, kwargs))
        if self.post_handler is not None:
            return self.post_handler(url, **kwargs)
        return response(
            200,
            {"run_id": RUN_ID, "received": 0, "written": 0, "skipped": 0},
        )


def sync_client(
    fake: FakeClient, endpoint: str = "https://collector.test"
) -> SyncClient:
    return SyncClient(endpoint, client=cast(httpx.Client, fake))


def test_sync_client_reuses_http_client_and_negotiates_capabilities_once() -> None:
    fake = FakeClient(idempotency=True)
    client = SyncClient(
        "https://collector.test/",
        "token",
        client=cast(httpx.Client, fake),
    )

    first = client.send_snapshot(Snapshot(provider="codex"))
    second = client.send_snapshot(Snapshot(provider="claude"))

    assert first["run_id"] == second["run_id"] == RUN_ID
    assert fake.get_calls == ["https://collector.test/api/v1/capabilities"]
    assert len(fake.post_calls) == 2
    first_url, first_request = fake.post_calls[0]
    assert first_url == "https://collector.test/api/v1/snapshots"
    assert first_request["headers"]["Authorization"] == "Bearer token"
    assert first_request["json"]["provider"] == "codex"
    assert first_request["json"]["schema_version"] == 1
    keys = [request["headers"]["Idempotency-Key"] for _, request in fake.post_calls]
    assert all(str(uuid.UUID(key)) == key for key in keys)
    assert keys[0] != keys[1]


def test_database_upload_requires_idempotency_before_first_post() -> None:
    fake = FakeClient(idempotency=False)
    client = sync_client(fake)

    with pytest.raises(IdempotencyUnsupportedError):
        client.require_idempotent_uploads()

    assert fake.get_calls == ["https://collector.test/api/v1/capabilities"]
    assert fake.post_calls == []


def test_snapshot_idempotency_key_is_stable_canonical_and_content_bound() -> None:
    first = Snapshot(provider="codex")
    same = Snapshot.from_dict(first.to_dict())
    changed = Snapshot(provider="codex", duplicate_conversations=1)

    first_key = snapshot_idempotency_key(first)

    assert snapshot_idempotency_key(same) == first_key
    assert snapshot_idempotency_key(changed) != first_key
    assert uuid.UUID(first_key).version == 4
    assert "codex" not in first_key


def test_explicit_idempotency_key_is_reused_across_calls_and_validated() -> None:
    fake = FakeClient(idempotency=True)
    client = sync_client(fake)
    snapshot = Snapshot(provider="codex")
    key = snapshot_idempotency_key(snapshot)

    client.require_idempotent_uploads()
    client.send_snapshot(snapshot, idempotency_key=key)
    client.send_snapshot(snapshot, idempotency_key=key)

    assert fake.get_calls == ["https://collector.test/api/v1/capabilities"]
    assert [
        request["headers"]["Idempotency-Key"] for _, request in fake.post_calls
    ] == [key, key]
    for invalid_key in ("", "PROMPT_SECRET_CANARY"):
        with pytest.raises(ValueError, match="Invalid idempotency key"):
            client.send_snapshot(snapshot, idempotency_key=invalid_key)
    assert len(fake.post_calls) == 2


def test_sync_client_rejects_incompatible_collector() -> None:
    fake = FakeClient()

    def incompatible(_url: str) -> httpx.Response:
        return response(200, {"snapshot_schema_min": 2, "snapshot_schema_max": 3})

    fake.get_handler = incompatible

    with pytest.raises(ValueError, match="does not support"):
        sync_client(fake).send_snapshot(Snapshot(provider="codex"))


@pytest.mark.parametrize(
    "endpoint",
    ("http://collector.test", "ftp://collector.test", "collector.test"),
)
def test_sync_client_rejects_insecure_or_invalid_remote_urls(endpoint: str) -> None:
    with pytest.raises(ValueError):
        SyncClient(endpoint, "token", client=cast(httpx.Client, FakeClient()))


def test_sync_client_allows_loopback_or_explicit_trusted_http() -> None:
    loopback = FakeClient()
    trusted = FakeClient()

    sync_client(loopback, "http://127.0.0.1:8765").send_snapshot(
        Snapshot(provider="codex")
    )
    SyncClient(
        "http://collector.internal",
        allow_insecure=True,
        client=cast(httpx.Client, trusted),
    ).send_snapshot(Snapshot(provider="codex"))

    assert loopback.get_calls[0] == "http://127.0.0.1:8765/api/v1/capabilities"
    assert trusted.get_calls[0] == "http://collector.internal/api/v1/capabilities"


def test_ambiguous_timeout_retries_with_the_same_idempotency_key() -> None:
    fake = FakeClient(idempotency=True)
    attempts: list[dict[str, Any]] = []

    def lose_first_response(url: str, **kwargs: Any) -> httpx.Response:
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("response lost")
        return response(
            200,
            {"run_id": RUN_ID, "received": 0, "written": 0, "skipped": 0},
        )

    fake.post_handler = lose_first_response
    sleeps: list[float] = []
    client = SyncClient(
        "https://collector.test",
        client=cast(httpx.Client, fake),
        sleep=sleeps.append,
    )

    result = client.send_snapshot(Snapshot(provider="codex"))

    assert result["run_id"] == RUN_ID
    assert len(attempts) == 2
    assert (
        attempts[0]["headers"]["Idempotency-Key"]
        == attempts[1]["headers"]["Idempotency-Key"]
    )
    assert sleeps == [0.25]


def test_retry_is_bounded_to_three_attempts() -> None:
    fake = FakeClient(idempotency=True)
    attempts = 0

    def always_timeout(_url: str, **_kwargs: Any) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("collector unavailable")

    fake.post_handler = always_timeout
    sleeps: list[float] = []
    client = SyncClient(
        "https://collector.test",
        client=cast(httpx.Client, fake),
        sleep=sleeps.append,
    )

    with pytest.raises(httpx.ConnectTimeout):
        client.send_snapshot(Snapshot(provider="codex"))

    assert attempts == 3
    assert sleeps == [0.25, 0.5]


def test_legacy_collector_is_not_retried_after_ambiguous_failure() -> None:
    fake = FakeClient(idempotency=False)
    attempts = 0

    def timeout(_url: str, **_kwargs: Any) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("response lost")

    fake.post_handler = timeout
    sleeps: list[float] = []
    client = SyncClient(
        "https://collector.test",
        client=cast(httpx.Client, fake),
        sleep=sleeps.append,
    )

    with pytest.raises(httpx.ReadTimeout):
        client.send_snapshot(Snapshot(provider="codex"))

    assert attempts == 1
    assert sleeps == []


@pytest.mark.parametrize("capabilities", ["not-found", "field-absent"])
def test_previous_collector_capabilities_never_enable_ambiguous_retry(
    capabilities: str,
) -> None:
    fake = FakeClient(idempotency=None)
    if capabilities == "not-found":
        fake.get_handler = lambda _url: response(404, {"detail": "not_found"})
    attempts = 0

    def timeout(_url: str, **_kwargs: Any) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("response lost")

    fake.post_handler = timeout
    client = SyncClient(
        "https://collector.test",
        client=cast(httpx.Client, fake),
        sleep=lambda _delay: pytest.fail("legacy collector must not retry"),
    )

    with pytest.raises(httpx.ReadTimeout):
        client.send_snapshot(Snapshot(provider="codex"))

    assert fake.get_calls == ["https://collector.test/api/v1/capabilities"]
    assert attempts == 1


def test_retryable_server_failure_is_retried() -> None:
    fake = FakeClient(idempotency=True)
    attempts = 0

    def unavailable_once(url: str, **kwargs: Any) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return response(503, {"detail": "temporarily_unavailable"})
        return response(
            200,
            {"run_id": RUN_ID, "received": 0, "written": 0, "skipped": 0},
        )

    fake.post_handler = unavailable_once

    result = sync_client(fake).send_snapshot(Snapshot(provider="codex"))

    assert result["run_id"] == RUN_ID
    assert attempts == 2


@pytest.mark.parametrize(
    "payload",
    [
        {
            "run_id": "PROMPT_SECRET_CANARY",
            "received": 0,
            "written": 0,
            "skipped": 0,
        },
        {
            "run_id": RUN_ID.upper(),
            "received": 0,
            "written": 0,
            "skipped": 0,
        },
        {"run_id": RUN_ID, "received": True, "written": 0, "skipped": 0},
    ],
)
def test_sync_client_rejects_unbounded_or_invalid_response_fields(
    payload: dict[str, object],
) -> None:
    fake = FakeClient()
    fake.post_handler = lambda _url, **_kwargs: response(200, payload)

    with pytest.raises(ValueError, match="invalid response"):
        sync_client(fake).send_snapshot(Snapshot(provider="codex"))


def test_sync_client_discards_unknown_response_fields() -> None:
    fake = FakeClient()
    fake.post_handler = lambda _url, **_kwargs: response(
        200,
        {
            "run_id": RUN_ID,
            "received": 1,
            "written": 1,
            "skipped": 0,
            "provider_payload": "PROMPT_SECRET_CANARY",
        },
    )

    assert sync_client(fake).send_snapshot(Snapshot(provider="codex")) == {
        "run_id": RUN_ID,
        "received": 1,
        "written": 1,
        "skipped": 0,
    }
