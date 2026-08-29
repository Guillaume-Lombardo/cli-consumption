from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cli_consumption.timestamps import canonical_timestamp


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.000000+00:00"),
        ("2026-01-01T02:00:00+02:00", "2026-01-01T00:00:00.000000+00:00"),
        ("2025-12-31T16:00:00-08:00", "2026-01-01T00:00:00.000000+00:00"),
        (
            "2026-01-01T00:00:00.000001+00:00",
            "2026-01-01T00:00:00.000001+00:00",
        ),
    ),
)
def test_equivalent_offsets_have_one_fixed_width_representation(
    value: str, expected: str
) -> None:
    assert canonical_timestamp(value) == expected


def test_lexical_and_chronological_order_match_across_offsets_and_dst() -> None:
    paris = ZoneInfo("Europe/Paris")
    instants = [
        datetime(2026, 3, 29, 0, 59, 59, 999999, tzinfo=UTC),
        datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 0, 59, 59, 999999, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC),
    ]
    representations = [
        instants[0].astimezone(paris),
        instants[1].astimezone(timezone(timedelta(hours=-7))),
        instants[2].astimezone(timezone(timedelta(hours=9, minutes=30))),
        instants[3].astimezone(paris),
    ]
    canonical = [canonical_timestamp(value) for value in representations]

    assert canonical == sorted(canonical)
    assert [datetime.fromisoformat(value) for value in canonical] == instants


@pytest.mark.parametrize(
    "value", ("not-a-timestamp", "2026-01-01T00:00:00", datetime(2026, 1, 1))
)
def test_invalid_or_naive_values_are_rejected_without_echo(
    value: str | datetime,
) -> None:
    with pytest.raises(ValueError) as error:
        canonical_timestamp(value)
    assert str(error.value) in {
        "invalid timestamp",
        "timestamp must include a timezone",
    }
    assert "not-a-timestamp" not in str(error.value)
