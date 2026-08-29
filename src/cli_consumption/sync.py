from __future__ import annotations

import httpx

from cli_consumption.models import CURRENT_SNAPSHOT_SCHEMA, Snapshot


def send_snapshot(
    snapshot: Snapshot,
    endpoint: str,
    token: str | None = None,
    timeout: float = 60.0,
) -> dict[str, int | str]:
    payload_to_send = Snapshot.from_dict(snapshot.to_dict()).to_dict()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    capabilities = httpx.get(
        endpoint.rstrip("/") + "/api/v1/capabilities", timeout=timeout
    )
    if capabilities.status_code != 404:
        capabilities.raise_for_status()
        _require_compatible_schema(capabilities.json())
    response = httpx.post(
        endpoint.rstrip("/") + "/api/v1/snapshots",
        json=payload_to_send,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Collector returned an invalid response")
    if not isinstance(payload.get("run_id"), str) or not all(
        isinstance(payload.get(field), int)
        and not isinstance(payload.get(field), bool)
        and payload[field] >= 0
        for field in ("received", "written", "skipped")
    ):
        raise ValueError("Collector returned an invalid response")
    return payload


def _require_compatible_schema(payload: object) -> None:
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
