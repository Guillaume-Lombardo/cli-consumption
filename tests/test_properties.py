from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from cli_consumption.models import (
    MAX_BIGINT,
    MAX_INTEGER,
    ConversationRecord,
    Snapshot,
    SnapshotValidationError,
    TokenRecord,
)
from cli_consumption.reporting import parse_export_window
from cli_consumption.storage import validate_snapshot
from cli_consumption.timestamps import canonical_timestamp

INSTANTS = st.datetimes(
    min_value=datetime(1970, 1, 2),
    max_value=datetime(2099, 12, 30, 23, 59, 59, 999999),
    timezones=st.timezones(),
    allow_imaginary=False,
)


@given(first=INSTANTS, second=INSTANTS)
def test_canonical_timestamps_sort_in_chronological_order(
    first: datetime, second: datetime
) -> None:
    first_canonical = canonical_timestamp(first)
    second_canonical = canonical_timestamp(second)

    assert (first_canonical > second_canonical) - (
        first_canonical < second_canonical
    ) == (first.astimezone(UTC) > second.astimezone(UTC)) - (
        first.astimezone(UTC) < second.astimezone(UTC)
    )
    assert first_canonical.endswith("+00:00")
    assert len(first_canonical) == 32


@given(instant=INSTANTS, offset_minutes=st.integers(min_value=-1439, max_value=1439))
def test_equivalent_offsets_have_one_canonical_timestamp(
    instant: datetime, offset_minutes: int
) -> None:
    # A fixed offset avoids relying on the host timezone while exercising negative and
    # positive offset spellings. Hypothesis' INSTANTS separately covers IANA DST zones.
    fixed_offset = timezone(timedelta(minutes=offset_minutes))
    assert canonical_timestamp(instant.astimezone(fixed_offset)) == canonical_timestamp(
        instant
    )


TOKEN_COMPONENTS = st.tuples(
    st.integers(min_value=0, max_value=MAX_BIGINT // 9),
    st.integers(min_value=0, max_value=MAX_BIGINT // 9),
    st.integers(min_value=0, max_value=MAX_BIGINT // 9),
    st.integers(min_value=0, max_value=MAX_BIGINT // 9),
    st.integers(min_value=0, max_value=MAX_BIGINT // 9),
    st.integers(min_value=0, max_value=MAX_BIGINT // 9),
)


def _tokens(components: tuple[int, int, int, int, int, int]) -> dict[str, int]:
    cached, cache_write, uncached, reasoning, visible, unattributed = components
    input_tokens = cached + cache_write + uncached
    output_tokens = reasoning + visible
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_write,
        "uncached_input_tokens": uncached,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "visible_output_tokens": visible,
        "unattributed_tokens": unattributed,
        "total_tokens": input_tokens + output_tokens + unattributed,
    }


@given(components=TOKEN_COMPONENTS)
def test_token_composition_accepts_exact_additive_components(
    components: tuple[int, int, int, int, int, int],
) -> None:
    assert TokenRecord.model_validate(_tokens(components)).model_dump() == _tokens(
        components
    )


@given(
    components=TOKEN_COMPONENTS,
    total_delta=st.integers(min_value=1, max_value=MAX_BIGINT // 9),
)
def test_token_composition_rejects_any_inconsistent_total(
    components: tuple[int, int, int, int, int, int], total_delta: int
) -> None:
    tokens = _tokens(components)
    tokens["total_tokens"] += total_delta
    with pytest.raises(ValidationError):
        TokenRecord.model_validate(tokens)


@given(
    field=st.sampled_from(tuple(TokenRecord.model_fields)),
    invalid=st.one_of(
        st.integers(max_value=-1),
        st.integers(min_value=MAX_BIGINT + 1),
        st.booleans(),
    ),
)
def test_token_fields_enforce_strict_bigint_bounds(field: str, invalid: int) -> None:
    tokens = _tokens((0, 0, 0, 0, 0, 0))
    tokens[field] = invalid
    with pytest.raises(ValidationError):
        TokenRecord.model_validate(tokens)


@given(
    instant=INSTANTS,
    before=st.timedeltas(
        min_value=timedelta(microseconds=1), max_value=timedelta(days=30)
    ),
    after=st.timedeltas(
        min_value=timedelta(microseconds=1), max_value=timedelta(days=30)
    ),
)
def test_snapshot_time_ranges_accept_order_and_reject_reversal(
    instant: datetime, before: timedelta, after: timedelta
) -> None:
    start = instant.astimezone(UTC) - before
    end = instant.astimezone(UTC) + after
    record = {
        "id": "codex:property",
        "provider": "codex",
        "external_id": "property",
        "source_machine": "synthetic",
        "project": "synthetic",
        "project_source": "none",
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "duration_seconds": None,
        "source": "synthetic",
        "models": [],
        "iterations": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "compactions": 0,
        "event_count": MAX_INTEGER,
        "content_hash": "0" * 64,
        **_tokens((0, 0, 0, 0, 0, 0)),
    }
    assert validate_snapshot(Snapshot(provider="codex", conversations=[record]))

    record["started_at"], record["ended_at"] = record["ended_at"], record["started_at"]
    with pytest.raises(SnapshotValidationError, match="invalid_snapshot"):
        validate_snapshot(Snapshot(provider="codex", conversations=[record]))


@given(day=st.dates(min_value=date(1970, 1, 1), max_value=date(2099, 12, 30)))
def test_date_export_windows_are_exactly_half_open_utc_days(day: date) -> None:
    window = parse_export_window(day.isoformat(), day.isoformat())
    # Equal date arguments mean [start-of-day, start-of-next-day), not an empty range.
    assert window.since == datetime.combine(day, datetime.min.time(), UTC)
    assert window.since is not None
    assert window.until is not None
    assert window.until == window.since + timedelta(days=1)


@given(bound=INSTANTS)
def test_timestamp_export_bounds_preserve_exact_half_open_instant(
    bound: datetime,
) -> None:
    canonical = canonical_timestamp(bound)
    window = parse_export_window(
        canonical, canonical_timestamp(bound + timedelta(microseconds=1))
    )
    assert window.since is not None
    assert window.until is not None
    assert canonical_timestamp(window.since) == canonical
    assert window.until - window.since == timedelta(microseconds=1)


def test_conversation_record_accepts_documented_integer_boundaries() -> None:
    record = ConversationRecord.model_validate(
        {
            "id": "codex:boundary",
            "provider": "codex",
            "external_id": "boundary",
            "source_machine": "synthetic",
            "project": "synthetic",
            "project_source": "none",
            "started_at": None,
            "ended_at": None,
            "duration_seconds": None,
            "source": "synthetic",
            "models": [],
            "iterations": MAX_INTEGER,
            "model_calls": MAX_INTEGER,
            "tool_calls": MAX_INTEGER,
            "compactions": MAX_INTEGER,
            "event_count": MAX_INTEGER,
            "content_hash": "0" * 64,
            **_tokens(
                (
                    MAX_BIGINT // 9,
                    MAX_BIGINT // 9,
                    MAX_BIGINT // 9,
                    MAX_BIGINT // 9,
                    MAX_BIGINT // 9,
                    MAX_BIGINT // 9,
                )
            ),
        }
    )
    assert record.event_count == MAX_INTEGER
