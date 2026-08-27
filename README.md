# CLI Consumption

CLI Consumption measures how AI coding CLIs use models, tokens, tools,
conversations, and turns. It runs locally, can consolidate copied data from several
machines, and can send metadata-only snapshots to a central collector.

It never stores prompts, responses, tool arguments, credentials, or raw provider
events. Local token counters are usage metadata, not billing records. Read the
[privacy boundary](docs/privacy.md) before sharing a database or report.

## Quick start

CLI Consumption requires Python 3.14 or newer and uses
[`uv`](https://docs.astral.sh/uv/).

From a checkout, collect every supported CLI detected on the machine and generate a
self-contained dashboard:

```bash
uv sync
uv run cli-consumption collect --provider all
uv run cli-consumption export --output reports
```

Open `reports/dashboard.html` locally. It makes no network requests. Detailed
normalized CSV tables are generated only when `--csv` is passed.

To collect a single CLI, use its provider name. For example, with Codex:

```bash
uv run cli-consumption collect --provider codex --database usage.sqlite
```

From PyPI, no permanent installation is required:

```bash
uv tool run cli-consumption collect --provider all
uv tool run cli-consumption export --output reports
```

To run the latest unreleased GitHub source:

```bash
uv tool run --from git+https://github.com/Guillaume-Lombardo/cli-consumption \
  cli-consumption providers
```

## Supported CLIs

`--provider all` detects the supported data stores found in their default locations.
Use the provider name below with `--provider` to select one CLI explicitly.

| CLI | Provider name | Default local source | Particularities and limits |
| --- | --- | --- | --- |
| Aider | `aider` | `~/.aider/analytics.jsonl` | Requires opt-in analytics logging; no projects, tools, cache/reasoning split, or provider-reported durations. |
| Amazon Q Developer CLI | `amazon-q` | `~/.local/share/amazon-q/data.sqlite3` | Persistent conversations only; request timing is available, but token counters are not. |
| Amp | `amp` | `~/.local/share/amp/threads/` | Per-inference tokens and context windows; no subthreads, compactions, reasoning split, or latency. |
| Claude Code | `claude` | `~/.claude/projects/` | Main sessions, tokens, tools, and compactions; no subagents, context windows, or provider-reported durations. `claude-code` is accepted as an alias. |
| Cline CLI | `cline` | `~/.cline/data/sessions/sessions.db` | Uses the session index and message artifacts; no costs or arbitrary task metadata. |
| Codex | `codex` | `~/.codex/sessions/` | Richest support: timing, context pressure, settings, compactions, work items, and subagent relationships. |
| Continue CLI | `continue` | `~/.continue/sessions/` | Token usage when present; session files lack reliable per-message timing and duration. |
| Crush | `crush` | `~/.local/share/crush/` | Reads registered per-project SQLite stores; token counters are a latest-context snapshot, not additive usage. |
| Cursor CLI | `cursor` | `~/.cursor/` | Composer 2 transcripts and chat metadata; no per-message time or tokens, and model attribution is incomplete. |
| Gemini CLI | `gemini` | `~/.gemini/tmp/` | Replays active history and rewinds; hashed projects are not reversed and nested agents are excluded. |
| GitHub Copilot CLI | `copilot` | `~/.copilot/session-state/` | Tokens are latest shutdown aggregates and cannot be assigned to individual turns. |
| Goose | `goose` | `~/.local/share/goose/sessions/sessions.db` | Supports SQLite schema v16; no legacy JSONL, subagents, reasoning tokens, or latency. |
| Grok Build | `grok` | `~/.grok/sessions/` | Per-prompt aggregates, reasoning effort, TTFT, and auto-compactions; no costs or subagent relationships. |
| Kilo Code | `kilo` | `~/.local/share/kilo/kilo.db` | CLI SQLite store only; excludes legacy IDE tasks, cloud sessions, subagents, context windows, and costs. |
| Kimi Code CLI | `kimi` | `~/.kimi/sessions/` | Wire v1 events, context windows, and compactions; selected model is not persisted and is reported as `unknown`. |
| OpenCode | `opencode` | `~/.local/share/opencode/opencode.db` | SQLite v2 only; no legacy storage, child sessions, context windows, or costs. |
| OpenHands CLI | `openhands` | `~/.openhands/conversations/` | SDK persistence with context windows, reasoning effort, and condensations; excludes cloud-only conversations and delegates. |
| Pi | `pi` | `~/.pi/agent/sessions/` | Counts all persisted branches; no branch relationships, custom-directory auto-detection, context windows, or provider-reported durations. |
| Plandex | `plandex` | `/plandex-server` | Requires an offline copy of a self-hosted `PLANDEX_BASE_DIR`; hosted accounts are not accessed, and models/tools are unavailable. |
| Qwen Code | `qwen` | `~/.qwen/projects/` | Follows the active branch and records context windows and compactions; excludes archived and sidechain sessions. |

Provider formats are internal and can change without notice. The detailed extraction
rules and qualification versions are documented in
[Provider support](docs/provider-support.md).

## Collect copied data

`--source [LABEL=]PATH` points to a provider home directory and can be repeated. With
`--provider all`, each path is inspected and unmatched sources are rejected. With one
provider selected, each path must contain that provider's expected store.

For example, consolidate trusted copies of Codex data from several machines:

```bash
uv run cli-consumption collect --provider codex \
  --source desktop=/data/codex/desktop \
  --source laptop=/data/codex/laptop \
  --source server=/data/codex/server \
  --database usage.sqlite
```

Copy only the required provider data. For Codex, copy the `sessions/` directory but
never `auth.json` or other credentials. Globally identical conversation IDs are
deduplicated, and the most complete copy wins.

Map original working-directory prefixes to stable project labels with repeated
`--project NAME=PATH_PREFIX` options. The longest matching prefix wins:

```bash
uv run cli-consumption collect --provider codex \
  --source desktop=/data/codex/desktop \
  --project cli-consumption=/home/me/dev/cli-consumption
```

Plandex auto-detection checks `/plandex-server`. For any other trusted offline copy of
a self-hosted server data directory, pass the path explicitly:

```bash
uv run cli-consumption collect --provider plandex \
  --source server=/srv/plandex-server --database usage.sqlite
```

## Explore and share reports

The dashboard can filter by time, provider, machine, project, and model. It reports
activity, token composition, cache efficiency, latency and duration distributions,
technical throughput, context pressure, work-item reliability, configuration cohorts,
compactions, subagent delegation, and ingestion quality. Availability varies by
provider, as summarized in the table above.

Generate a more shareable dashboard by pseudonymizing labels, grouping tool names,
rounding timestamps to days, and hiding small cohort rows:

```bash
uv run cli-consumption export --output shared-report --share-safe
```

Share-safe reports still disclose aggregate work patterns and remain private
operational data. A technically completed turn is not a measure of task quality or
productivity.

## SQLite and PostgreSQL

A file path selects SQLite. A SQLAlchemy URL selects PostgreSQL:

```bash
uv run cli-consumption collect --provider all --database usage.sqlite
uv run cli-consumption collect --provider all \
  --database postgresql+psycopg://usage@localhost/cli_consumption
```

Pass credentials through environment variables or a secret manager rather than shell
history. `CLI_CONSUMPTION_DATABASE` can provide the database setting.

## Central collector API

Copied files are simplest for personal or air-gapped use. For recurring collection
across machines, start the metadata-only API:

```bash
export CLI_CONSUMPTION_API_TOKEN="$(your-secret-provider)"
uv run cli-consumption serve \
  --database postgresql+psycopg://usage@localhost/cli_consumption \
  --host 0.0.0.0
```

Then send all locally detected snapshots from another machine:

```bash
export CLI_CONSUMPTION_API_TOKEN="$(your-secret-provider)"
uv run cli-consumption sync --provider all \
  --endpoint https://usage.example.test
```

The application refuses to bind beyond localhost without a token. Production
deployments also need TLS and standard operational controls. See
[Architecture](docs/architecture.md) for the trade-offs.

## Commands

| Command | Purpose |
| --- | --- |
| `collect` | Collect local or copied provider data into SQL. |
| `sync` | Collect and send metadata-only snapshots to a central API. |
| `serve` | Run the central collection API. |
| `export` | Write the HTML dashboard and optional CSV tables. |
| `providers` | List provider names and support status. |

Run `uv run cli-consumption COMMAND --help` for all options.

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

Development uses short-lived branches and squash-merged pull requests into protected
`main`. Read [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) before
changing the project.

## License

Licensed under the [Apache License 2.0](LICENSE).
