from __future__ import annotations

import gzip
import json
import os
import stat
import tempfile
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

from cli_consumption.models import MAX_SNAPSHOT_RECORDS, Snapshot

FILE_MAGIC = b"CLI-CONSUMPTION-SNAPSHOT-V1\n"
SIGNATURE_SIZE = 64
MAX_SIGNED_FILE_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_KEY_BYTES = 64 * 1024
MAX_SNAPSHOTS = 64

_SNAPSHOT_COLLECTIONS = (
    "conversations",
    "turns",
    "model_calls",
    "tool_calls",
    "work_items",
    "context_samples",
    "turn_settings",
    "compaction_events",
    "subagents",
)


class SnapshotFileError(ValueError):
    """A bounded snapshot-file failure safe for CLI output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def write_snapshot_file(
    snapshots: list[Snapshot], output: Path, signing_key: Path
) -> None:
    """Validate, compress, sign, and atomically install an offline snapshot file."""
    if not snapshots or len(snapshots) > MAX_SNAPSHOTS:
        raise SnapshotFileError("snapshot_file_invalid")
    validated = [Snapshot.from_dict(snapshot.to_dict()) for snapshot in snapshots]
    _bound_total_records(validated)
    payload = json.dumps(
        {
            "format": "cli-consumption.snapshot",
            "format_version": 1,
            "snapshots": [snapshot.to_dict() for snapshot in validated],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_DECOMPRESSED_BYTES:
        raise SnapshotFileError("snapshot_payload_too_large")
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    private_key = _load_private_key(_read_regular_file(signing_key, MAX_KEY_BYTES))
    signed_content = FILE_MAGIC + compressed
    signature = private_key.sign(signed_content)
    if len(signature) != SIGNATURE_SIZE:
        raise SnapshotFileError("snapshot_key_invalid")
    contents = FILE_MAGIC + signature + compressed
    if len(contents) > MAX_SIGNED_FILE_BYTES:
        raise SnapshotFileError("snapshot_file_too_large")
    _atomic_write(output, contents)


def read_snapshot_file(input_path: Path, verification_key: Path) -> list[Snapshot]:
    """Verify an offline snapshot before bounded decompression and strict parsing."""
    contents = _read_regular_file(input_path, MAX_SIGNED_FILE_BYTES)
    if len(contents) <= len(FILE_MAGIC) + SIGNATURE_SIZE or not contents.startswith(
        FILE_MAGIC
    ):
        raise SnapshotFileError("snapshot_file_invalid")
    signature_start = len(FILE_MAGIC)
    signature_end = signature_start + SIGNATURE_SIZE
    signature = contents[signature_start:signature_end]
    compressed = contents[signature_end:]
    public_key = _load_public_key(_read_regular_file(verification_key, MAX_KEY_BYTES))
    try:
        public_key.verify(signature, FILE_MAGIC + compressed)
    except Exception as error:
        if error.__class__.__module__.startswith("cryptography"):
            raise SnapshotFileError("snapshot_signature_invalid") from None
        raise
    payload = _decompress_bounded(compressed)
    try:
        envelope = json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, SnapshotFileError):
        raise SnapshotFileError("snapshot_file_invalid") from None
    if not isinstance(envelope, dict) or set(envelope) != {
        "format",
        "format_version",
        "snapshots",
    }:
        raise SnapshotFileError("snapshot_file_invalid")
    if (
        envelope["format"] != "cli-consumption.snapshot"
        or envelope["format_version"] != 1
        or isinstance(envelope["format_version"], bool)
        or not isinstance(envelope["snapshots"], list)
        or not 1 <= len(envelope["snapshots"]) <= MAX_SNAPSHOTS
    ):
        raise SnapshotFileError("snapshot_file_invalid")
    try:
        snapshots = [
            Snapshot.from_dict(value)
            for value in envelope["snapshots"]
            if isinstance(value, dict)
        ]
    except ValueError:
        raise SnapshotFileError("snapshot_file_invalid") from None
    if len(snapshots) != len(envelope["snapshots"]):
        raise SnapshotFileError("snapshot_file_invalid")
    _bound_total_records(snapshots)
    return snapshots


def _load_private_key(data: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ModuleNotFoundError:
        raise SnapshotFileError("snapshot_dependency_missing") from None
    try:
        key = load_pem_private_key(data, password=None)
    except (TypeError, ValueError):
        raise SnapshotFileError("snapshot_key_invalid") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SnapshotFileError("snapshot_key_invalid")
    return key


def _load_public_key(data: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
    except ModuleNotFoundError:
        raise SnapshotFileError("snapshot_dependency_missing") from None
    try:
        key = load_pem_public_key(data)
    except ValueError:
        raise SnapshotFileError("snapshot_key_invalid") from None
    if not isinstance(key, Ed25519PublicKey):
        raise SnapshotFileError("snapshot_key_invalid")
    return key


def _read_regular_file(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SnapshotFileError("snapshot_file_invalid") from None
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > limit:
            code = (
                "snapshot_file_too_large"
                if file_stat.st_size > limit
                else "snapshot_file_invalid"
            )
            raise SnapshotFileError(code)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise SnapshotFileError("snapshot_file_too_large")
        return data
    finally:
        os.close(descriptor)


def _decompress_bounded(compressed: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=BytesIO(compressed), mode="rb") as handle:
            payload = handle.read(MAX_DECOMPRESSED_BYTES + 1)
    except (EOFError, OSError):
        raise SnapshotFileError("snapshot_file_invalid") from None
    if len(payload) > MAX_DECOMPRESSED_BYTES:
        raise SnapshotFileError("snapshot_payload_too_large")
    return payload


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotFileError("snapshot_file_invalid")
        result[key] = value
    return result


def _bound_total_records(snapshots: list[Snapshot]) -> None:
    total = sum(
        len(getattr(snapshot, collection))
        for snapshot in snapshots
        for collection in _SNAPSHOT_COLLECTIONS
    )
    if total > MAX_SNAPSHOT_RECORDS:
        raise SnapshotFileError("snapshot_payload_too_large")


def _atomic_write(output: Path, contents: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    try:
        current = output.stat(follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISREG(current.st_mode):
            mode = stat.S_IMODE(current.st_mode)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, mode)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
        _fsync_directory(output.parent)
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                temporary_path.unlink()


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
