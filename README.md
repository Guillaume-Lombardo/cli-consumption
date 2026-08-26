# CLI Consumption

CLI Consumption is a local-first Python package for measuring how AI coding CLIs use
models, tokens, tools, conversations, and turns. It can analyze one workstation,
consolidate copied data from several machines, or send metadata-only snapshots to a
central collector.

Codex, Pi, OpenCode, and the core Claude Code local transcript format are supported.
Kilo Code is planned behind the same provider-neutral adapter contract.

The collector deliberately excludes prompts, responses, tool arguments, and
credentials. See [Privacy](docs/privacy.md) before sharing a database or export.

## Quick start

The project requires Python 3.14 or newer and uses
[`uv`](https://docs.astral.sh/uv/) for every development and execution workflow.

From a checkout:

```bash
uv sync
uv run cli-consumption collect
uv run cli-consumption export --output reports
```

Open `reports/dashboard.html` locally. The generated dashboard is self-contained and
makes no network requests. Detailed normalized CSV tables are opt-in with `--csv`.

The dashboard supports time, provider, machine, project, and model filters. It reports
period-over-period activity, token composition, cache efficiency, turn latency and
duration distributions, technical throughput, context pressure, content-free work-item
duration and reliability, configuration cohorts, compactions, subagent delegation, and
ingestion quality. Token events are local usage metadata rather than billing records,
and a technically completed turn is not a measure of task quality or productivity.

For a dashboard that is safer to share, pseudonymize
machine, project, model, and role labels, group tool names, round timestamps to days,
and hide small rows in the cohort-comparison view:

```bash
uv run cli-consumption export --output shared-report --share-safe
```

Share-safe reports still disclose aggregate work patterns and must be treated as
private operational data.

From PyPI, the CLI can run without a permanent installation:

```bash
uv tool run cli-consumption collect
uv tool run cli-consumption export --output reports
```

To run the latest unreleased GitHub source instead:

```bash
uv tool run --from git+https://github.com/Guillaume-Lombardo/cli-consumption \
  cli-consumption providers
```

## Collect from one or more machines

With no source option, `collect` reads `~/.codex` and labels it with the local
hostname:

```bash
uv run cli-consumption collect --database usage.sqlite
```

Select Claude Code to read `~/.claude/projects/` instead:

```bash
uv run cli-consumption collect --provider claude --database usage.sqlite
```

Select OpenCode to read `~/.local/share/opencode/opencode.db` instead:

```bash
uv run cli-consumption collect --provider opencode --database usage.sqlite
```

Select Pi to read `~/.pi/agent/sessions/` instead:

```bash
uv run cli-consumption collect --provider pi --database usage.sqlite
```

Use `all` to detect and collect every supported provider present on the machine:

```bash
uv run cli-consumption collect --provider all --database usage.sqlite
```

With explicit copied sources, each path is inspected for the provider-specific
`sessions/` or `projects/` directory. Sources that contain no supported provider data
are rejected instead of silently skipped.

For an offline multi-machine workflow, copy only each machine's Codex `sessions/`
directory into a trusted analysis location. Do not copy `auth.json` or other
credentials. Then repeat `--source`:

```bash
uv run cli-consumption collect \
  --source desktop=/data/codex/desktop \
  --source laptop=/data/codex/laptop \
  --source server=/data/codex/server \
  --database usage.sqlite
```

Globally identical conversation IDs are deduplicated. If copies differ, the most
complete rollout wins. Explicit project mappings use the longest matching original
working-directory prefix:

```bash
uv run cli-consumption collect \
  --source desktop=/data/codex/desktop \
  --project cli-consumption=/home/me/dev/cli-consumption
```

Copied Claude Code sources point to the configuration directory containing `projects/`:

```bash
uv run cli-consumption collect --provider claude \
  --source desktop=/data/claude/desktop \
  --source laptop=/data/claude/laptop
```

Copied OpenCode sources point to the data directory containing `opencode.db`:

```bash
uv run cli-consumption collect --provider opencode \
  --source desktop=/data/opencode/desktop \
  --source laptop=/data/opencode/laptop
```

Copied Pi sources point to the agent directory containing `sessions/`:

```bash
uv run cli-consumption collect --provider pi \
  --source desktop=/data/pi/desktop \
  --source laptop=/data/pi/laptop
```

## SQLite and PostgreSQL

A file path selects SQLite. A SQLAlchemy URL selects PostgreSQL:

```bash
uv run cli-consumption collect --database usage.sqlite
uv run cli-consumption collect \
  --database postgresql+psycopg://usage@localhost/cli_consumption
```

Pass credentials through environment variables or a secret manager rather than shell
history. `CLI_CONSUMPTION_DATABASE` can supply the database setting.

## Central collector API

Offline imports are the simplest choice for personal use and air-gapped machines. A
central API is useful for recurring collection across machines.

Start the collector locally:

```bash
export CLI_CONSUMPTION_API_TOKEN="$(your-secret-provider)"
uv run cli-consumption serve \
  --database postgresql+psycopg://usage@localhost/cli_consumption \
  --host 0.0.0.0
```

Send a snapshot from another machine:

```bash
export CLI_CONSUMPTION_API_TOKEN="$(your-secret-provider)"
uv run cli-consumption sync --endpoint https://usage.example.test
```

The application refuses to bind beyond localhost without a token. Production
deployments must also place TLS and normal operational controls in front of the ASGI
server. See [Architecture](docs/architecture.md) for trade-offs.

## Commands

```text
cli-consumption collect    Collect local or copied provider data into SQL
cli-consumption sync       Collect and send a snapshot to a central API
cli-consumption serve      Run the central collection API
cli-consumption export     Write the HTML dashboard and optional CSV tables
cli-consumption providers  Show supported and planned providers
```

Run `uv run cli-consumption COMMAND --help` for every option.

## Development

```bash
uv sync --all-groups
uv run pre-commit install
uv run pre-commit run --all-files
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov --cov-report=term-missing
uv build
```

Development follows short-lived branches and squash-merged pull requests into a
protected `main`. Read [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md)
before changing the project.

## License

Licensed under the [Apache License 2.0](LICENSE). It provides permissive reuse plus an
explicit patent grant and contribution protections appropriate for an extensible
developer tool.
