from pathlib import Path

import pytest

from cli_consumption.adapters._shared import (
    ProviderDataLimitError,
    iter_bounded_jsonl_bytes,
    read_bounded_bytes,
)
from cli_consumption.models import Snapshot, SnapshotValidationError


def test_monolithic_provider_files_are_read_with_a_hard_limit(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_bytes(b"12345")

    assert read_bounded_bytes(path, maximum=5) == b"12345"
    with pytest.raises(ProviderDataLimitError, match="provider_file_too_large"):
        read_bounded_bytes(path, maximum=4)


def test_jsonl_lines_are_read_with_a_hard_limit(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"1234\n12\n")

    assert list(iter_bounded_jsonl_bytes(path, maximum_line=5)) == [b"1234\n", b"12\n"]
    with pytest.raises(ProviderDataLimitError, match="provider_line_too_large"):
        list(iter_bounded_jsonl_bytes(path, maximum_line=4))
    with pytest.raises(ProviderDataLimitError, match="provider_file_too_large"):
        list(iter_bounded_jsonl_bytes(path, maximum_file=6))


def test_provider_file_symlinks_are_rejected_without_exposing_the_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "private-canary.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "session.json"
    link.symlink_to(target)

    with pytest.raises(ProviderDataLimitError) as error:
        read_bounded_bytes(link)

    assert str(error.value) == "provider_file_symlink_not_allowed"
    assert "private-canary" not in str(error.value)


def test_snapshot_record_limit_is_enforced_while_adapters_build_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cli_consumption.models.MAX_SNAPSHOT_RECORDS", 2)
    snapshot = Snapshot(provider="codex")

    snapshot.conversations.append({"id": "one"})
    snapshot.turns.append({"id": "two"})
    with pytest.raises(SnapshotValidationError, match="snapshot_too_large"):
        snapshot.model_calls.append({"id": "three"})
