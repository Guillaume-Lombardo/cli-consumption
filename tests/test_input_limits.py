import sqlite3
from contextlib import nullcontext
from pathlib import Path

import pytest

from cli_consumption.adapters._shared import (
    ProviderDataLimitError,
    ProviderInputBudget,
    ensure_provider_sqlite_fields,
    iter_bounded_jsonl_bytes,
    open_provider_sqlite,
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


def test_jsonl_counts_growth_and_the_final_line(tmp_path: Path) -> None:
    path = tmp_path / "growing.jsonl"
    path.write_bytes(b"1\n")
    lines = iter_bounded_jsonl_bytes(path, maximum_file=6)
    assert next(lines) == b"1\n"
    with path.open("ab") as handle:
        handle.write(b"23456")
    with pytest.raises(ProviderDataLimitError) as error:
        next(lines)
    assert str(error.value) == "provider_file_too_large"

    exact = tmp_path / "exact.jsonl"
    exact.write_bytes(b"1234")
    assert list(iter_bounded_jsonl_bytes(exact, maximum_line=4, maximum_file=4)) == [
        b"1234"
    ]


def test_provider_reads_are_bounded_by_the_remaining_collection_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.read_sizes: list[int] = []
            self.readline_sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            return b"x" * size

        def readline(self, size: int) -> bytes:
            self.readline_sizes.append(size)
            return b"x" * size

    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_READ_BYTES", 10)

    stream = RecordingStream()
    monkeypatch.setattr(
        "cli_consumption.adapters._shared._open_provider_file",
        lambda _path: nullcontext(stream),
    )
    budget = ProviderInputBudget()
    budget.file_data(7)
    with pytest.raises(ProviderDataLimitError, match="provider_read_limit_exceeded"):
        read_bounded_bytes(tmp_path / "ignored", budget, maximum=100)
    assert stream.read_sizes == [4]

    stream = RecordingStream()
    monkeypatch.setattr(
        "cli_consumption.adapters._shared._open_provider_file",
        lambda _path: nullcontext(stream),
    )
    budget = ProviderInputBudget()
    budget.file_data(7)
    with pytest.raises(ProviderDataLimitError, match="provider_read_limit_exceeded"):
        next(
            iter_bounded_jsonl_bytes(
                tmp_path / "ignored", budget, maximum_line=100, maximum_file=100
            )
        )
    assert stream.readline_sizes == [4]


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


def test_file_byte_budget_is_shared_across_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_bytes(b"123")
    second.write_bytes(b"456")
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_READ_BYTES", 5)
    budget = ProviderInputBudget()
    assert read_bounded_bytes(first, budget) == b"123"
    with pytest.raises(ProviderDataLimitError) as error:
        read_bounded_bytes(second, budget)
    assert str(error.value) == "provider_read_limit_exceeded"


def test_sqlite_budgets_use_generic_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "secret-canary.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE values_table (payload TEXT)")
    connection.commit()
    connection.close()
    monkeypatch.setattr("cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_BYTES", 3)
    budget = ProviderInputBudget()
    with (
        pytest.raises(ProviderDataLimitError) as file_error,
        open_provider_sqlite(database, budget),
    ):
        pass
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


def test_sqlite_limit_rejects_large_fields_and_closes_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "large.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("CREATE TABLE records (payload TEXT)")
    writer.execute("INSERT INTO records VALUES (?)", ("x" * 2048,))
    writer.commit()
    writer.close()
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_FIELD_BYTES", 1024
    )
    with (
        pytest.raises(ProviderDataLimitError) as error,
        open_provider_sqlite(database, ProviderInputBudget()) as connection,
    ):
        ensure_provider_sqlite_fields(connection, [("records", "payload")])
    assert str(error.value) == "provider_sqlite_field_too_large"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_sqlite_field_limit_applies_to_each_field_not_the_whole_row(
    tmp_path: Path,
) -> None:
    database = tmp_path / "two-fields.sqlite"
    value = "x" * (5 * 1024 * 1024)
    writer = sqlite3.connect(database)
    writer.execute("CREATE TABLE records (first TEXT, second TEXT)")
    writer.execute("INSERT INTO records VALUES (?, ?)", (value, value))
    writer.commit()
    writer.close()

    budget = ProviderInputBudget()
    with open_provider_sqlite(database, budget) as connection:
        ensure_provider_sqlite_fields(
            connection, [("records", "first"), ("records", "second")]
        )
        row = connection.execute("SELECT first, second FROM records").fetchone()
        assert row is not None
        assert len(budget.json_field(row[0])) == 5 * 1024 * 1024
        assert len(budget.json_field(row[1])) == 5 * 1024 * 1024


