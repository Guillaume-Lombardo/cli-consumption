# Architecture

## Goals

CLI Consumption separates provider-specific extraction from provider-neutral storage
and reporting. This lets users compare multiple AI coding CLIs without forcing their
local formats into one parser.

```text
provider files -> adapter -> metadata-only snapshot -> SQL storage -> dashboard/CSV
                                      |
                                      +-> HTTPS collector -> central SQL storage
```

## Components

- `adapters`: parse a CLI's local data into conversations, turns, model calls, tool
  calls, context-pressure samples, bounded turn settings, compactions, and content-free
  work-item intervals. Codex exposes the complete analytics contract. Aider, Amazon Q
  Developer CLI, Amp, Cline CLI, Claude Code, Continue CLI, Crush, Cursor CLI, Gemini
  CLI, GitHub Copilot CLI, Goose, Grok Build, Kilo Code, Kimi Code CLI, OpenCode,
  OpenHands CLI, Pi, Plandex, and Qwen Code expose the reliable subset available in
  each local store. The exact differences are maintained in
  [Provider support](provider-support.md).
- `models`: define the transport boundary shared by offline and API ingestion.
- `storage`: owns the normalized schema, idempotent replacement rules, SQLite, and
  PostgreSQL engine creation.
- `api` and `sync`: offer an optional push workflow for recurring multi-machine use.
- `dashboard` and `exporting`: provide an offline HTML view by default and portable CSV
  tables when explicitly requested.
- `cli`: exposes the same capabilities through one executable.

When Codex exposes its local thread graph, the adapter also records metadata-only
subagent relationships. Agent filesystem paths are intentionally discarded.

## Multi-machine alternatives

### Copied files

Copy provider metadata directories to a trusted machine and pass repeated `--source`
options. This has no listening service, works offline, and is easiest to audit. It is
recommended for personal use, occasional reports, and air-gapped environments.

The trade-off is operational: users must schedule copies and keep machine clocks
synchronized if they later analyze overlapping activity.

### Central API

Run `serve` with PostgreSQL and send snapshots using `sync`. Parsing stays on the source
machine, so raw provider files do not cross the network. This works well for recurring
collection and several workstations.

The API is intentionally small. A bearer token protects ingestion; a production
deployment still needs TLS, token rotation, backups, request-size limits, monitoring,
and a reverse proxy or platform ingress. Read access is not exposed in the initial API.

## Storage

SQLite is the zero-configuration default. PostgreSQL is selected by passing a
`postgresql+psycopg://` URL. SQLAlchemy keeps the first schema portable. Schema
migrations will be introduced before a released version needs an in-place incompatible
change.

Conversation records use a provider-qualified stable ID. Repeated ingestion skips an
identical or less complete record. A more complete copy atomically replaces the
conversation and its child records.

Workflow analytics use additive child tables: `work_items`, `context_samples`,
`turn_settings`, and `compaction_events`. Existing SQLite and PostgreSQL databases gain
these tables through SQLAlchemy `create_all`; no existing table is altered. Older
snapshots omit the new lists and remain valid on a newer collector. A newer client sent
to an older strict API is rejected before ingestion, so central deployments must
upgrade the server first. Downgrading a writer after rich metadata has been ingested is
not supported because old writers do not know how to replace the new child rows.

## Adapter qualification

Adapters are introduced one at a time because local data formats are undocumented or
can evolve independently. Each adapter must have synthetic fixtures, format detection,
privacy tests, and a documented support level before it appears as supported.
