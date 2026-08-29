from __future__ import annotations

from datetime import UTC, datetime


def canonical_timestamp(value: str | datetime) -> str:
    """Return one fixed-width UTC representation for a timezone-aware instant."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid timestamp") from error
    else:
        parsed = value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")
