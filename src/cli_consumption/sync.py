from __future__ import annotations

import hashlib
import ipaddress
import json
import time
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from cli_consumption.models import CURRENT_SNAPSHOT_SCHEMA, Snapshot

MAX_UPLOAD_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (0.25, 0.5)
RETRYABLE_STATUS_CODES = frozenset({500, 502, 503, 504})


class SyncClient:
    """Reusable collector client with one capability negotiation per endpoint."""

    def __init__(
        self,
        endpoint: str,
        token: str | None = None,
        timeout: float = 60.0,
        *,
        allow_insecure: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        _require_secure_endpoint(endpoint, allow_insecure=allow_insecure)
        self._endpoint = endpoint.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._sleep = sleep
        self._capabilities_checked = False
        self._supports_idempotency = False

    def __enter__(self) -> SyncClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def require_idempotent_uploads(self) -> None:
        """Fail before upload when replay receipts are unavailable."""
        self._negotiate_capabilities()
        if not self._supports_idempotency:
            raise IdempotencyUnsupportedError

    def send_snapshot(
        self,
        snapshot: Snapshot,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, int | str]:
        payload_to_send = Snapshot.from_dict(snapshot.to_dict()).to_dict()
        self._negotiate_capabilities()
        idempotency_key = _canonical_idempotency_key(
            str(uuid.uuid4()) if idempotency_key is None else idempotency_key
        )
        headers = {**self._headers, "Idempotency-Key": idempotency_key}
        attempts = MAX_UPLOAD_ATTEMPTS if self._supports_idempotency else 1

        for attempt in range(attempts):
            try:
                response = self._client.post(
                    self._endpoint + "/api/v1/snapshots",
                    json=payload_to_send,
                    headers=headers,
                )
            except httpx.TransportError:
                if attempt + 1 == attempts:
                    raise
                self._sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt + 1 < attempts
            ):
                self._sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            response.raise_for_status()
            return _validated_response(response.json())

        raise RuntimeError("Upload retry loop exhausted")  # pragma: no cover

    def _negotiate_capabilities(self) -> None:
        if self._capabilities_checked:
            return
        response = self._client.get(self._endpoint + "/api/v1/capabilities")
        if response.status_code == 404:
            self._capabilities_checked = True
            return
        response.raise_for_status()
        self._supports_idempotency = _require_compatible_schema(response.json())
        self._capabilities_checked = True


class IdempotencyUnsupportedError(RuntimeError):
    """The collector cannot safely replay a database upload."""


def snapshot_idempotency_key(snapshot: Snapshot) -> str:
    """Return a deterministic canonical UUIDv4 for one logical snapshot."""
    payload = Snapshot.from_dict(snapshot.to_dict()).to_dict()
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(b"cli-consumption:upload-db:v1\0" + canonical).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def send_snapshot(
    snapshot: Snapshot,
    endpoint: str,
    token: str | None = None,
    timeout: float = 60.0,
    *,
    allow_insecure: bool = False,
) -> dict[str, int | str]:
    with SyncClient(
        endpoint,
        token,
        timeout,
        allow_insecure=allow_insecure,
    ) as client:
        return client.send_snapshot(snapshot)


def _validated_response(payload: object) -> dict[str, int | str]:
    if not isinstance(payload, dict):
        raise ValueError("Collector returned an invalid response")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not all(
        isinstance(payload.get(field), int)
        and not isinstance(payload.get(field), bool)
        and payload[field] >= 0
        for field in ("received", "written", "skipped")
    ):
        raise ValueError("Collector returned an invalid response")
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError
    except ValueError:
        raise ValueError("Collector returned an invalid response") from None
    return {
        "run_id": run_id,
        "received": payload["received"],
        "written": payload["written"],
        "skipped": payload["skipped"],
    }


def _canonical_idempotency_key(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        raise ValueError("Invalid idempotency key") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Invalid idempotency key")
    return value


def _require_secure_endpoint(endpoint: str, *, allow_insecure: bool) -> None:
    parsed = urlsplit(endpoint)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Collector URL must not contain credentials")
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ValueError("Collector endpoint must be an HTTP(S) URL")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if not loopback and not allow_insecure:
        raise ValueError(
            "Remote collector requires HTTPS; pass --allow-insecure explicitly "
            "only on a trusted network"
        )


def _require_compatible_schema(payload: object) -> bool:
    if not isinstance(payload, dict):
        raise ValueError("Collector returned invalid capabilities")
    minimum = payload.get("snapshot_schema_min")
    maximum = payload.get("snapshot_schema_max")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
    ):
        raise ValueError("Collector returned invalid capabilities")
    if not minimum <= CURRENT_SNAPSHOT_SCHEMA <= maximum:
        raise ValueError("Collector does not support this snapshot schema")
    idempotency = payload.get("idempotent_snapshot_uploads", False)
    if not isinstance(idempotency, bool):
        raise ValueError("Collector returned invalid capabilities")
    return idempotency
