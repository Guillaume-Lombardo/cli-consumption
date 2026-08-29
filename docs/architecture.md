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
- `models`: define the strict, versioned transport boundary shared by offline and API
  ingestion.
- `storage`, `schema`, and `migrations`: own the normalized schema, idempotent
  replacement rules, automatic Alembic upgrades, legacy adoption, SQLite, and
  PostgreSQL engine creation.
- `api` and `sync`: offer an optional push workflow for recurring multi-machine use.
- `dashboard`, `reporting`, and `exporting`: select complete conversation graphs,
  provide an offline HTML view by default, and stream deterministic portable CSV tables
  when explicitly requested.
- `adapters.registry`: is the single source for canonical names, aliases, adapter
  classes, default homes, detection markers, support state, and token semantics.
- `cli`: exposes the same capabilities through one executable.

When Codex exposes its local thread graph, the adapter also records metadata-only
subagent relationships. Agent filesystem paths and nicknames are intentionally
discarded. Roles and statuses are reduced to fixed provider-neutral vocabularies, and
the stored conversation `source` is a normalized format label rather than an arbitrary
provider value.

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
deployment still needs TLS, token rotation, backups, monitoring, and a reverse proxy
or platform ingress. Read access is not exposed. The collector limits bodies to 32 MiB
including chunked requests, accepts snapshot schema v1, and caps a snapshot at 250,000
normalized records. `/api/v1/capabilities` publishes these limits and the supported
schema range so clients can fail before uploading incompatible data.
The sync client refuses plain HTTP beyond loopback unless the operator passes the
explicit `--allow-insecure` override for a trusted network.

## Storage

SQLite is the zero-configuration default. PostgreSQL is selected by passing a
`postgresql+psycopg://` URL. Every database open upgrades through packaged Alembic
migrations. An unversioned database is adopted only when its columns, SQL types,
nullability, primary and foreign keys, and indexes exactly match a published schema; an
unknown, newer, or locally modified schema is refused. Migration revisions support
SQLite and PostgreSQL and define a bounded downgrade, but application rollback can
still require restoring a pre-upgrade backup. Operators must upgrade the server first
and avoid mixed-version access during migration. The detailed policy is recorded in
[ADR 0001](decisions/0001-versioned-schema-migrations.md).

Conversation records use a provider-qualified stable ID. Repeated ingestion skips an
identical or less complete record. A more complete copy atomically replaces the
conversation and its child records.

Subagent relationships have a provider-and-source-machine lifecycle. The first
snapshot representing a scope can create its graph. After that, the graph is replaced
atomically only when at least one represented conversation is strictly more complete
than its stored copy and none is less complete. This permits a genuinely newer empty
graph to remove deleted relationships, while identical, graph-only, mixed stale/richer,
and wholly older snapshots cannot regress the graph. An internal scope row serializes
this decision across concurrent writers on SQLite and PostgreSQL; it is not exported.

Workflow analytics use additive child tables: `work_items`, `context_samples`,
`turn_settings`, and `compaction_events`. Snapshot schema v1 validates every record,
rejects unknown fields, enforces normalized labels and relationships, and exposes only
generic validation errors. A newer client sent to an older strict API is rejected
before ingestion, so central deployments must upgrade the server first.

Provider input is bounded before persistence. Monolithic JSON files are capped at
64 MiB, JSONL files at 256 MiB with an 8 MiB per-line limit, and discovery at 10,000
candidate entries per provider collection before sorting. JSONL readers count bytes as
they stream, so a file that grows after its initial size check cannot bypass the cap.
Provider SQLite files are capped at 512 MiB; readers share budgets of 250,000 selected
rows, 8 MiB per structured field, and 256 MiB of structured fields in total. The
complete in-memory normalized snapshot remains capped at 250,000 records. Direct
symlinks to provider files are rejected. These limits use generic error codes and apply
to local collection as well as snapshots later sent to the API.

Retention is an explicit two-step operation: `retention --keep-days N` reports what
would be deleted, while `--apply` deletes old conversations (with cascading children),
subagent relationships, and ingestion runs in one transaction. Internal subagent-scope
rows remain as replay guards; deleting them would let an unversioned graph-only copy
become first-seen and recreate stale relationships.

Time-bounded reporting selects conversations whose recorded activity overlaps the
half-open `[since, until)` window. Once selected, the complete conversation and all its
children are included; this is graph consistency, not timestamp-level redaction.
Related subagent edges are included when either endpoint belongs to a selected
conversation, while ingestion runs are filtered by their own timestamp. All tables are
ordered by primary key. CSV output consumes streamed batches rather than materializing
each table, and neutralizes text that spreadsheet software could execute as a formula.

## Adapter qualification

Adapters are introduced one at a time because local data formats are undocumented or
can evolve independently. Each adapter must have synthetic fixtures, format detection,
privacy tests, and a documented support level before it appears as supported.

Timestamp fields are canonical fixed-width UTC strings after strict snapshot
validation and schema revision `0003`. Reporting and retention bind values in the same
form and use direct indexed predicates with explicit null branches. The representation,
migration, and rollback boundary are documented in
[ADR 0002](decisions/0002-canonical-utc-timestamps.md).
