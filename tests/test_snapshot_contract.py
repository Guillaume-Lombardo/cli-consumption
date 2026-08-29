from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.models import Snapshot, SnapshotValidationError
from cli_consumption.storage import create_database_engine, ingest_snapshot


def test_schema_version_is_emitted_and_absence_is_accepted() -> None:
    snapshot = Snapshot(provider="codex")
    assert snapshot.to_dict()["schema_version"] == 1

    legacy = snapshot.to_dict()
    legacy.pop("schema_version")
    assert Snapshot.from_dict(legacy).schema_version == 1


def test_strict_types_and_values_are_rejected_without_echoing_input() -> None:
    for field, value in (
        ("malformed_records", True),
        ("duplicate_conversations", -1),
        ("provider", "privacy canary\nsecret"),
        ("schema_version", 2),
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
