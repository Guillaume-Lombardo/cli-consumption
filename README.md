# CLI Consumption

CLI Consumption turns local AI coding CLI metadata into one private, self-contained
view of models, tokens, tools, conversations, turns, context pressure, and workflow
health. It can consolidate trusted offline copies from several machines or send
metadata-only snapshots to a central collector.

It never stores prompts, responses, tool arguments, credentials, or raw provider
events. Local token counters are usage metadata, not billing records. Read the
[privacy boundary](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/privacy.md)
before sharing a database or report.

[![Synthetic CLI Consumption dashboard](https://raw.githubusercontent.com/Guillaume-Lombardo/cli-consumption/main/docs/demo/dashboard.png)](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/demo/dashboard.html)

The preview and
[self-contained demo dashboard](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/demo/dashboard.html)
contain only
deterministic synthetic records. Rebuild the HTML with:

```bash
uv run python docs/demo/generate.py
```

The generator never reads provider directories and creates its temporary SQLite
database outside the repository. The demo HTML, image, documentation, tests, and other
repository assets are excluded from Python distribution artifacts.

## Installation

CLI Consumption requires Python 3.12 or newer. Run it directly from PyPI with `uv`:

```bash
uv tool run cli-consumption providers
```

The default package covers local collection, SQLite storage, and exports. Install only
the optional runtime capabilities you use:

- `cli-consumption[sync]` for the sync client;
- `cli-consumption[server]` for the collector service;
- `cli-consumption[postgres]` for PostgreSQL.

Extras can be combined, for example `cli-consumption[server,postgres]` on a central
collector.

## Quick start

From a checkout, detect supported local CLIs, collect their metadata, and create an
offline dashboard:

```bash
uv sync --all-extras
uv run cli-consumption collect --provider all
uv run cli-consumption export --output reports
```

Open `reports/dashboard.html` locally. It makes no network requests. Detailed CSV
tables are generated only when `--csv` is passed.

To collect one provider or select another database:

```bash
uv run cli-consumption collect --provider codex --database usage.sqlite
```

Use `--source [LABEL=]PATH` for trusted offline copies and repeated
`--project NAME=PATH_PREFIX` mappings for stable project labels. See the
[usage and operations guide](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/usage.md)
for multi-machine collection, reporting,
PostgreSQL, retention, synchronization, readiness, and automation.

## Supported CLIs

The current registry supports Aider, Amazon Q Developer CLI, Amp, Claude Code, Cline,
Codex, Continue, Crush, Cursor CLI, Gemini CLI, GitHub Copilot CLI, Goose, Grok Build,
Kilo Code, Kimi Code CLI, Mistral Vibe CLI, OpenCode, OpenHands CLI, Pi, Plandex, and
Qwen Code.

Provider formats are internal and can change without notice. Exact source locations,
token semantics, extraction limits, synthetic qualification fixtures, provenance, and
known gaps live in the
[provider support ledger](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/provider-support.md).
Inspect the
local registry without exposing paths or content:

```bash
uv run cli-consumption providers --json
```

## Essential limits

- Provider files are untrusted. Collection enforces discovery, file, line, SQLite row,
  structured-field, and total normalized-record limits; direct provider-file symlinks
  are refused.
- `collect --strict` and `sync --strict` refuse a batch when any provider skipped
  malformed records. Machine-readable results use fixed error codes without paths,
  payloads, response bodies, tokens, or exception text.
- Dashboards are self-contained and network-free. Share-safe mode pseudonymizes
  labels, groups tools, rounds timestamps, and hides small cohorts, but still exposes
  aggregate work patterns.
- CSV and normalized databases remain detailed private operational data. Never treat a
  technically completed turn as a measure of quality or productivity.
- Central collection requires TLS, token rotation, backups, limits, monitoring, and a
  reverse proxy. The application does not replace those operator controls.

See [Privacy](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/privacy.md)
for the exact data boundary and
[Architecture](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/architecture.md)
for ingestion, idempotency, migrations, report limits, and collector behavior.

## Commands

| Command | Purpose |
| --- | --- |
| `collect` | Collect local or copied provider data into SQL. |
| `sync` | Collect and send metadata-only snapshots to a central API. |
| `serve` | Run the central collection API. |
| `export` | Write the HTML dashboard and optional CSV tables. |
| `providers` | List provider names and compatibility status. |
| `retention` | Preview or apply deletion outside a retention window. |

Run `uv run cli-consumption COMMAND --help` for every option. The
[usage guide](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/usage.md)
contains copy-ready examples.

## Documentation

- [Usage and operations](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/usage.md)
- [Provider support and qualification](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/provider-support.md)
- [Privacy boundary](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/privacy.md)
- [Architecture](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/architecture.md)
- [Roadmap](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/roadmap.md)
- [Contributing](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/CONTRIBUTING.md)
- [Security policy](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/SECURITY.md)

## Development

```bash
uv sync --all-extras --all-groups
uv run pre-commit install
uv run pre-commit run --all-files
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest --cov --cov-report=term-missing
uv build
```

Development uses short-lived branches and squash-merged pull requests into protected
`main`. Read
[CONTRIBUTING.md](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/CONTRIBUTING.md)
and [AGENTS.md](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/AGENTS.md)
before changing the project.

## License

Licensed under the
[Apache License 2.0](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/LICENSE).
