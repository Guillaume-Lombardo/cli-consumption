from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Annotated

import typer

from cli_consumption import __version__
from cli_consumption.adapters import (
    AiderAdapter,
    AmpAdapter,
    ClaudeAdapter,
    CodexAdapter,
    ContinueAdapter,
    CopilotAdapter,
    CrushAdapter,
    CursorAdapter,
    GeminiAdapter,
    GooseAdapter,
    GrokAdapter,
    KiloAdapter,
    OpenCodeAdapter,
    PiAdapter,
    QwenAdapter,
)
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
    typer.echo("all      auto-detect supported providers")
    typer.echo("aider    supported")
    typer.echo("amp      supported")
    typer.echo("codex    supported")
    typer.echo("copilot  supported")
    typer.echo("continue supported")
    typer.echo("crush    supported")
    typer.echo("cursor   supported")
    typer.echo("gemini   supported")
    typer.echo("goose    supported")
    typer.echo("grok     supported")
    typer.echo("claude   supported")
    typer.echo("kilo     supported")
    typer.echo("opencode supported")
    typer.echo("pi       supported")
    typer.echo("qwen     supported")


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
) -> None:
    """Collect one or more local/copied CLI data directories into SQL storage."""
    snapshots = _collect_snapshots(provider, source, project)
    engine = create_database_engine(database)
    try:
        results = [
            (snapshot, ingest_snapshot(engine, snapshot)) for snapshot in snapshots
        ]
    finally:
        engine.dispose()
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
) -> None:
    """Collect locally and send metadata-only records to a central collector."""
    snapshots = _collect_snapshots(provider, source, project)
    token = os.environ.get(token_env)
    for snapshot in snapshots:
        result = send_snapshot(snapshot, endpoint, token)
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
) -> None:
    """Write a self-contained HTML dashboard and optional detailed CSV tables."""
    if share_safe and not dashboard:
        raise typer.BadParameter("--share-safe requires --dashboard")
    if share_safe and csv_exports:
        raise typer.BadParameter("--share-safe cannot be combined with --csv")
    if not dashboard and not csv_exports:
        raise typer.BadParameter("enable --dashboard or --csv")
    if (
        share_safe
        and output.is_dir()
        and any(path.name != "dashboard.html" for path in output.iterdir())
    ):
        raise typer.BadParameter(
            "--share-safe output directory must be empty or contain only dashboard.html"
        )
    engine = create_database_engine(database)
    try:
        initialize_database(engine)
        paths = export_csv(engine, output) if csv_exports else []
        if dashboard:
            dashboard_path = output / "dashboard.html"
            generate_dashboard(engine, dashboard_path, share_safe=share_safe)
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


def _collect_snapshots(
    provider: str,
    source_values: list[str] | None,
    project_values: list[str] | None,
) -> list[Snapshot]:
    provider = "claude" if provider == "claude-code" else provider
    adapters = {
        "aider": (AiderAdapter, ".aider", "analytics.jsonl"),
        "amp": (AmpAdapter, ".local/share/amp", "threads"),
        "codex": (CodexAdapter, ".codex", "sessions"),
        "copilot": (CopilotAdapter, ".copilot", "session-state"),
        "continue": (ContinueAdapter, ".continue", "sessions"),
        "crush": (
            CrushAdapter,
            ".local/share/crush",
            ("projects.json", "crush.db", ".crush/crush.db"),
        ),
        "cursor": (
            CursorAdapter,
            ".cursor",
            ("chats", "projects/*/agent-transcripts"),
        ),
        "gemini": (GeminiAdapter, ".gemini", "tmp"),
        "goose": (GooseAdapter, ".local/share/goose/sessions", "sessions.db"),
        "grok": (GrokAdapter, ".grok", "sessions/*/*/summary.json"),
        "claude": (ClaudeAdapter, ".claude", "projects/*/*.jsonl"),
        "kilo": (KiloAdapter, ".local/share/kilo", "kilo.db"),
        "opencode": (
            OpenCodeAdapter,
            ".local/share/opencode",
            "opencode.db",
        ),
        "pi": (PiAdapter, ".pi/agent", "sessions"),
        "qwen": (QwenAdapter, ".qwen", "projects/*/chats"),
    }
    if provider != "all" and provider not in adapters:
        raise typer.BadParameter(
            f"Provider {provider!r} is not implemented yet. Run `providers` for status."
        )
    mappings = _parse_project_mappings(project_values or [])
    if provider != "all":
        adapter, home, markers = adapters[provider]
        return [
            adapter().collect(
                _parse_sources(source_values or [], home, markers), mappings
            )
        ]

    snapshots: list[Snapshot] = []
    if source_values:
        sources = _parse_source_values(source_values)
        matched_labels: set[str] = set()
        for adapter, _, markers in adapters.values():
            matched = [
                source for source in sources if _has_provider_data(source[1], markers)
            ]
            if matched:
                matched_labels.update(label for label, _ in matched)
                snapshots.append(adapter().collect(matched, mappings))
        unmatched = [label for label, _ in sources if label not in matched_labels]
        if unmatched:
            raise typer.BadParameter(
                "No supported provider data detected for source labels: "
                + ", ".join(unmatched)
            )
    else:
        machine = platform.node()
        for adapter, home, markers in adapters.values():
            path = (Path.home() / home).resolve()
            if _has_provider_data(path, markers):
                snapshots.append(adapter().collect([(machine, path)], mappings))
    if not snapshots:
        raise typer.BadParameter("No supported provider data detected.")
    return snapshots


def _parse_sources(
    values: list[str],
    home: str = ".codex",
    markers: str | tuple[str, ...] = "sessions",
) -> list[tuple[str, Path]]:
    if not values:
        values = [f"{platform.node()}={Path.home() / home}"]
    result = _parse_source_values(values)
    for _, path in result:
        if not _has_provider_data(path, markers):
            expected = ", ".join(_markers(markers))
            raise typer.BadParameter(
                f"Missing provider data ({expected}) under: {path}"
            )
    return result


def _markers(value: str | tuple[str, ...]) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else value


def _has_provider_data(path: Path, markers: str | tuple[str, ...]) -> bool:
    return any(
        any(path.glob(marker)) if "*" in marker else (path / marker).exists()
        for marker in _markers(markers)
    )


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
