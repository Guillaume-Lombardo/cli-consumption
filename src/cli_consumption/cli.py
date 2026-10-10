from __future__ import annotations

import json
import os
import platform
import secrets
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Annotated, Literal, Never, Protocol, TextIO, TypedDict, cast

import typer
from sqlalchemy.engine import Engine

from cli_consumption import __version__
from cli_consumption.adapters._incremental import is_aggregate_limit
from cli_consumption.adapters._shared import ProviderDataLimitError
from cli_consumption.adapters.base import (
    CollectionBatch,
    IncrementalAdapter,
    UnsupportedProviderFormat,
)
from cli_consumption.adapters.registry import (
    ADAPTER_SPECS,
    AdapterSpec,
    default_source_path,
    diagnose_provider,
    has_provider_data,
    resolve_adapter_spec,
)
from cli_consumption.dashboard import DashboardLimitError, generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.models import Snapshot, SnapshotValidationError
from cli_consumption.reporting import parse_export_window
from cli_consumption.retention import retain_before
from cli_consumption.snapshot_extraction import (
    SnapshotExtractionError,
    extract_snapshots,
)
from cli_consumption.storage import (
    MissingOptionalDependencyError,
    create_database_engine,
    ingest_snapshot,
    initialize_database,
    validate_snapshot,
)
from cli_consumption.usage_report import (
    Breakdown,
    ReportView,
    UsageQuery,
    UsageQueryError,
    UsageReport,
    aggregate_usage,
    parse_report_window,
    report_filters,
    resolve_timezone,
)

MAX_INCREMENTAL_BATCHES = 10_000
MAX_INCREMENTAL_STAGING_BYTES = 4 * 1024 * 1024 * 1024

app = typer.Typer(
    name="cli-consumption",
    help="Analyze AI coding CLI consumption without exporting conversation content.",
    no_args_is_help=True,
)
snapshot_app = typer.Typer(
    help="Create and ingest signed, compressed metadata-only snapshot files.",
    no_args_is_help=True,
)
app.add_typer(snapshot_app, name="snapshot")


class CollectionFailure(RuntimeError):
    """A classified provider failure containing only bounded presentation fields."""

    def __init__(self, provider: str, code: str, message: str) -> None:
        self.provider = provider
        self.code = code
        self.message = message
        super().__init__(code)


class IncrementalIngestion(TypedDict):
    provider: str
    batched: bool
    batches: int
    received: int
    written: int
    skipped: int
    malformed: int
    batch_duplicates: int


IncrementalTrigger = Literal["requested", "automatic"]


class _AggregateLimitExceeded(Exception):
    """An incremental-capable provider exceeded only an aggregate collection limit."""


@dataclass(frozen=True, slots=True)
class _PlannedCollection:
    """One provider's collection plan: a collected snapshot or deferred batches."""

    spec: AdapterSpec
    sources: list[tuple[str, Path]]
    snapshot: Snapshot | None
    batched: bool


class _ServerProcess(Protocol):
    should_exit: bool

    def run(self) -> None: ...


class _BoundedStagingWriter:
    """Count UTF-8 metadata bytes before writing them to strict staging."""

    def __init__(self, handle: TextIO, consumed: int) -> None:
        self.handle = handle
        self.consumed = consumed

    def write(self, value: str) -> int:
        size = len(value.encode("utf-8"))
        if self.consumed + size > MAX_INCREMENTAL_STAGING_BYTES:
            raise ProviderDataLimitError("provider_incremental_staging_limit_exceeded")
        self.consumed += size
        return self.handle.write(value)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


def _open_database(database: str | Path) -> Engine:
    try:
        return create_database_engine(database)
    except MissingOptionalDependencyError as error:
        raise typer.BadParameter(str(error)) from None


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """Collect locally, consolidate offline, or send snapshots to an API."""


@app.command()
def providers(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Check local provider formats and emit deterministic JSON.",
        ),
    ] = False,
) -> None:
    """Show supported CLI adapters and check local format compatibility."""
    if json_output:
        payload = {
            "schema_version": 2,
            "providers": [
                diagnose_provider(spec, default_source_path(spec)).to_dict()
                for spec in ADAPTER_SPECS
            ],
        }
        typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    typer.echo("all      auto-detect supported providers")
    for spec in ADAPTER_SPECS:
        separator = " " * max(1, 9 - len(spec.name))
        typer.echo(f"{spec.name}{separator}{spec.support}")


