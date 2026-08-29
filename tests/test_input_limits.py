from pathlib import Path
from typing import Any

import pytest

from cli_consumption.adapters._shared import (
    ProviderDataLimitError,
    ProviderInputBudget,
    check_provider_sqlite_file,
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


def test_jsonl_recounts_bytes_after_a_stale_initial_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "growing.jsonl"
    path.write_bytes(b"1234\n12\n")
    real_stat = Path.stat

    def stale_stat(value: Path, *, follow_symlinks: bool = True) -> Any:
        result = real_stat(value, follow_symlinks=follow_symlinks)
        if value == path and follow_symlinks:
            return type("StaleStat", (), {"st_size": 5})()
        return result

    monkeypatch.setattr(Path, "stat", stale_stat)
    with pytest.raises(ProviderDataLimitError) as error:
        list(iter_bounded_jsonl_bytes(path, maximum_file=6))
    assert str(error.value) == "provider_file_too_large"


def test_candidate_budget_is_generic_and_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_CANDIDATES", 2)
    budget = ProviderInputBudget()
    budget.sorted_paths([tmp_path / "a"])
    with pytest.raises(ProviderDataLimitError) as error:
        budget.sorted_paths([tmp_path / "b", tmp_path / "secret-canary"])
    assert str(error.value) == "provider_candidate_limit_exceeded"
    assert "secret-canary" not in str(error.value)


def test_sqlite_budgets_use_generic_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "secret-canary.sqlite"
    database.write_bytes(b"1234")
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_BYTES", 3)
    with pytest.raises(ProviderDataLimitError) as file_error:
        check_provider_sqlite_file(database)
    assert str(file_error.value) == "provider_sqlite_file_too_large"
    assert "secret-canary" not in str(file_error.value)

    budget = ProviderInputBudget()
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_ROWS", 1)
    with pytest.raises(ProviderDataLimitError) as row_error:
        list(budget.rows([("safe",), ("secret-canary",)]))
    assert str(row_error.value) == "provider_sqlite_row_limit_exceeded"

    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_FIELD_BYTES", 3
    )
    with pytest.raises(ProviderDataLimitError) as field_error:
        budget.json_field("secret-canary")
    assert str(field_error.value) == "provider_sqlite_field_too_large"
    assert "secret-canary" not in str(field_error.value)

    cumulative = ProviderInputBudget()
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_FIELD_BYTES", 10
    )
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_FIELDS_BYTES", 5
    )
    cumulative.json_field("abc")
    with pytest.raises(ProviderDataLimitError) as cumulative_error:
        cumulative.json_field("def")
    assert str(cumulative_error.value) == "provider_sqlite_fields_too_large"


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
