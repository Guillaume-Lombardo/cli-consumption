from __future__ import annotations

import json
import os
import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.engine import Engine

from cli_consumption import __version__
from cli_consumption.adapters._shared import ProviderDataLimitError
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
from cli_consumption.storage import (
    MissingOptionalDependencyError,
    create_database_engine,
    ingest_snapshot,
    initialize_database,
)

app = typer.Typer(
    name="cli-consumption",
    help="Analyze AI coding CLI consumption without exporting conversation content.",
    no_args_is_help=True,
)


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
    """Show implemented and planned CLI adapters."""
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
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a deterministic JSON result.")
    ] = False,
) -> None:
    """Collect one or more local/copied CLI data directories into SQL storage."""
    snapshots = _collect_snapshots(provider, source, project)
    if strict and any(snapshot.malformed_records for snapshot in snapshots):
        raise typer.BadParameter(
            "--strict refused snapshots containing malformed provider records"
        )
    engine = _open_database(database)
    try:
        results = [
            (snapshot, ingest_snapshot(engine, snapshot)) for snapshot in snapshots
        ]
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
) -> None:
    """Collect locally and send metadata-only records to a central collector."""
    try:
        from cli_consumption.sync import send_snapshot
    except ModuleNotFoundError:
        raise typer.BadParameter(
            "sync requires optional dependencies; install cli-consumption[sync]"
        ) from None

    snapshots = _collect_snapshots(provider, source, project)
    token = os.environ.get(token_env)
    for snapshot in snapshots:
        try:
            result = send_snapshot(
                snapshot, endpoint, token, allow_insecure=allow_insecure
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None
        typer.echo(
            f"Remote ingestion {snapshot.provider} {result['run_id']}: "
            f"{result['written']} written, {result['skipped']} unchanged."
        )


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
                raise typer.BadParameter(
                    "dashboard exceeds safe generation limits; narrow the export "
                    "with --since and/or --until"
                ) from None
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
        typer.Option(help="Environment variable containing the accepted bearer token."),
    ] = "CLI_CONSUMPTION_API_TOKEN",
) -> None:
    """Run the optional central HTTP collector."""
    try:
        import uvicorn

        from cli_consumption.api import create_app
    except ModuleNotFoundError:
        raise typer.BadParameter(
            "serve requires optional dependencies; install cli-consumption[server]"
        ) from None

    token = os.environ.get(token_env)
    if token is None and host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter(
            f"Set {token_env} before exposing the collector beyond localhost."
        )
    if token is None:
        typer.echo(
            "Warning: collector authentication is disabled on localhost.", err=True
        )
    engine = _open_database(database)
    uvicorn.run(create_app(engine, token), host=host, port=port)


def _collect_snapshots(
    provider: str,
    source_values: list[str] | None,
    project_values: list[str] | None,
) -> list[Snapshot]:
    spec = resolve_adapter_spec(provider) if provider != "all" else None
    if provider != "all" and spec is None:
        raise typer.BadParameter(
            f"Provider {provider!r} is not implemented yet. Run `providers` for status."
        )
    mappings = _parse_project_mappings(project_values or [])
    if spec is not None:
        return [
            _collect_adapter(spec, _parse_sources(source_values or [], spec), mappings)
        ]

    snapshots: list[Snapshot] = []
    if source_values:
        sources = _parse_source_values(source_values)
        matched_labels: set[str] = set()
        for candidate in ADAPTER_SPECS:
            matched = [
                source for source in sources if has_provider_data(candidate, source[1])
            ]
            if matched:
                matched_labels.update(label for label, _ in matched)
                snapshots.append(_collect_adapter(candidate, matched, mappings))
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
                snapshots.append(
                    _collect_adapter(candidate, [(machine, path)], mappings)
                )
    if not snapshots:
        raise typer.BadParameter("No supported provider data detected.")
    return snapshots


def _collect_adapter(
    spec: AdapterSpec,
    sources: list[tuple[str, Path]],
    mappings: list[tuple[str, str]],
) -> Snapshot:
    try:
        return spec.adapter_type().collect(sources, mappings)
    except (ProviderDataLimitError, SnapshotValidationError):
        raise typer.BadParameter(
            f"Provider {spec.name!r} data exceeds collection safety limits."
        ) from None


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
