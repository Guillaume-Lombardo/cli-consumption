# CLI Consumption

CLI Consumption measures how AI coding CLIs use models, tokens, tools,
conversations, and turns. It runs locally, can consolidate copied data from several
machines, and can send metadata-only snapshots to a central collector.

It never stores prompts, responses, tool arguments, credentials, or raw provider
events. Local token counters are usage metadata, not billing records. Read the
[privacy boundary](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/privacy.md)
before sharing a database or report.

## Quick start

CLI Consumption requires Python 3.12 or newer and uses
[`uv`](https://docs.astral.sh/uv/).
Supporting 3.12–3.14 keeps the package usable on more existing development and CI
images without changing its architecture; the compatibility matrix exercises all
three versions.

From a checkout, collect every supported CLI detected on the machine and generate a
self-contained dashboard:

```bash
uv sync --all-extras
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

The default installation covers local collection, SQLite storage, and exports. Install
only the optional runtime capabilities you use: `cli-consumption[sync]` for the sync
client, `cli-consumption[server]` for the collector service, and
`cli-consumption[postgres]` for PostgreSQL. Extras can be combined, for example
`cli-consumption[server,postgres]` on a central collector.

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
| Mistral Vibe CLI | `mistral-vibe` | `~/.vibe/logs/session/` | Session-level token aggregates, user turns, tools, and compactions; no per-message timestamps or historical model attribution. |
| OpenCode | `opencode` | `~/.local/share/opencode/opencode.db` | SQLite v2 only; no legacy storage, child sessions, context windows, or costs. |
| OpenHands CLI | `openhands` | `~/.openhands/conversations/` | SDK persistence with context windows, reasoning effort, and condensations; excludes cloud-only conversations and delegates. |
| Pi | `pi` | `~/.pi/agent/sessions/` | Counts all persisted branches; no branch relationships, custom-directory auto-detection, context windows, or provider-reported durations. |
| Plandex | `plandex` | `/plandex-server` | Requires an offline copy of a self-hosted `PLANDEX_BASE_DIR`; hosted accounts are not accessed, and models/tools are unavailable. |
| Qwen Code | `qwen` | `~/.qwen/projects/` | Follows the active branch and records context windows and compactions; excludes archived and sidechain sessions. |

Provider formats are internal and can change without notice. The detailed extraction
rules and qualification versions are documented in
[Provider support](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/provider-support.md).

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
deduplicated, and the most complete copy wins. After a subagent scope is first seen,
its relationship graph is replaced only when at least one conversation from that
provider and source machine is strictly more complete and none is less complete.
Identical, graph-only, or older copies cannot erase a newer graph.

Provider files are untrusted. Monolithic JSON files are limited to 64 MiB, JSONL files
to 256 MiB with an 8 MiB per-line limit, 512 MiB of provider-file bytes actually read,
and discovery to 10,000 candidate entries per provider collection. Provider SQLite
inputs share a cumulative 512 MiB limit across databases and active WAL, SHM, or
journal sidecars, plus 250,000 selected rows, 8 MiB per structured field, and 256 MiB
across structured fields. A snapshot is limited to 250,000 normalized records while it
is being built. Direct provider-file symlinks are refused. `collect --strict` refuses
to write a snapshot when malformed records were skipped.

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
turn rate, context pressure, work-item reliability, configuration cohorts,
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

Limit an export to conversations whose activity overlaps a half-open UTC window:

```bash
uv run cli-consumption export --output reports \
  --since 2026-08-01 --until 2026-08-31
```

Dates denote UTC calendar boundaries; timestamps must include a timezone. An included
conversation is exported with its complete child graph rather than partially redacted
to the window. CSV rows are streamed in stable primary-key order. Spreadsheet formula
prefixes in text cells are neutralized with a leading apostrophe; CSV remains a
detailed operational-data export, not a share-safe format.

Dashboard generation preflights the selected report before streaming its tables. The
selection is limited to 250,000 rows and 128 MiB of selected scalar values, and the
final self-contained HTML is limited to 128 MiB of bytes actually encoded. If an
accumulated database exceeds these limits, narrow it with `--since` and/or `--until`.
A dashboard is streamed through a temporary file in its destination directory,
synchronized, and atomically replaces an older dashboard only after generation
succeeds.

When `--csv` and the dashboard are requested together, each CSV is still streamed
before dashboard generation. The dashboard file is atomic, but the output directory
as a whole is not: a dashboard limit or write failure can leave newly written CSV
files alongside the preserved older dashboard.

## SQLite and PostgreSQL

A file path selects SQLite. A SQLAlchemy URL selects PostgreSQL:

```bash
uv run cli-consumption collect --provider all --database usage.sqlite
uv run cli-consumption collect --provider all \
  --database postgresql+psycopg://usage@localhost/cli_consumption
```

Pass credentials through environment variables or a secret manager rather than shell
history. `CLI_CONSUMPTION_DATABASE` can provide the database setting.

Database schemas are upgraded automatically when a command opens them. Existing
unversioned databases that exactly match a published schema are adopted before the
upgrade; unknown or modified schemas are refused. Back up production databases before
upgrading and do not run mixed application versions against one database while a
migration is in progress. See the
[migration decision](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/decisions/0001-versioned-schema-migrations.md)
for rollback and compatibility rules.

Timezone-aware timestamps are normalized to fixed-width UTC strings during ingestion.
Revision `0003` rewrites legacy timestamp text in bounded batches and adds an indexed
conversation end-time path; see the
[timestamp decision](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/decisions/0002-canonical-utc-timestamps.md)
for the exact representation and downgrade boundary.

Revision `0004` adds internal per-scope state that serializes subagent graph freshness
decisions. It does not add snapshot or export fields; see the
[subagent freshness decision](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/decisions/0003-subagent-scope-freshness.md).

Preview retention before deleting normalized metadata:

```bash
uv run cli-consumption retention --keep-days 90 --database usage.sqlite
uv run cli-consumption retention --keep-days 90 --database usage.sqlite --apply
```

The first command is a dry run. `--apply` deletes old conversations and their child
rows, old subagent relationships, and old ingestion-run records.
Internal subagent-scope coordination rows remain as replay guards, so an older
graph-only copy cannot recreate relationships after retention. They contain only the
provider, source-machine label, and a lock counter and are never exported.

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
deployments also need TLS and standard operational controls. The sync client refuses
plain HTTP beyond loopback unless `--allow-insecure` is passed explicitly. See
[Architecture](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/architecture.md) for the trade-offs.

Use `GET /health` as the process liveness probe; it never opens the database. Use
`GET /ready` as the traffic readiness probe; it returns `200` only when the database
is reachable and its schema is the expected revision, otherwise a generic `503`.
The readiness path uses one fixed schema query with a two-second statement/busy
timeout. Engine connection and PostgreSQL pool acquisition are capped at five seconds,
so a configured PostgreSQL probe has a seven-second upper operational deadline.
Both endpoints are intentionally unauthenticated so infrastructure probes can call
them, and every HTTP response carries a bounded `X-Request-ID`. Put the collector
behind a TLS-terminating reverse proxy or platform ingress. Configure request rate
limits, connection limits, trusted proxy headers, and access-log redaction there; the
application does not implement a second rate limiter and disables Uvicorn access logs
to avoid recording untrusted URLs or query strings.

Snapshots use strict schema version 1. The collector rejects request bodies larger
than 32 MiB and snapshots containing more than 250,000 normalized records. A sync
client checks `/api/v1/capabilities` before sending when the endpoint exposes it.
Upgrade the server before clients whenever supported snapshot schemas change.

## Provider diagnostics

`providers` reads the central adapter registry. Its machine-readable mode checks local
default stores and emits deterministic JSON:

```bash
uv run cli-consumption providers --json
```

Each provider reports one of `no-data`, `detected`, `compatible`, `degraded`, or
`unsupported-schema`. Diagnostics parse enough metadata to assess compatibility but do
not persist it and never include paths, identifiers, record contents, counts, or parser
errors in their output. Schema version 2 also declares whether token counters are
additive, conversation aggregates, context snapshots, or unavailable. Dashboard token
per-turn percentiles use only additive providers rather than treating missing measures
as zero.

## Commands

| Command | Purpose |
| --- | --- |
| `collect` | Collect local or copied provider data into SQL. |
| `sync` | Collect and send metadata-only snapshots to a central API. |
| `serve` | Run the central collection API. |
| `export` | Write the HTML dashboard and optional CSV tables. |
| `providers` | List provider names and support status. |
| `retention` | Preview or apply deletion of metadata outside a retention window. |

Run `uv run cli-consumption COMMAND --help` for all options.

`collect`, `export`, and `retention` accept `--json` for deterministic
machine-readable results. `collect --strict` rejects snapshots containing malformed
provider records before opening the destination database.

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
before changing the project. Security issues follow the private reporting guidance in
[SECURITY.md](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/SECURITY.md).

## License

Licensed under the [Apache License 2.0](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/LICENSE).
