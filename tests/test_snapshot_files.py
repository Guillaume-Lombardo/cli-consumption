from __future__ import annotations

import gzip
import json
import os
import zlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from typer.testing import CliRunner

from cli_consumption.cli import CollectionFailure, app
from cli_consumption.models import Snapshot
from cli_consumption.snapshot_files import (
    FILE_MAGIC,
    SIGNATURE_SIZE,
    SnapshotFileError,
    read_snapshot_file,
    write_snapshot_file,
)


def _keys(root: Path, name: str = "signer") -> tuple[Path, Path, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    private_path = root / f"{name}-private.pem"
    public_path = root / f"{name}-public.pem"
    private_path.write_bytes(
        private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    public_path.write_bytes(
        private.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
    )
    return private_path, public_path, private


def _signed_envelope(private: Ed25519PrivateKey, envelope: object) -> bytes:
    compressed = gzip.compress(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
        mtime=0,
    )
    return FILE_MAGIC + private.sign(FILE_MAGIC + compressed) + compressed


def test_snapshot_file_round_trip_is_deterministic_and_private(tmp_path: Path) -> None:
    private_path, public_path, _ = _keys(tmp_path)
    output = tmp_path / "usage.snapshot"
    snapshots = [Snapshot(provider="codex"), Snapshot(provider="claude")]

    write_snapshot_file(snapshots, output, private_path)
    first = output.read_bytes()
    write_snapshot_file(snapshots, output, private_path)

    assert output.read_bytes() == first
    assert [
        snapshot.provider for snapshot in read_snapshot_file(output, public_path)
    ] == [
        "codex",
        "claude",
    ]
    assert output.stat().st_mode & 0o777 == 0o600


def test_signature_is_checked_before_decompression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path, public_path, _ = _keys(tmp_path)
    output = tmp_path / "usage.snapshot"
    write_snapshot_file([Snapshot(provider="codex")], output, private_path)
    contents = bytearray(output.read_bytes())
    contents[-1] ^= 1
    output.write_bytes(contents)
    decompressed = False

    def forbidden(_: bytes) -> bytes:
        nonlocal decompressed
        decompressed = True
        raise AssertionError

    monkeypatch.setattr("cli_consumption.snapshot_files._decompress_bounded", forbidden)
    with pytest.raises(SnapshotFileError, match="snapshot_signature_invalid"):
        read_snapshot_file(output, public_path)
    assert not decompressed


def test_corrupt_deflate_errors_are_reduced_to_a_generic_file_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path, public_path, _ = _keys(tmp_path)
    output = tmp_path / "usage.snapshot"
    write_snapshot_file([Snapshot(provider="codex")], output, private_path)

    def corrupt_deflate(*_: object, **__: object) -> bytes:
        raise zlib.error("synthetic private canary")

    monkeypatch.setattr(gzip.GzipFile, "read", corrupt_deflate)
    with pytest.raises(SnapshotFileError, match="snapshot_file_invalid"):
        read_snapshot_file(output, public_path)


def test_wrong_key_and_malformed_signed_envelope_are_rejected(tmp_path: Path) -> None:
    private_path, _, private = _keys(tmp_path, "first")
    _, wrong_public_path, _ = _keys(tmp_path, "second")
    output = tmp_path / "usage.snapshot"
    write_snapshot_file([Snapshot(provider="codex")], output, private_path)

    with pytest.raises(SnapshotFileError, match="snapshot_signature_invalid"):
        read_snapshot_file(output, wrong_public_path)

    valid_public = tmp_path / "first-public.pem"
    output.write_bytes(_signed_envelope(private, {"format": "unexpected"}))
    with pytest.raises(SnapshotFileError, match="snapshot_file_invalid"):
        read_snapshot_file(output, valid_public)


def test_truncated_and_oversized_signed_files_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path, public_path, _ = _keys(tmp_path)
    output = tmp_path / "usage.snapshot"
    write_snapshot_file([Snapshot(provider="codex")], output, private_path)
    complete = output.read_bytes()

    output.write_bytes(complete[: len(FILE_MAGIC) + 8])
    with pytest.raises(SnapshotFileError, match="snapshot_file_invalid"):
        read_snapshot_file(output, public_path)

    output.write_bytes(complete)
    monkeypatch.setattr(
        "cli_consumption.snapshot_files.MAX_SIGNED_FILE_BYTES", len(complete) - 1
    )
    with pytest.raises(SnapshotFileError, match="snapshot_file_too_large"):
        read_snapshot_file(output, public_path)


def test_bounded_decompression_and_snapshot_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path, public_path, _ = _keys(tmp_path)
    output = tmp_path / "usage.snapshot"
    write_snapshot_file([Snapshot(provider="codex")], output, private_path)
    monkeypatch.setattr("cli_consumption.snapshot_files.MAX_DECOMPRESSED_BYTES", 8)

    with pytest.raises(SnapshotFileError, match="snapshot_payload_too_large"):
        read_snapshot_file(output, public_path)
    with pytest.raises(SnapshotFileError, match="snapshot_file_invalid"):
        write_snapshot_file([Snapshot(provider="codex")] * 65, output, private_path)


def test_total_normalized_record_count_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path, _, _ = _keys(tmp_path)
    monkeypatch.setattr("cli_consumption.snapshot_files.MAX_SNAPSHOT_RECORDS", -1)

    with pytest.raises(SnapshotFileError, match="snapshot_payload_too_large"):
        write_snapshot_file(
            [Snapshot(provider="codex")], tmp_path / "usage.snapshot", private_path
        )


def test_atomic_replacement_preserves_previous_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path, _, _ = _keys(tmp_path)
    output = tmp_path / "usage.snapshot"
    output.write_bytes(b"previous")
    output.chmod(0o640)

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("private canary")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="private canary"):
        write_snapshot_file([Snapshot(provider="codex")], output, private_path)

    assert output.read_bytes() == b"previous"
    assert output.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".usage.snapshot.*.tmp"))


