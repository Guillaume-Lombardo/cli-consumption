from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Annotated

import typer

from cli_consumption import __version__
from cli_consumption.adapters import CodexAdapter
from cli_consumption.api import create_app
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.models import Snapshot
from cli_consumption.storage import (
    create_database_engine,
    ingest_snapshot,
    initialize_database,
)
from cli_consumption.sync import send_snapshot

app = typer.Typer(
    name="cli-consumption",
    help="Analyze AI coding CLI consumption without exporting conversation content.",
    no_args_is_help=True,
)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True),
    ] = None,
) -> None:
    """Collect locally, consolidate offline, or send snapshots to an API."""


@app.command()
def providers() -> None:
    """Show implemented and planned CLI adapters."""
    typer.echo("codex    supported")
    for provider in ("claude", "opencode", "kilo", "pi"):
        typer.echo(f"{provider:<8} planned")


@app.command()
def collect(
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help="[LABEL=]CODEX_HOME. Repeat to consolidate copied machine data.",
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
    provider: Annotated[str, typer.Option(help="CLI provider to collect.")] = "codex",
    project: Annotated[
        list[str] | None,
        typer.Option(
            "--project",
            help="NAME=PATH_PREFIX project mapping. Longest matching prefix wins.",
        ),
    ] = None,
) -> None:
    """Collect one or more local/copied CLI data directories into SQL storage."""
    snapshot = _collect_snapshot(provider, source, project)
    engine = create_database_engine(database)
    try:
        result = ingest_snapshot(engine, snapshot)
    finally:
        engine.dispose()
    typer.echo(
        f"Ingestion {result.run_id}: {result.written} written, "
        f"{result.skipped} unchanged, {snapshot.malformed_records} malformed skipped."
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
        typer.Option("--source", "-s", help="[LABEL=]CODEX_HOME. Repeat as needed."),
    ] = None,
    provider: Annotated[str, typer.Option(help="CLI provider to collect.")] = "codex",
    project: Annotated[
        list[str] | None,
        typer.Option("--project", help="NAME=PATH_PREFIX project mapping."),
    ] = None,
    token_env: Annotated[
        str,
        typer.Option(help="Environment variable containing the API bearer token."),
    ] = "CLI_CONSUMPTION_API_TOKEN",
) -> None:
    """Collect locally and send metadata-only records to a central collector."""
    snapshot = _collect_snapshot(provider, source, project)
    token = os.environ.get(token_env)
    result = send_snapshot(snapshot, endpoint, token)
    typer.echo(
        f"Remote ingestion {result['run_id']}: {result['written']} written, "
        f"{result['skipped']} unchanged."
    )


@app.command("export")
def export_command(
    database: Annotated[
        str,
        typer.Option("--database", "-d", envvar="CLI_CONSUMPTION_DATABASE"),
    ] = "cli-consumption.sqlite",
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("reports"),
    dashboard: Annotated[bool, typer.Option("--dashboard/--no-dashboard")] = True,
) -> None:
    """Export normalized SQL tables to CSV and a self-contained HTML dashboard."""
    engine = create_database_engine(database)
    try:
        initialize_database(engine)
        paths = export_csv(engine, output)
        if dashboard:
            dashboard_path = output / "dashboard.html"
            generate_dashboard(engine, dashboard_path)
            paths.append(dashboard_path)
    finally:
        engine.dispose()
    typer.echo(f"Wrote {len(paths)} files to {output.resolve()}")


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
    import uvicorn

    token = os.environ.get(token_env)
    if token is None and host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter(
            f"Set {token_env} before exposing the collector beyond localhost."
        )
    if token is None:
        typer.echo(
            "Warning: collector authentication is disabled on localhost.", err=True
        )
    engine = create_database_engine(database)
    uvicorn.run(create_app(engine, token), host=host, port=port)


def _collect_snapshot(
    provider: str,
    source_values: list[str] | None,
    project_values: list[str] | None,
) -> Snapshot:
    if provider != "codex":
        raise typer.BadParameter(
            f"Provider {provider!r} is not implemented yet. Run `providers` for status."
        )
    return CodexAdapter().collect(
        _parse_sources(source_values or []),
        _parse_project_mappings(project_values or []),
    )


def _parse_sources(values: list[str]) -> list[tuple[str, Path]]:
    if not values:
        values = [f"{platform.node()}={Path.home() / '.codex'}"]
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
        if not (path / "sessions").is_dir():
            raise typer.BadParameter(f"Missing sessions directory: {path / 'sessions'}")
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
