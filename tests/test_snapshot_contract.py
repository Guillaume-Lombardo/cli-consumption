from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.models import Snapshot, SnapshotValidationError
from cli_consumption.storage import create_database_engine, ingest_snapshot


def test_schema_version_is_emitted_and_absence_is_accepted() -> None:
    snapshot = Snapshot(provider="codex")
    assert snapshot.to_dict()["schema_version"] == 2

    legacy = snapshot.to_dict()
    legacy.pop("schema_version")
    # An unversioned payload is schema 1 and is represented as schema 2 in memory.
    assert Snapshot.from_dict(legacy).schema_version == 2


def _call(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": "codex:one:model:1",
        "conversation_id": "codex:one",
        "turn_id": None,
        "sequence": 1,
        "timestamp": None,
        "model": "model-a",
        "input_tokens": 30,
        "cached_input_tokens": 10,
        "cache_write_input_tokens": 15,
        "uncached_input_tokens": 5,
        "output_tokens": 9,
        "reasoning_output_tokens": 4,
        "visible_output_tokens": 5,
        "unattributed_tokens": 0,
        "total_tokens": 39,
    }
    record.update(overrides)
    return record


def _payload(version: int | None, call: dict[str, object]) -> dict[str, object]:
    payload = Snapshot(provider="codex").to_dict()
    payload["model_calls"] = [call]
    if version is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = version
    return payload


def test_schema_1_payloads_upgrade_without_a_cache_write_duration() -> None:
    for version in (None, 1):
        snapshot = Snapshot.from_dict(_payload(version, _call()))
        assert snapshot.schema_version == 2
        assert snapshot.model_calls[0]["cache_write_1h_input_tokens"] is None
        # The upgraded form is itself a valid current payload.
        assert Snapshot.from_dict(snapshot.to_dict()).to_dict() == snapshot.to_dict()


@pytest.mark.parametrize("value", (None, 0, 15))
def test_schema_1_rejects_the_cache_write_duration_field(value: object) -> None:
    with pytest.raises(SnapshotValidationError, match="invalid_snapshot"):
        Snapshot.from_dict(_payload(1, _call(cache_write_1h_input_tokens=value)))


@pytest.mark.parametrize("value", (None, 0, 7, 15))
def test_schema_2_accepts_a_bounded_or_unreported_cache_write_duration(
    value: int | None,
) -> None:
    snapshot = Snapshot.from_dict(_payload(2, _call(cache_write_1h_input_tokens=value)))
    assert snapshot.model_calls[0]["cache_write_1h_input_tokens"] == value
    assert (
        Snapshot.from_dict(_payload(2, _call())).model_calls[0][
            "cache_write_1h_input_tokens"
        ]
        is None
    )


@pytest.mark.parametrize("value", (16, -1, True, 1.5, "1", 2**63))
def test_schema_2_rejects_an_invalid_cache_write_duration(value: object) -> None:
    with pytest.raises(SnapshotValidationError, match="invalid_snapshot"):
        Snapshot.from_dict(_payload(2, _call(cache_write_1h_input_tokens=value)))


def test_strict_types_and_values_are_rejected_without_echoing_input() -> None:
    for field, value in (
        ("malformed_records", True),
        ("duplicate_conversations", -1),
        ("provider", "privacy canary\nsecret"),
        ("schema_version", 3),
        ("schema_version", 0),
    ):
        payload = Snapshot(provider="codex").to_dict()
        payload[field] = value
        with pytest.raises(SnapshotValidationError) as error:
            Snapshot.from_dict(payload)
        assert str(error.value) == "invalid_snapshot"
        assert "privacy" not in str(error.value)


def test_referential_integrity_and_provider_are_enforced(
    tmp_path: Path, rollout_factory
) -> None:
    home = tmp_path / "codex"
    rollout_factory(home)
    original = CodexAdapter().collect([("machine", home)])
    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        mutations = []

        duplicate = deepcopy(original)
        duplicate.turns.append(dict(duplicate.turns[0]))
        mutations.append(duplicate)

        orphan = deepcopy(original)
        orphan.tool_calls[0]["conversation_id"] = "missing"
        mutations.append(orphan)

        wrong_provider = deepcopy(original)
        wrong_provider.conversations[0]["provider"] = "claude"
        mutations.append(wrong_provider)

        duplicate_model = deepcopy(original)
        duplicate_model.conversations[0]["models"] *= 2
        mutations.append(duplicate_model)

        invalid_tokens = deepcopy(original)
        invalid_tokens.turns[0]["input_tokens"] += 1
        mutations.append(invalid_tokens)

        naive_timestamp = deepcopy(original)
        naive_timestamp.turns[0]["started_at"] = "2026-08-25T10:00:00"
        mutations.append(naive_timestamp)

        for invalid in mutations:
            with pytest.raises(SnapshotValidationError, match="invalid_snapshot"):
                ingest_snapshot(engine, invalid)
    finally:
        engine.dispose()