@app.command()
def collect(
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help="[LABEL=]PROVIDER_HOME. Repeat to consolidate copied machine data.",
        ),
    ] = None,
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "-d",
            envvar="CLI_CONSUMPTION_DATABASE",
            help="SQLite path or SQLAlchemy PostgreSQL URL.",
        ),
    ] = "cli-consumption.sqlite",
    provider: Annotated[
        str, typer.Option(help="CLI provider to collect, or 'all' to auto-detect.")
    ] = "codex",
    project: Annotated[
        list[str] | None,
        typer.Option(
            "--project",
            help="NAME=PATH_PREFIX project mapping. Longest matching prefix wins.",
        ),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Refuse ingestion when any malformed provider record was skipped.",
        ),
    ] = False,
    incremental: Annotated[
        bool | None,
        typer.Option(
            "--incremental/--no-incremental",
            help=(
                "Force bounded, restart-safe batches for supported providers, or "
                "forbid the automatic switch to batches when a supported provider "
                "exceeds an aggregate collection limit."
            ),
            show_default="automatic",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Collect one or more local/copied CLI data directories into SQL storage."""
    if incremental:
        plans, mappings = _plan_collection(provider, source, project, mode="forced")
        _collect_incrementally(
            plans,
            mappings,
            database,
            strict=strict,
            json_output=json_output,
            trigger="requested",
        )
        return
    try:
        plans, mappings = _plan_collection(
            provider,
            source,
            project,
            mode="never" if incremental is False else "automatic",
        )
    except CollectionFailure as error:
        _abort_collection(error, json_output=json_output)
    if any(plan.batched for plan in plans):
        _collect_incrementally(
            plans,
            mappings,
            database,
            strict=strict,
            json_output=json_output,
            trigger="automatic",
        )
        return
    snapshots = [plan.snapshot for plan in plans if plan.snapshot is not None]
    if strict and any(snapshot.malformed_records for snapshot in snapshots):
        raise typer.BadParameter(
            "--strict refused snapshots containing malformed provider records"
        )
    engine = _open_database(database)
    try:
        results = []
        for snapshot in snapshots:
            try:
                result = ingest_snapshot(engine, snapshot)
            except SnapshotValidationError as error:
                _abort_collection(
                    _snapshot_failure(snapshot.provider, error),
                    json_output=json_output,
                )
            results.append((snapshot, result))
    finally:
        engine.dispose()
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "ingestions": [
                        {
                            "provider": snapshot.provider,
                            "run_id": result.run_id,
                            "received": result.received,
                            "written": result.written,
                            "skipped": result.skipped,
                            "malformed": snapshot.malformed_records,
                            "duplicates": snapshot.duplicate_conversations,
                        }
                        for snapshot, result in results
                    ]
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    for snapshot, result in results:
        typer.echo(
            f"Ingestion {snapshot.provider} {result.run_id}: "
            f"{result.written} written, {result.skipped} unchanged, "
            f"{snapshot.malformed_records} malformed skipped."
        )


@snapshot_app.command("create")
def snapshot_create(
    signing_key: Annotated[Path, typer.Option(help="Ed25519 private key in PEM form.")],
    output: Annotated[Path, typer.Option("--output", "-o")],
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="[LABEL=]PROVIDER_HOME. Repeat as needed."),
    ] = None,
    provider: Annotated[
        str, typer.Option(help="CLI provider to collect, or 'all' to auto-detect.")
    ] = "codex",
    project: Annotated[
        list[str] | None,
        typer.Option("--project", help="NAME=PATH_PREFIX project mapping."),
    ] = None,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Refuse creation when any malformed provider record was skipped.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Collect metadata and write one authenticated offline snapshot file."""
    from cli_consumption.snapshot_files import SnapshotFileError, write_snapshot_file

    try:
        snapshots = _collect_snapshots(provider, source, project)
    except CollectionFailure as error:
        _abort_snapshot(error.code, json_output=json_output)
    except Exception:
        _abort_snapshot("local_collection_failed", json_output=json_output)
    if strict and any(snapshot.malformed_records for snapshot in snapshots):
        _abort_snapshot("malformed_records", json_output=json_output)
    try:
        write_snapshot_file(snapshots, output, signing_key)
    except (OSError, SnapshotFileError, SnapshotValidationError) as error:
        _abort_snapshot(
            getattr(error, "code", "snapshot_file_invalid"),
            json_output=json_output,
        )
    result = {
        "providers": [snapshot.provider for snapshot in snapshots],
        "snapshots": len(snapshots),
    }
    if json_output:
        typer.echo(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        typer.echo(f"Wrote {len(snapshots)} signed metadata snapshots.")


@snapshot_app.command("ingest")
def snapshot_ingest(
    input_path: Annotated[Path, typer.Option("--input", "-i")],
    verification_key: Annotated[
        Path, typer.Option(help="Trusted Ed25519 public key in PEM form.")
    ],
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "-d",
            envvar="CLI_CONSUMPTION_DATABASE",
            help="SQLite path or SQLAlchemy PostgreSQL URL.",
        ),
    ] = "cli-consumption.sqlite",
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Verify and ingest an authenticated offline snapshot file."""
    from cli_consumption.snapshot_files import SnapshotFileError, read_snapshot_file

    try:
        snapshots = read_snapshot_file(input_path, verification_key)
    except (OSError, SnapshotFileError, SnapshotValidationError) as error:
        _abort_snapshot(
            getattr(error, "code", "snapshot_file_invalid"),
            json_output=json_output,
        )
    engine = _open_database(database)
    try:
        results = [
            (snapshot, ingest_snapshot(engine, snapshot)) for snapshot in snapshots
        ]
    except SnapshotValidationError:
        _abort_snapshot("snapshot_file_invalid", json_output=json_output)
    finally:
        engine.dispose()
    payload = {
        "ingestions": [
            {
                "provider": snapshot.provider,
                "run_id": result.run_id,
                "received": result.received,
                "written": result.written,
                "skipped": result.skipped,
                "malformed": snapshot.malformed_records,
                "duplicates": snapshot.duplicate_conversations,
            }
            for snapshot, result in results
        ]
    }
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        typer.echo(f"Ingested {len(results)} verified metadata snapshots.")


def _abort_snapshot(code: str, *, json_output: bool) -> Never:
    safe_codes = {
        "invalid_snapshot",
        "local_collection_failed",
        "malformed_records",
        "provider_collection_failed",
        "provider_format_incompatible",
        "provider_limit_exceeded",
        "snapshot_dependency_missing",
        "snapshot_file_invalid",
        "snapshot_file_too_large",
        "snapshot_key_invalid",
        "snapshot_payload_too_large",
        "snapshot_signature_invalid",
    }
    bounded_code = code if code in safe_codes else "snapshot_file_invalid"
    if json_output:
        typer.echo(
            json.dumps(
                {"error": {"code": bounded_code}},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        if bounded_code == "snapshot_dependency_missing":
            typer.echo(
                "Snapshot files require optional dependencies; "
                "install cli-consumption[snapshots].",
                err=True,
            )
        else:
            typer.echo(f"Snapshot operation failed ({bounded_code}).", err=True)
    raise typer.Exit(code=2) from None


@app.command()
def sync(
    endpoint: Annotated[
        str,
        typer.Option(
            help="Collector base URL, for example https://usage.example.test."
        ),
    ],
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="[LABEL=]PROVIDER_HOME. Repeat as needed."),
    ] = None,
    provider: Annotated[
        str, typer.Option(help="CLI provider to collect, or 'all' to auto-detect.")
    ] = "codex",
    project: Annotated[
        list[str] | None,
        typer.Option("--project", help="NAME=PATH_PREFIX project mapping."),
    ] = None,
    token_env: Annotated[
        str,
        typer.Option(help="Environment variable containing the API bearer token."),
    ] = "CLI_CONSUMPTION_API_TOKEN",
    allow_insecure: Annotated[
        bool,
        typer.Option(
            "--allow-insecure",
            help="Allow plain HTTP to a non-loopback collector on a trusted network.",
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Refuse upload when any malformed provider record was skipped.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Collect locally and send metadata-only records to a central collector."""
    try:
        from cli_consumption.sync import SyncClient
    except ModuleNotFoundError:
        if json_output:
            _emit_sync_json(
                [],
                complete=False,
                error_code="sync_dependency_missing",
            )
            raise typer.Exit(code=2) from None
        raise typer.BadParameter(
            "sync requires optional dependencies; install cli-consumption[sync]"
        ) from None

    try:
        snapshots = _collect_snapshots(provider, source, project)
    except CollectionFailure as error:
        if json_output:
            _emit_sync_json([], complete=False, error_code=error.code)
            raise typer.Exit(code=2) from None
        typer.echo(error.message, err=True)
        raise typer.Exit(code=2) from None
    except Exception:  # Provider errors are untrusted and stay generic for sync.
        if json_output:
            _emit_sync_json(
                [],
                complete=False,
                error_code="local_collection_failed",
            )
            raise typer.Exit(code=2) from None
        typer.echo("Local provider collection failed.", err=True)
        raise typer.Exit(code=2) from None
    if strict and any(snapshot.malformed_records for snapshot in snapshots):
        outcomes = [
            _sync_diagnostics(snapshot, status="refused") for snapshot in snapshots
        ]
        if json_output:
            _emit_sync_json(
                outcomes,
                complete=False,
                error_code="malformed_records",
            )
            raise typer.Exit(code=2) from None
        raise typer.BadParameter(
            "--strict refused snapshots containing malformed provider records"
        )

    token = os.environ.get(token_env)
    outcomes: list[dict[str, object]] = []
    failures = 0
    try:
        with SyncClient(endpoint, token, allow_insecure=allow_insecure) as sync_client:
            for snapshot in snapshots:
                try:
                    result = sync_client.send_snapshot(snapshot)
                except Exception:  # Remote errors are untrusted and stay generic.
                    failures += 1
                    outcomes.append(
                        {
                            **_sync_diagnostics(snapshot, status="failed"),
                            "error": {"code": "remote_sync_failed"},
                        }
                    )
                    if not json_output:
                        typer.echo(
                            f"Remote ingestion {snapshot.provider} failed.", err=True
                        )
                    continue
                outcomes.append(
                    {
                        **_sync_diagnostics(snapshot, status="succeeded"),
                        "run_id": result["run_id"],
                        "received": result["received"],
                        "written": result["written"],
                        "skipped": result["skipped"],
                    }
                )
                if not json_output:
                    typer.echo(
                        f"Remote ingestion {snapshot.provider} {result['run_id']}: "
                        f"{result['written']} written, {result['skipped']} unchanged, "
                        f"{snapshot.malformed_records} malformed, "
                        f"{snapshot.duplicate_conversations} duplicates."
                    )
    except Exception:  # Endpoint and client errors are untrusted and stay generic.
        if json_output:
            _emit_sync_json(
                outcomes,
                complete=False,
                error_code="remote_sync_failed",
            )
            raise typer.Exit(code=2) from None
        typer.echo("Remote synchronization failed.", err=True)
        raise typer.Exit(code=2) from None

    complete = failures == 0
    if json_output:
        _emit_sync_json(outcomes, complete=complete)
    elif failures:
        succeeded = len(outcomes) - failures
        typer.echo(
            f"Synchronization partially completed: {succeeded} succeeded, "
            f"{failures} failed.",
            err=True,
        )
    if not complete:
        raise typer.Exit(code=2)


@app.command("upload-db")
def upload_database(
    endpoint: Annotated[
        str,
        typer.Option(
            help="Collector base URL, for example https://usage.example.test."
        ),
    ],
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "-d",
            envvar="CLI_CONSUMPTION_DATABASE",
            help="Existing local SQLite database created by collect.",
        ),
    ] = "cli-consumption.sqlite",
    since: Annotated[
        str | None,
        typer.Option(help="Include conversations overlapping this date or timestamp."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(help="Exclude conversations at or after this date or timestamp."),
    ] = None,
    token_env: Annotated[
        str,
        typer.Option(help="Environment variable containing the API bearer token."),
    ] = "CLI_CONSUMPTION_API_TOKEN",
    allow_insecure: Annotated[
        bool,
        typer.Option(
            "--allow-insecure",
            help="Allow plain HTTP to a non-loopback collector on a trusted network.",
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Stop after the first failed provider instead of continuing.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Upload validated snapshots reconstructed from a local collection database."""
    try:
        from cli_consumption.sync import (
            IdempotencyUnsupportedError,
            SyncClient,
            snapshot_idempotency_key,
        )
    except ModuleNotFoundError:
        _abort_database_upload(
            [],
            code="upload_dependency_missing",
            json_output=json_output,
        )

    try:
        snapshots = extract_snapshots(database, since=since, until=until)
    except SnapshotExtractionError as error:
        _abort_database_upload([], code=error.code, json_output=json_output)
    except Exception:
        _abort_database_upload(
            [],
            code="database_unavailable",
            json_output=json_output,
        )

    if not snapshots:
        if json_output:
            _emit_database_upload_json([], complete=True)
        else:
            typer.echo("No matching snapshots to upload.")
        return

    token = os.environ.get(token_env)
    outcomes: list[dict[str, object]] = []
    ordered_snapshots = sorted(snapshots, key=lambda snapshot: snapshot.provider)
    try:
        with SyncClient(
            endpoint, token, allow_insecure=allow_insecure
        ) as upload_client:
            upload_client.require_idempotent_uploads()
            for index, snapshot in enumerate(ordered_snapshots):
                try:
                    result = upload_client.send_snapshot(
                        snapshot,
                        idempotency_key=snapshot_idempotency_key(snapshot),
                    )
                except Exception:
                    outcomes.append(
                        {
                            "provider": snapshot.provider,
                            "status": "failed",
                            "error": {"code": "remote_upload_failed"},
                        }
                    )
                    if not json_output:
                        typer.echo(
                            f"Database upload {snapshot.provider} failed.", err=True
                        )
                    if strict:
                        outcomes.extend(
                            {
                                "provider": remaining.provider,
                                "status": "skipped",
                                "error": {"code": "strict_upload_stopped"},
                            }
                            for remaining in ordered_snapshots[index + 1 :]
                        )
                        break
                    continue
                outcomes.append(
                    {
                        "provider": snapshot.provider,
                        "status": "succeeded",
                        "run_id": result["run_id"],
                        "received": result["received"],
                        "written": result["written"],
                        "skipped": result["skipped"],
                    }
                )
                if not json_output:
                    typer.echo(
                        f"Uploaded {snapshot.provider} {result['run_id']}: "
                        f"{result['written']} written, "
                        f"{result['skipped']} unchanged."
                    )
    except IdempotencyUnsupportedError:
        _abort_database_upload(
            outcomes,
            code="idempotency_unsupported",
            json_output=json_output,
        )
    except Exception:
        _abort_database_upload(
            outcomes,
            code="remote_upload_failed",
            json_output=json_output,
        )

    complete = all(outcome["status"] == "succeeded" for outcome in outcomes)
    if json_output:
        _emit_database_upload_json(outcomes, complete=complete)
    elif not complete:
        succeeded = sum(outcome["status"] == "succeeded" for outcome in outcomes)
        failed = sum(outcome["status"] == "failed" for outcome in outcomes)
        typer.echo(
            f"Database upload partially completed: {succeeded} succeeded, "
            f"{failed} failed.",
            err=True,
        )
    if not complete:
        raise typer.Exit(code=2)


def _sync_diagnostics(snapshot: Snapshot, *, status: str) -> dict[str, object]:
    """Return the bounded local diagnostics allowed in sync results."""
    return {
        "provider": snapshot.provider,
        "status": status,
        "malformed": snapshot.malformed_records,
        "duplicates": snapshot.duplicate_conversations,
    }


def _emit_sync_json(
    outcomes: list[dict[str, object]],
    *,
    complete: bool,
    error_code: str | None = None,
) -> None:
    """Emit one deterministic sync result without external error details."""
    payload: dict[str, object] = {
        "complete": complete,
        "synchronizations": outcomes,
    }
    if error_code is not None:
        payload["error"] = {"code": error_code}
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


_DATABASE_UPLOAD_ERROR_CODES = frozenset(
    {
        "database_unavailable",
        "idempotency_unsupported",
        "incompatible_database",
        "invalid_database",
        "invalid_window",
        "remote_upload_failed",
        "snapshot_too_large",
        "upload_dependency_missing",
    }
)


def _abort_database_upload(
    outcomes: list[dict[str, object]],
    *,
    code: str,
    json_output: bool,
) -> Never:
    bounded_code = (
        code if code in _DATABASE_UPLOAD_ERROR_CODES else "database_unavailable"
    )
    if json_output:
        _emit_database_upload_json(
            outcomes,
            complete=False,
            error_code=bounded_code,
        )
    elif bounded_code == "upload_dependency_missing":
        typer.echo(
            "Database upload requires optional dependencies; "
            "install cli-consumption[sync].",
            err=True,
        )
    else:
        typer.echo(f"Database upload failed ({bounded_code}).", err=True)
    raise typer.Exit(code=2) from None


def _emit_database_upload_json(
    outcomes: list[dict[str, object]],
    *,
    complete: bool,
    error_code: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "complete": complete,
        "uploads": outcomes,
    }
    if error_code is not None:
        payload["error"] = {"code": error_code}
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _emit_collection_json(
    error: CollectionFailure,
    *,
    ingestions: list[IncrementalIngestion] | None = None,
    trigger: IncrementalTrigger | None = None,
) -> None:
    """Emit one deterministic collection failure without provider error details."""
    payload: dict[str, object] = {
        "error": {"code": error.code, "provider": error.provider},
        "ingestions": ingestions or [],
    }
    if trigger is not None:
        payload["incremental"] = True
        payload["incremental_trigger"] = trigger
    typer.echo(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _abort_collection(
    error: CollectionFailure,
    *,
    json_output: bool,
    ingestions: list[IncrementalIngestion] | None = None,
    trigger: IncrementalTrigger | None = None,
) -> Never:
    if json_output:
        _emit_collection_json(error, ingestions=ingestions, trigger=trigger)
    else:
        completed = sum(item["batches"] for item in ingestions or [])
        suffix = (
            f" {completed} earlier incremental batch(es) were committed; rerun is safe."
            if trigger is not None and completed
            else ""
        )
        typer.echo(error.message + suffix, err=True)
    raise typer.Exit(code=2) from None


@app.command("export")
def export_command(
    database: Annotated[
        str,
        typer.Option("--database", "-d", envvar="CLI_CONSUMPTION_DATABASE"),
    ] = "cli-consumption.sqlite",
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("reports"),
    dashboard: Annotated[bool, typer.Option("--dashboard/--no-dashboard")] = True,
    csv_exports: Annotated[
        bool,
        typer.Option(
            "--csv/--no-csv",
            help="Also write detailed normalized SQL tables as CSV files.",
        ),
    ] = False,
    share_safe: Annotated[
        bool,
        typer.Option(
            "--share-safe",
            help="Write a pseudonymized dashboard and reject detailed CSV exports.",
        ),
    ] = False,
    since: Annotated[
        str | None,
        typer.Option(
            help="Include conversations overlapping this UTC date or zoned timestamp.",
        ),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(
            help=(
                "Exclude conversations starting at/after this date or zoned timestamp."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Write a self-contained HTML dashboard and optional detailed CSV tables."""
    if share_safe and not dashboard:
        raise typer.BadParameter("--share-safe requires --dashboard")
    if share_safe and csv_exports:
        raise typer.BadParameter("--share-safe cannot be combined with --csv")
    if not dashboard and not csv_exports:
        raise typer.BadParameter("enable --dashboard or --csv")
    try:
        window = parse_export_window(since, until)
    except ValueError:
        raise typer.BadParameter(
            "invalid export window; use UTC dates or timezone-aware timestamps"
        ) from None
    if (
        share_safe
        and output.is_dir()
        and any(path.name != "dashboard.html" for path in output.iterdir())
    ):
        raise typer.BadParameter(
            "--share-safe output directory must be empty or contain only dashboard.html"
        )
    engine = _open_database(database)
    try:
        initialize_database(engine)
        paths = export_csv(engine, output, window=window) if csv_exports else []
        if dashboard:
            dashboard_path = output / "dashboard.html"
            try:
                generate_dashboard(
                    engine,
                    dashboard_path,
                    share_safe=share_safe,
                    window=window,
                )
            except DashboardLimitError:
                hint = "narrow the export with --since and/or --until"
                if json_output:
                    typer.echo(
                        json.dumps(
                            {
                                "error": {
                                    "code": "dashboard_limit_exceeded",
                                    "hint": hint,
                                }
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                else:
                    typer.echo(
                        f"Dashboard exceeds safe generation limits; {hint}.",
                        err=True,
                    )
                raise typer.Exit(code=2) from None
            paths.append(dashboard_path)
    finally:
        engine.dispose()
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "files": [path.name for path in paths],
                    "written": len(paths),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(f"Wrote {len(paths)} files to {output.resolve()}")


@app.command("retention")
def retention_command(
    keep_days: Annotated[
        int,
        typer.Option(
            min=1,
            help="Keep normalized metadata from this many most recent days.",
        ),
    ],
    database: Annotated[
        str,
        typer.Option("--database", "-d", envvar="CLI_CONSUMPTION_DATABASE"),
    ] = "cli-consumption.sqlite",
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Apply the deletion. Without this flag, only preview counts.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Preview or delete normalized metadata older than a retention window."""
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    engine = _open_database(database)
    try:
        result = retain_before(engine, cutoff, apply=apply)
    finally:
        engine.dispose()
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "applied": result.applied,
                    "cutoff": result.cutoff.isoformat(),
                    "conversations": result.conversations,
                    "subagents": result.subagents,
                    "ingestion_runs": result.ingestion_runs,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        mode = "Applied" if result.applied else "Preview"
        typer.echo(
            f"{mode} retention before {result.cutoff.isoformat()}: "
            f"{result.conversations} conversations, {result.subagents} subagents, "
            f"{result.ingestion_runs} ingestion runs."
        )


# Terminal usage reports -----------------------------------------------------

_REPORT_ERROR_MESSAGES = {
    "database_not_found": (
        "No usage database found. Run `cli-consumption quick` or "
        "`cli-consumption collect` first, or pass --database."
    ),
    "database_driver_missing": (
        "PostgreSQL support requires optional dependencies; "
        "install cli-consumption[postgres]."
    ),
    "database_unavailable": "The usage database is unavailable.",
    "invalid_timezone": "Unknown timezone; use an IANA name such as Europe/Paris.",
    "invalid_window": (
        "Invalid report window; use dates or timezone-aware timestamps with "
        "--since earlier than --until."
    ),
    "report_limit_exceeded": (
        "Share-safe labels exceed safe limits; narrow the report with --since "
        "and/or --until."
    ),
    "unknown_provider": "Unknown provider. Run `cli-consumption providers`.",
}


@app.command("report")
def report_command(
    view: Annotated[
        ReportView,
        typer.Argument(
            help="Group by day, ISO week (Monday start), month, or session.",
        ),
    ] = ReportView.DAILY,
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "-d",
            envvar="CLI_CONSUMPTION_DATABASE",
            help="SQLite path or SQLAlchemy PostgreSQL URL.",
        ),
    ] = "cli-consumption.sqlite",
    since: Annotated[
        str | None,
        typer.Option(
            help="Window start: a date in --timezone or a zoned timestamp.",
        ),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(
            help="Exclusive window end: a date (included) or a zoned timestamp.",
        ),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option("--provider", help="Only this provider or alias. Repeatable."),
    ] = None,
    project: Annotated[
        list[str] | None,
        typer.Option("--project", help="Only this project label. Repeatable."),
    ] = None,
    machine: Annotated[
        list[str] | None,
        typer.Option("--machine", help="Only this machine label. Repeatable."),
    ] = None,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", help="Only calls of this model. Repeatable."),
    ] = None,
    timezone: Annotated[
        str,
        typer.Option(help="IANA timezone for periods and plain dates."),
    ] = "UTC",
    by: Annotated[
        Breakdown | None,
        typer.Option("--by", help="Break every row down by this dimension."),
    ] = None,
    share_safe: Annotated[
        bool,
        typer.Option(
            "--share-safe",
            help="Pseudonymize project, machine, and model labels.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit the versioned JSON report.")
    ] = False,
) -> None:
    """Show token usage tables from the database without collecting."""
    try:
        query = UsageQuery(
            view=view,
            window=parse_report_window(since, until, timezone),
            filters=report_filters(
                providers=provider or (),
                machines=machine or (),
                projects=project or (),
                models=model or (),
            ),
            timezone=timezone,
            breakdown=by,
            share_safe=share_safe,
        )
    except UsageQueryError as error:
        _abort_report(error.code, json_output=json_output)
    if "://" not in database and not Path(database).expanduser().is_file():
        _abort_report("database_not_found", json_output=json_output)
    with _usage_database(database, json_output=json_output) as engine:
        report = aggregate_usage(engine, query)
    _emit_usage_report(report, json_output=json_output)


@app.command("quick")
def quick_command(
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "-d",
            envvar="CLI_CONSUMPTION_DATABASE",
            help="SQLite path or SQLAlchemy PostgreSQL URL.",
        ),
    ] = "cli-consumption.sqlite",
    timezone: Annotated[
        str,
        typer.Option(help="IANA timezone for daily periods."),
    ] = "UTC",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit collection results and the JSON report."),
    ] = False,
) -> None:
    """Collect every detected provider, then show the daily usage report."""
    try:
        resolve_timezone(timezone)
    except UsageQueryError as error:
        _abort_report(error.code, json_output=json_output)
    try:
        inputs, mappings = _collection_inputs("all", None, None)
    except typer.BadParameter:
        inputs, mappings = [], []
    summaries: dict[str, IncrementalIngestion] = {}
    batched: set[str] = set()
    failures: list[CollectionFailure] = []
    with _usage_database(database, json_output=json_output) as engine:
        batches = 0
        for spec, sources in inputs:
            # Each provider is planned and ingested like `collect --provider all`,
            # switching to bounded batches on an aggregate overrun, but one
            # provider's failure never stops the others.
            try:
                plan = _plan_provider(spec, sources, mappings, mode="automatic")
                if plan.batched:
                    batched.add(spec.name)
                for batch in _iter_planned_batches([plan], mappings):
                    batches += 1
                    if batches > MAX_INCREMENTAL_BATCHES:
                        raise CollectionFailure(
                            spec.name,
                            "provider_limit_exceeded",
                            f"Provider {spec.name!r} data exceeds collection "
                            "safety limits.",
                        )
                    _ingest_batch(engine, batch, summaries, batched)
            except CollectionFailure as error:
                failures.append(error)
        report = aggregate_usage(engine, UsageQuery(timezone=timezone))
    ingestions = list(summaries.values())
    if json_output:
        payload = {
            "collection": {
                "incremental": bool(batched),
                "ingestions": ingestions,
                "failures": [
                    {"provider": failure.provider, "code": failure.code}
                    for failure in failures
                ],
            },
            "report": report.to_dict(),
        }
        typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        if not inputs:
            typer.echo("No supported provider data detected.", err=True)
        if batched:
            typer.echo(
                "Aggregate collection limits exceeded; collected in bounded batches: "
                + ", ".join(sorted(batched))
                + ".",
                err=True,
            )
        for item in ingestions:
            batches_note = (
                f" in {item['batches']} bounded batches" if item["batched"] else ""
            )
            typer.echo(
                f"Collected {item['provider']}{batches_note}: {item['written']} "
                f"written, {item['skipped']} unchanged, {item['malformed']} "
                "malformed skipped.",
                err=True,
            )
        for failure in failures:
            committed = summaries.get(failure.provider)
            suffix = (
                f" {committed['batches']} earlier batch(es) were committed; "
                "rerun is safe."
                if committed is not None and committed["batched"]
                else ""
            )
            typer.echo(failure.message + suffix, err=True)
        _emit_usage_report(report, json_output=False)
    if failures:
        raise typer.Exit(code=2)


@contextmanager
def _usage_database(database: str, *, json_output: bool) -> Iterator[Engine]:
    """Open, use, and dispose a database behind a fixed-code error boundary.

    Engine construction, migrations, ingestion, and aggregation failures can carry
    URLs, paths, SQL statements, and bound parameters in their text, so they are
    reduced to fixed codes and never printed.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from cli_consumption.schema import SchemaCompatibilityError

    database_errors = (SQLAlchemyError, SchemaCompatibilityError, OSError, ValueError)
    try:
        engine = create_database_engine(database)
    except MissingOptionalDependencyError:
        _abort_report("database_driver_missing", json_output=json_output)
    except database_errors:
        _abort_report("database_unavailable", json_output=json_output)
    try:
        yield engine
    except DashboardLimitError:
        _abort_report("report_limit_exceeded", json_output=json_output)
    except database_errors:
        _abort_report("database_unavailable", json_output=json_output)
    finally:
        engine.dispose()


def _emit_usage_report(report: UsageReport, *, json_output: bool) -> None:
    import sys

    from cli_consumption.terminal_report import (
        color_enabled,
        render_report,
        terminal_width,
    )

    if json_output:
        typer.echo(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
        return
    typer.echo(
        render_report(report, width=terminal_width(), color=color_enabled(sys.stdout)),
        nl=False,
    )


def _abort_report(code: str, *, json_output: bool) -> Never:
    if json_output:
        typer.echo(
            json.dumps({"error": {"code": code}}, sort_keys=True, separators=(",", ":"))
        )
    else:
        typer.echo(_REPORT_ERROR_MESSAGES[code], err=True)
    raise typer.Exit(code=2)


@app.command()
def serve(
    database: Annotated[
        str,
        typer.Option("--database", "-d", envvar="CLI_CONSUMPTION_DATABASE"),
    ] = "cli-consumption.sqlite",
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
    token_env: Annotated[
        str,
        typer.Option(
            help="Environment variable containing the ingestion bearer token."
        ),
    ] = "CLI_CONSUMPTION_API_TOKEN",
    read_token_env: Annotated[
        str,
        typer.Option(
            help="Environment variable containing the reporting read bearer token."
        ),
    ] = "CLI_CONSUMPTION_READ_TOKEN",
    export_token_env: Annotated[
        str,
        typer.Option(
            help="Environment variable containing the reporting export bearer token."
        ),
    ] = "CLI_CONSUMPTION_EXPORT_TOKEN",
    layout_token_env: Annotated[
        str,
        typer.Option(
            help="Environment variable containing the dashboard layout mutation token."
        ),
    ] = "CLI_CONSUMPTION_LAYOUT_TOKEN",
    front: Annotated[
        bool,
        typer.Option(help="Also run the bundled persistent Next.js dashboard."),
    ] = False,
    front_host: Annotated[
        str,
        typer.Option(help="Loopback address for the bundled dashboard."),
    ] = "127.0.0.1",
    front_port: Annotated[
        int,
        typer.Option(help="Port for the bundled dashboard."),
    ] = 3000,
    front_password_env: Annotated[
        str,
        typer.Option(
            help="Environment variable containing the dashboard login password."
        ),
    ] = "CLI_CONSUMPTION_DASHBOARD_PASSWORD",
) -> None:
    """Run the central HTTP collector and optionally its dashboard."""
    try:
        import uvicorn

        from cli_consumption.api import create_app
    except ModuleNotFoundError:
        raise typer.BadParameter(
            "serve requires FastAPI and Uvicorn; reinstall cli-consumption"
        ) from None

    if front and front_host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("--front-host must be a loopback address.")
    if front and port == front_port:
        raise typer.BadParameter("--port and --front-port must be different.")
    if not 1 <= port <= 65535 or not 1 <= front_port <= 65535:
        raise typer.BadParameter("Server ports must be between 1 and 65535.")

    token = os.environ.get(token_env)
    read_token = os.environ.get(read_token_env)
    export_token = os.environ.get(export_token_env)
    layout_token = os.environ.get(layout_token_env) or None
    if any(value == "" for value in (token, read_token, export_token)):
        raise typer.BadParameter(
            "Configured token environment variables must be non-empty."
        )
    if front:
        read_token = read_token or secrets.token_urlsafe(32)
        export_token = export_token or secrets.token_urlsafe(32)
        layout_token = layout_token or secrets.token_urlsafe(32)
    if (
        token is None
        and read_token is None
        and export_token is None
        and layout_token is None
        and host not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise typer.BadParameter(
            "Set at least one configured token environment variable before exposing "
            "the service beyond localhost."
        )
    if (
        token is None
        and read_token is None
        and export_token is None
        and layout_token is None
    ):
        typer.echo(
            "Warning: service authentication is disabled on localhost.", err=True
        )
    engine = _open_database(database)
    try:
        application = (
            create_app(engine, token)
            if read_token is None and export_token is None and layout_token is None
            else create_app(
                engine,
                token,
                read_token=read_token,
                export_token=export_token,
                layout_token=layout_token,
            )
        )
        if not front:
            uvicorn.run(application, host=host, port=port, access_log=False)
            return
        _serve_with_frontend(
            uvicorn,
            application,
            host=host,
            port=port,
            front_host=front_host,
            front_port=front_port,
            front_password_env=front_password_env,
            read_token=cast(str, read_token),
            export_token=cast(str, export_token),
            layout_token=cast(str, layout_token),
        )
    finally:
        engine.dispose()


def _serve_with_frontend(
    uvicorn: ModuleType,
    application: object,
    *,
    host: str,
    port: int,
    front_host: str,
    front_port: int,
    front_password_env: str,
    read_token: str,
    export_token: str,
    layout_token: str,
) -> None:
    from cli_consumption.frontend import (
        FrontendRuntimeError,
        find_node_runtime,
        frontend_environment,
        materialize_frontend_runtime,
        start_frontend,
        stop_frontend,
    )

    password = os.environ.get(front_password_env)
    if password is None:
        password = typer.prompt("Dashboard password", hide_input=True)
    if len(password) < 12:
        raise typer.BadParameter(
            f"{front_password_env} must contain at least 12 characters."
        )
    session_secret = os.environ.get("CLI_CONSUMPTION_SESSION_SECRET")
    if session_secret is not None and len(session_secret.encode("utf-8")) < 32:
        raise typer.BadParameter(
            "CLI_CONSUMPTION_SESSION_SECRET must contain at least 32 bytes."
        )
    session_secret = session_secret or secrets.token_urlsafe(32)
    api_host = {
        "0.0.0.0": "127.0.0.1",  # noqa: S104 - converts an explicit bind address
        "::": "::1",
    }.get(host, host)
    api_origin = _http_origin(api_host, port)
    front_origin = _http_origin(front_host, front_port)

    try:
        node = find_node_runtime()
        environment = frontend_environment(
            api_url=api_origin,
            origin=front_origin,
            host=front_host,
            port=front_port,
            password=password,
            read_token=read_token,
            export_token=export_token,
            layout_token=layout_token,
            session_secret=session_secret,
        )
        with materialize_frontend_runtime() as runtime:
            frontend = start_frontend(node, runtime, environment)
            typer.echo(f"Dashboard: {front_origin}")
            _supervise_servers(
                uvicorn,
                application,
                frontend,
                host=host,
                port=port,
                stop_frontend=stop_frontend,
            )
    except FrontendRuntimeError as error:
        messages = {
            "frontend_node_missing": "serve --front requires Node.js 20.9 or newer.",
            "frontend_node_invalid": "The Node.js runtime could not be validated.",
            "frontend_node_unsupported": (
                "serve --front requires Node.js 20.9 or newer."
            ),
            "frontend_runtime_missing": "The bundled dashboard runtime is missing.",
            "frontend_runtime_invalid": "The bundled dashboard runtime is invalid.",
            "frontend_start_failed": "The bundled dashboard could not be started.",
            "frontend_stop_failed": "The bundled dashboard could not be stopped.",
        }
        typer.echo(f"Error: {messages[str(error)]}", err=True)
        raise typer.Exit(1) from None


def _supervise_servers(
    uvicorn: ModuleType,
    application: object,
    frontend: subprocess.Popen[bytes],
    *,
    host: str,
    port: int,
    stop_frontend: Callable[[subprocess.Popen[bytes]], None],
) -> None:
    server = uvicorn.Server(
        uvicorn.Config(application, host=host, port=port, access_log=False)
    )
    failures: list[BaseException] = []

    def run_backend() -> None:
        try:
            server.run()
        except BaseException as error:
            failures.append(error)

    backend = threading.Thread(target=run_backend, name="cli-consumption-api")
    backend.start()
    frontend_failed = False
    try:
        while backend.is_alive() and frontend.poll() is None:
            backend.join(timeout=0.1)
        frontend_failed = frontend.poll() is not None and backend.is_alive()
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        stop_frontend(frontend)
        backend.join(timeout=10)
    if backend.is_alive() or failures:
        typer.echo("Error: The API server stopped unexpectedly.", err=True)
        raise typer.Exit(1)
    if frontend_failed:
        typer.echo("Error: The dashboard server stopped unexpectedly.", err=True)
        raise typer.Exit(1)


def _http_origin(host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


def _collect_incrementally(
    plans: list[_PlannedCollection],
    mappings: list[tuple[str, str]],
    database: str,
    *,
    strict: bool,
    json_output: bool,
    trigger: IncrementalTrigger,
) -> None:
    summaries: dict[str, IncrementalIngestion] = {}
    batched = {plan.spec.name for plan in plans if plan.batched}
    provider = plans[0].spec.name if plans else "all"

    def ingest(engine: Engine, batch: CollectionBatch) -> None:
        _ingest_batch(engine, batch, summaries, batched)

    failure: CollectionFailure | None = None
    if strict:
        with tempfile.TemporaryDirectory(prefix="cli-consumption-") as staging:
            staged: list[Path] = []
            staged_bytes = 0
            active_provider = provider
            try:
                for index, batch in enumerate(_iter_planned_batches(plans, mappings)):
                    snapshot = batch.snapshot
                    active_provider = snapshot.provider
                    if snapshot.malformed_records:
                        raise CollectionFailure(
                            snapshot.provider,
                            "malformed_records",
                            "--strict refused incremental snapshots containing "
                            "malformed provider records.",
                        )
                    validated = validate_snapshot(snapshot)
                    path = Path(staging) / f"{index:08d}.json"
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        writer = _BoundedStagingWriter(handle, staged_bytes)
                        json.dump(
                            {
                                "snapshot": validated.to_dict(),
                                "authoritative_subagent_scopes": (
                                    sorted(batch.authoritative_subagent_scopes)
                                    if batch.authoritative_subagent_scopes is not None
                                    else None
                                ),
                                "subagent_merge": batch.subagent_merge,
                            },
                            writer,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        staged_bytes = writer.consumed
                    staged.append(path)
            except CollectionFailure as error:
                failure = error
            except SnapshotValidationError as error:
                failure = _snapshot_failure(active_provider, error)
            except ProviderDataLimitError:
                failure = CollectionFailure(
                    active_provider,
                    "provider_limit_exceeded",
                    f"Provider {active_provider!r} data exceeds collection "
                    "safety limits.",
                )
            except OSError:
                failure = CollectionFailure(
                    active_provider,
                    "provider_collection_failed",
                    f"Provider {active_provider!r} collection failed.",
                )

            if failure is None:
                engine = _open_database(database)
                try:
                    for path in staged:
                        with path.open(encoding="utf-8") as handle:
                            payload = json.load(handle)
                        raw_scopes = payload["authoritative_subagent_scopes"]
                        ingest(
                            engine,
                            CollectionBatch(
                                Snapshot.from_dict(payload["snapshot"]),
                                (
                                    frozenset(
                                        (str(item[0]), str(item[1]))
                                        for item in raw_scopes
                                    )
                                    if raw_scopes is not None
                                    else None
                                ),
                                subagent_merge=payload["subagent_merge"] is True,
                            ),
                        )
                except CollectionFailure as error:
                    failure = error
                finally:
                    engine.dispose()
    else:
        engine = _open_database(database)
        try:
            try:
                for batch in _iter_planned_batches(plans, mappings):
                    ingest(engine, batch)
            except CollectionFailure as error:
                failure = error
        finally:
            engine.dispose()

    outcomes = list(summaries.values())
    if failure is not None:
        _abort_collection(
            failure,
            json_output=json_output,
            ingestions=outcomes,
            trigger=trigger,
        )
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "incremental": True,
                    "incremental_trigger": trigger,
                    "ingestions": outcomes,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if trigger == "automatic":
        typer.echo(
            "Aggregate collection limits exceeded; collected in bounded batches: "
            + ", ".join(sorted(batched))
            + "."
        )
    for summary in outcomes:
        if trigger == "automatic" and not summary["batched"]:
            typer.echo(
                f"Ingestion {summary['provider']}: "
                f"{summary['written']} written, {summary['skipped']} unchanged, "
                f"{summary['malformed']} malformed skipped."
            )
            continue
        typer.echo(
            f"Incremental ingestion {summary['provider']}: "
            f"{summary['batches']} batches, {summary['written']} written, "
            f"{summary['skipped']} unchanged, "
            f"{summary['malformed']} malformed skipped."
        )


def _ingest_batch(
    engine: Engine,
    batch: CollectionBatch,
    summaries: dict[str, IncrementalIngestion],
    batched: set[str],
) -> None:
    """Ingest one collection batch and add its counters to the provider summary."""
    snapshot = batch.snapshot
    try:
        result = ingest_snapshot(
            engine,
            snapshot,
            authoritative_subagent_scopes=batch.authoritative_subagent_scopes,
            subagent_merge=batch.subagent_merge,
        )
    except SnapshotValidationError as error:
        raise _snapshot_failure(snapshot.provider, error) from None
    summary = summaries.setdefault(
        snapshot.provider,
        {
            "provider": snapshot.provider,
            "batched": snapshot.provider in batched,
            "batches": 0,
            "received": 0,
            "written": 0,
            "skipped": 0,
            "malformed": 0,
            "batch_duplicates": 0,
        },
    )
    summary["batches"] += 1
    summary["received"] += result.received
    summary["written"] += result.written
    summary["skipped"] += result.skipped
    summary["malformed"] += snapshot.malformed_records
    summary["batch_duplicates"] += snapshot.duplicate_conversations


def _collection_inputs(
    provider: str,
    source_values: list[str] | None,
    project_values: list[str] | None,
) -> tuple[list[tuple[AdapterSpec, list[tuple[str, Path]]]], list[tuple[str, str]]]:
    spec = resolve_adapter_spec(provider) if provider != "all" else None
    if provider != "all" and spec is None:
        raise typer.BadParameter(
            f"Provider {provider!r} is not implemented yet. Run `providers` for status."
        )
    mappings = _parse_project_mappings(project_values or [])
    if spec is not None:
        return [(spec, _parse_sources(source_values or [], spec))], mappings

    inputs: list[tuple[AdapterSpec, list[tuple[str, Path]]]] = []
    if source_values:
        sources = _parse_source_values(source_values)
        matched_labels: set[str] = set()
        for candidate in ADAPTER_SPECS:
            matched = [
                source for source in sources if has_provider_data(candidate, source[1])
            ]
            if matched:
                matched_labels.update(label for label, _ in matched)
                inputs.append((candidate, matched))
        unmatched = [label for label, _ in sources if label not in matched_labels]
        if unmatched:
            raise typer.BadParameter(
                "No supported provider data detected for source labels: "
                + ", ".join(unmatched)
            )
    else:
        machine = platform.node()
        for candidate in ADAPTER_SPECS:
            path = default_source_path(candidate)
            if has_provider_data(candidate, path):
                inputs.append((candidate, [(machine, path)]))
    if not inputs:
        raise typer.BadParameter("No supported provider data detected.")
    return inputs, mappings


def _collect_snapshots(
    provider: str,
    source_values: list[str] | None,
    project_values: list[str] | None,
) -> list[Snapshot]:
    inputs, mappings = _collection_inputs(provider, source_values, project_values)
    return [_collect_adapter(spec, sources, mappings) for spec, sources in inputs]


def _plan_collection(
    provider: str,
    source_values: list[str] | None,
    project_values: list[str] | None,
    *,
    mode: Literal["forced", "automatic", "never"],
) -> tuple[list[_PlannedCollection], list[tuple[str, str]]]:
    """Collect each provider once, deferring to batches only where allowed.

    ``forced`` defers every provider to batch iteration without collecting it here.
    ``automatic`` collects normally and switches an incremental-capable provider to
    bounded batches only when it exceeds an aggregate candidate, read, or
    normalized-record limit. ``never`` keeps the all-or-nothing behavior.
    """
    inputs, mappings = _collection_inputs(provider, source_values, project_values)
    plans = [
        _plan_provider(spec, sources, mappings, mode=mode) for spec, sources in inputs
    ]
    return plans, mappings


def _plan_provider(
    spec: AdapterSpec,
    sources: list[tuple[str, Path]],
    mappings: list[tuple[str, str]],
    *,
    mode: Literal["forced", "automatic", "never"],
) -> _PlannedCollection:
    """Plan one provider; see ``_plan_collection`` for the modes."""
    capable = isinstance(spec.adapter_type(), IncrementalAdapter)
    if mode == "forced":
        return _PlannedCollection(spec, sources, None, capable)
    try:
        snapshot = _collect_adapter(
            spec, sources, mappings, switchable=capable and mode == "automatic"
        )
    except _AggregateLimitExceeded:
        return _PlannedCollection(spec, sources, None, True)
    return _PlannedCollection(spec, sources, snapshot, False)


def _iter_planned_batches(
    plans: list[_PlannedCollection],
    mappings: list[tuple[str, str]],
) -> Iterator[CollectionBatch]:
    batches = 0
    for plan in plans:
        spec = plan.spec
        try:
            if plan.snapshot is not None:
                batches_for_adapter: Iterator[CollectionBatch] = iter(
                    (CollectionBatch(plan.snapshot),)
                )
            else:
                adapter = spec.adapter_type()
                batches_for_adapter = (
                    adapter.collect_incrementally(plan.sources, mappings)
                    if plan.batched and isinstance(adapter, IncrementalAdapter)
                    else iter(
                        (CollectionBatch(adapter.collect(plan.sources, mappings)),)
                    )
                )
            for batch in batches_for_adapter:
                batches += 1
                if batches > MAX_INCREMENTAL_BATCHES:
                    raise ProviderDataLimitError(
                        "provider_incremental_batch_limit_exceeded"
                    )
                yield batch
        except ProviderDataLimitError:
            raise CollectionFailure(
                spec.name,
                "provider_limit_exceeded",
                f"Provider {spec.name!r} data exceeds collection safety limits.",
            ) from None
        except UnsupportedProviderFormat:
            raise CollectionFailure(
                spec.name,
                "provider_format_incompatible",
                f"Provider {spec.name!r} data format is incompatible.",
            ) from None
        except SnapshotValidationError as error:
            raise _snapshot_failure(spec.name, error) from None
        except Exception:
            raise CollectionFailure(
                spec.name,
                "provider_collection_failed",
                f"Provider {spec.name!r} collection failed.",
            ) from None


def _collect_adapter(
    spec: AdapterSpec,
    sources: list[tuple[str, Path]],
    mappings: list[tuple[str, str]],
    *,
    switchable: bool = False,
) -> Snapshot:
    try:
        return spec.adapter_type().collect(sources, mappings)
    except (ProviderDataLimitError, SnapshotValidationError) as error:
        if switchable and is_aggregate_limit(error):
            raise _AggregateLimitExceeded from None
        if isinstance(error, SnapshotValidationError):
            raise _snapshot_failure(spec.name, error) from None
        raise CollectionFailure(
            spec.name,
            "provider_limit_exceeded",
            f"Provider {spec.name!r} data exceeds collection safety limits.",
        ) from None
    except UnsupportedProviderFormat:
        raise CollectionFailure(
            spec.name,
            "provider_format_incompatible",
            f"Provider {spec.name!r} data format is incompatible.",
        ) from None
    except Exception:
        raise CollectionFailure(
            spec.name,
            "provider_collection_failed",
            f"Provider {spec.name!r} collection failed.",
        ) from None


def _snapshot_failure(
    provider: str, error: SnapshotValidationError
) -> CollectionFailure:
    if error.code == "snapshot_too_large":
        return CollectionFailure(
            provider,
            "provider_limit_exceeded",
            f"Provider {provider!r} data exceeds collection safety limits.",
        )
    return CollectionFailure(
        provider,
        "invalid_snapshot",
        f"Provider {provider!r} produced an invalid metadata snapshot.",
    )


def _parse_sources(
    values: list[str],
    spec: AdapterSpec,
) -> list[tuple[str, Path]]:
    if not values:
        values = [f"{platform.node()}={default_source_path(spec)}"]
    result = _parse_source_values(values)
    for _, path in result:
        if not has_provider_data(spec, path):
            expected = ", ".join(spec.markers)
            raise typer.BadParameter(
                f"Missing provider data ({expected}) under: {path}"
            )
    return result


def _parse_source_values(values: list[str]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    labels: set[str] = set()
    for index, value in enumerate(values, 1):
        if "=" in value:
            label, raw_path = value.split("=", 1)
        else:
            label, raw_path = f"machine-{index}", value
        label = label.strip()
        path = Path(raw_path).expanduser().resolve()
        if not label or label in labels:
            raise typer.BadParameter(
                f"Source labels must be non-empty and unique: {label!r}"
            )
        labels.add(label)
        result.append((label, path))
    return result


def _parse_project_mappings(values: list[str]) -> list[tuple[str, str]]:
    mappings: list[tuple[str, str]] = []
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(
                f"Project mapping must be NAME=PATH_PREFIX: {value!r}"
            )
        name, prefix = (part.strip() for part in value.split("=", 1))
        if not name or not prefix:
            raise typer.BadParameter(f"Invalid project mapping: {value!r}")
        mappings.append((name, prefix.rstrip("/\\")))
    return mappings
