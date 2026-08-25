from __future__ import annotations

import httpx

from cli_consumption.models import Snapshot


def send_snapshot(
    snapshot: Snapshot,
    endpoint: str,
    token: str | None = None,
    timeout: float = 60.0,
) -> dict[str, int | str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.post(
        endpoint.rstrip("/") + "/api/v1/snapshots",
        json=snapshot.to_dict(),
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Collector returned an invalid response")
    return payload