def test_cli_snapshot_round_trip_is_idempotent_and_privacy_safe(
    tmp_path: Path, rollout_factory
) -> None:
    private_path, public_path, _ = _keys(tmp_path)
    codex_home = tmp_path / "codex"
    rollout = rollout_factory(codex_home)
    canary = "PROMPT_SECRET_CANARY"
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-08-25T10:00:03.750Z",
                    "type": "response_item",
                    "payload": {"type": "message", "content": canary},
                }
            )
            + "\n"
        )
    output = tmp_path / "usage.snapshot"
    database = tmp_path / "usage.sqlite"
    runner = CliRunner()

    created = runner.invoke(
        app,
        [
            "snapshot",
            "create",
            "--signing-key",
            str(private_path),
            "--source",
            f"desktop={codex_home}",
            "--output",
            str(output),
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout) == {"providers": ["codex"], "snapshots": 1}
    compressed = output.read_bytes()[len(FILE_MAGIC) + SIGNATURE_SIZE :]
    assert canary.encode() not in gzip.decompress(compressed)
    assert canary not in str(read_snapshot_file(output, public_path)[0].to_dict())

    first = runner.invoke(
        app,
        [
            "snapshot",
            "ingest",
            "--input",
            str(output),
            "--verification-key",
            str(public_path),
            "--database",
            str(database),
            "--json",
        ],
    )
    second = runner.invoke(
        app,
        [
            "snapshot",
            "ingest",
            "--input",
            str(output),
            "--verification-key",
            str(public_path),
            "--database",
            str(database),
            "--json",
        ],
    )
    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout)["ingestions"][0]["written"] > 0
    assert json.loads(second.stdout)["ingestions"][0]["written"] == 0
    assert canary not in first.stdout + second.stdout


def test_cli_snapshot_errors_do_not_expose_paths_or_key_contents(
    tmp_path: Path,
) -> None:
    canary = "PRIVATE_KEY_AND_PATH_CANARY"
    key = tmp_path / canary
    key.write_text(canary, encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "snapshot",
            "ingest",
            "--input",
            str(tmp_path / "missing.snapshot"),
            "--verification-key",
            str(key),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"error": {"code": "snapshot_file_invalid"}}
    assert canary not in result.stdout


@pytest.mark.parametrize("code", ["invalid_snapshot", "provider_collection_failed"])
def test_cli_snapshot_preserves_bounded_collection_error_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    def fail_collection(*_: object) -> list[Snapshot]:
        raise CollectionFailure("codex", code, "synthetic private canary")

    monkeypatch.setattr("cli_consumption.cli._collect_snapshots", fail_collection)
    result = CliRunner().invoke(
        app,
        [
            "snapshot",
            "create",
            "--signing-key",
            str(tmp_path / "private-canary.pem"),
            "--output",
            str(tmp_path / "private-canary.snapshot"),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"error": {"code": code}}
    assert "synthetic private canary" not in result.stdout