def test_sqlite_budget_includes_live_wal_and_shm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "wal.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE records (payload TEXT)")
    writer.execute("INSERT INTO records VALUES ('safe')")
    writer.commit()
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    assert wal.is_file() and shm.is_file()
    total = database.stat().st_size + wal.stat().st_size + shm.stat().st_size
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_BYTES", total - 1
    )
    with (
        pytest.raises(ProviderDataLimitError, match="provider_sqlite_file_too_large"),
        open_provider_sqlite(database, ProviderInputBudget()),
    ):
        pass
    writer.close()


def test_sqlite_budget_charges_sidecar_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "growing-wal.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE records (payload TEXT)")
    writer.commit()
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    total = database.stat().st_size + wal.stat().st_size + shm.stat().st_size
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_BYTES", total + 1
    )
    with (
        pytest.raises(ProviderDataLimitError, match="provider_sqlite_file_too_large"),
        open_provider_sqlite(database, ProviderInputBudget()),
        wal.open("ab") as handle,
    ):
        handle.write(b"growth")
    writer.close()


def test_sqlite_budget_includes_live_rollback_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "journal.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("CREATE TABLE records (payload TEXT)")
    writer.execute("INSERT INTO records VALUES ('before')")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE records SET payload = 'after'")
    journal = Path(f"{database}-journal")
    assert journal.is_file()
    total = database.stat().st_size + journal.stat().st_size
    monkeypatch.setattr(
        "cli_consumption.adapters._shared.MAX_PROVIDER_SQLITE_BYTES", total - 1
    )
    with (
        pytest.raises(ProviderDataLimitError, match="provider_sqlite_file_too_large"),
        open_provider_sqlite(database, ProviderInputBudget()),
    ):
        pass
    writer.rollback()
    writer.close()


def test_sqlite_detects_symlink_substitution_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "source.sqlite"
    replacement = tmp_path / "private-canary.sqlite"
    for path in (database, replacement):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE records (payload TEXT)")
        connection.commit()
        connection.close()
    real_connect = sqlite3.connect

    def substitute(database_arg: str, *, uri: bool = False) -> sqlite3.Connection:
        original = tmp_path / "held.sqlite"
        database.rename(original)
        database.symlink_to(replacement)
        return real_connect(database_arg, uri=uri)

    monkeypatch.setattr("cli_consumption.adapters._shared.sqlite3.connect", substitute)
    with (
        pytest.raises(ProviderDataLimitError) as error,
        open_provider_sqlite(database, ProviderInputBudget()),
    ):
        pass
    assert str(error.value) == "provider_sqlite_source_changed"
    assert "private-canary" not in str(error.value)


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


def test_open_descriptor_is_not_redirected_by_symlink_substitution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    replacement = tmp_path / "private-canary.jsonl"
    path.write_bytes(b"safe-1\nsafe-2\n")
    replacement.write_bytes(b"secret\n")
    lines = iter_bounded_jsonl_bytes(path)
    assert next(lines) == b"safe-1\n"
    original = tmp_path / "original.jsonl"
    path.rename(original)
    path.symlink_to(replacement)
    assert list(lines) == [b"safe-2\n"]


def test_snapshot_record_limit_is_enforced_while_adapters_build_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cli_consumption.models.MAX_SNAPSHOT_RECORDS", 2)
    snapshot = Snapshot(provider="codex")

    snapshot.conversations.append({"id": "one"})
    snapshot.turns.append({"id": "two"})
    with pytest.raises(SnapshotValidationError, match="snapshot_too_large"):
        snapshot.model_calls.append({"id": "three"})
