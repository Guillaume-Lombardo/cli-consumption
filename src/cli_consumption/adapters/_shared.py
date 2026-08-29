from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cli_consumption.models import empty_tokens

MAX_BIGINT = 9_223_372_036_854_775_807
MAX_PROVIDER_JSON_BYTES = 64 * 1024 * 1024
MAX_PROVIDER_JSONL_BYTES = 256 * 1024 * 1024
MAX_PROVIDER_JSONL_LINE_BYTES = 8 * 1024 * 1024
SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]*")


class ProviderDataLimitError(ValueError):
    """A privacy-safe failure raised when provider input exceeds a hard limit."""


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def label(value: object, maximum: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= maximum and SAFE_LABEL.fullmatch(value) else None


def counter(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return min(MAX_BIGINT, max(0, int(value)))


def bounded_sum(*values: int) -> int:
    return min(MAX_BIGINT, sum(values))


def timestamp(value: object) -> datetime | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float) and math.isfinite(value):
            scale = 1000 if abs(value) > 10_000_000_000 else 1
            return datetime.fromtimestamp(value / scale, UTC)
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (OSError, OverflowError, ValueError):
        pass
    return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def tokens(
    *,
    uncached: object = 0,
    cached: object = 0,
    cache_write: object = 0,
    visible: object = 0,
    reasoning: object = 0,
    total: object = 0,
) -> dict[str, int]:
    uncached_n = counter(uncached)
    cached_n = counter(cached)
    write_n = counter(cache_write)
    visible_n = counter(visible)
    reasoning_n = counter(reasoning)
    input_n = bounded_sum(uncached_n, cached_n, write_n)
    output_n = bounded_sum(visible_n, reasoning_n)
    attributed = bounded_sum(input_n, output_n)
    total_n = max(attributed, counter(total))
    return {
        "input_tokens": input_n,
        "cached_input_tokens": cached_n,
        "cache_write_input_tokens": write_n,
        "output_tokens": output_n,
        "reasoning_output_tokens": reasoning_n,
        "total_tokens": total_n,
        "uncached_input_tokens": uncached_n,
        "visible_output_tokens": visible_n,
        "unattributed_tokens": max(0, total_n - attributed),
    }


def add_tokens(target: dict[str, Any], value: dict[str, int]) -> None:
    for field, amount in value.items():
        target[field] = bounded_sum(int(target[field]), amount)


def new_turn(
    conversation_id: str, external_id: str, started: datetime | None
) -> dict[str, Any]:
    return {
        "id": f"{conversation_id}:{external_id}",
        "conversation_id": conversation_id,
        "external_id": external_id,
        "started_at": iso(started),
        "ended_at": None,
        "status": "in-progress",
        "duration_ms": None,
        "time_to_first_token_ms": None,
        "model_calls": 0,
        "tool_calls": 0,
        **empty_tokens(),
    }


def finish_turn(
    turn: dict[str, Any], ended: datetime | None, status: str = "completed"
) -> None:
    turn["ended_at"] = turn["ended_at"] or iso(ended)
    start = timestamp(turn["started_at"])
    end = timestamp(turn["ended_at"])
    if start and end:
        turn["duration_ms"] = max(0, int((end - start).total_seconds() * 1000))
    turn["status"] = status


def project(directory: str | None, mappings: list[tuple[str, str]]) -> tuple[str, str]:
    if directory:
        normalized = directory.replace("\\", "/").rstrip("/")
        for name, prefix in sorted(
            mappings, key=lambda item: len(item[1]), reverse=True
        ):
            prefix = prefix.replace("\\", "/").rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return name, "mapping"
    return "outside-project", "none"


def digest_records(records: object) -> str:
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def read_json(path: Path) -> object:
    return json.loads(read_bounded_bytes(path))


def read_bounded_bytes(path: Path, maximum: int = MAX_PROVIDER_JSON_BYTES) -> bytes:
    reject_provider_file_symlink(path)
    if path.stat().st_size > maximum:
        raise ProviderDataLimitError("provider_file_too_large")
    with path.open("rb") as handle:
        payload = handle.read(maximum + 1)
    if len(payload) > maximum:
        raise ProviderDataLimitError("provider_file_too_large")
    return payload


def iter_bounded_jsonl_bytes(
    path: Path,
    maximum_line: int = MAX_PROVIDER_JSONL_LINE_BYTES,
    maximum_file: int = MAX_PROVIDER_JSONL_BYTES,
) -> Iterator[bytes]:
    reject_provider_file_symlink(path)
    if path.stat().st_size > maximum_file:
        raise ProviderDataLimitError("provider_file_too_large")
    with path.open("rb") as handle:
        for line in handle:
            if len(line) > maximum_line:
                raise ProviderDataLimitError("provider_line_too_large")
            yield line


def reject_provider_file_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ProviderDataLimitError("provider_file_symlink_not_allowed")
