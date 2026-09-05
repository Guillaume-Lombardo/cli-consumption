# Architecture

## Goals

CLI Consumption separates provider-specific extraction from provider-neutral storage
and reporting. This lets users compare multiple AI coding CLIs without forcing their
local formats into one parser.

```text
provider files -> adapter -> metadata-only snapshot -> SQL storage -> dashboard/CSV
                                      |
                                      +-> signed gzip file -> SQL storage
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
- `snapshot_files`: serializes the existing strict snapshot schema in a bounded,
  deterministic gzip envelope authenticated with an Ed25519 signature.
- `api`, `reporting_api`, and `sync`: offer optional scoped ingestion, reporting,
  standalone export, and recurring multi-machine synchronization.
- `dashboard`, `reporting`, and `exporting`: select complete conversation graphs,
  provide an offline HTML view by default, and stream deterministic portable CSV tables
  when explicitly requested.
- `apps/web` provides the persistent Next.js dashboard as a server-side BFF and UI;
  FastAPI remains the sole reporting/export boundary and the only service allowed to
  open PostgreSQL.
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

### Signed offline snapshot files

`snapshot create` collects provider data locally, validates the existing
provider-neutral snapshot schema, serializes a fixed version-1 envelope, compresses it
deterministically, and signs the magic header plus compressed bytes with Ed25519. The
envelope adds only its fixed format name and version; it does not extend the normalized
data model. `snapshot ingest` reads a regular bounded file, verifies it with a trusted
PEM public key before decompression, strictly parses the envelope, and sends each
snapshot through normal idempotent ingestion for SQLite or PostgreSQL.

This option works without a network service and avoids transferring raw provider
stores. Authentication detects tampering and identifies possession of a trusted
signing key; it does not encrypt metadata or establish when or where a file was
created. Trust distribution, private-key protection, revocation, snapshot-file
retention, and replay policy remain operator responsibilities. Replays are safe at the
storage boundary but still produce ingestion-run metadata.

### Central API

Run `serve` with PostgreSQL and send snapshots using `sync`. Parsing stays on the source
machine, so raw provider files do not cross the network. This works well for recurring
collection and several workstations.

The API is intentionally bounded. Separate bearer credentials protect `ingest`,
`read`, and combined `read`/`export` operations; the legacy API token grants ingestion
only. A production deployment still needs TLS, token rotation, backups, monitoring,
and a reverse proxy or platform ingress. The collector limits snapshot bodies to 32 MiB
including chunked requests, accepts snapshot schema v1, and caps a snapshot at 250,000
normalized records. `/api/v1/capabilities` publishes these limits and the supported
schema range so clients can fail before uploading incompatible data. One sync command
reuses a single HTTP client and negotiates capabilities once for its endpoint. A
collector that advertises idempotent uploads receives an opaque UUIDv4 per logical
snapshot. The client reuses that UUID for at most three attempts, with 0.25 and 0.5
second backoffs after transport failures or HTTP 500/502/503/504. It does not retry an
ambiguous upload against a legacy collector that lacks the capability.
Strict sync validates the complete local batch before creating that client. During a
multi-provider execution, one failed upload does not prevent later independent
providers from being attempted. Machine-readable results preserve provider order,
carry local malformed and duplicate counters, and distinguish complete from partial
success. External exceptions, response bodies, paths, and payload values are reduced
to fixed error codes before they reach human or JSON output.
The sync client refuses plain HTTP beyond loopback unless the operator passes the
explicit `--allow-insecure` override for a trusted network.

The persistent dashboard and web-export consumers follow the database-upload,
reporting, pagination, and scoped-authorization boundaries defined in
[ADR 0004](decisions/0004-persistent-dashboard-contracts.md). It defines
`DashboardQuery v1`, the minimized dataset, complete-graph selection, SQLite and
PostgreSQL transaction behavior, concrete limits, server-first compatibility, and the
rule that Next.js never connects directly to SQL. Reporting bodies are strict and
limited to 64 KiB; selections fail rather than truncate above 250,000 normalized rows,
128 MiB of scalar data, or 32 MiB of JSON. Four report reads and one export may run per
process. Pagination membership is retained in bounded process memory behind random
opaque handles, so concurrent ingestion cannot alter an active page sequence. Handles
are deliberately process-local: a restart expires them, and multi-replica deployments
must use request affinity or a future shared session implementation. Clients always
recover by restarting from the first page.

The Next.js BFF authenticates an operator with a short-lived, signed, HTTP-only
session cookie and holds the collector read token only in its server environment. It
accepts a fixed allowlist of reporting operations, enforces same-origin mutations and
bounded JSON bodies, and converts upstream failures to generic error codes. Operational
labels are sent in POST bodies, while only the period and UTC date window may appear in
the dashboard URL. The browser starts with a bounded latest-30-days query, retains no
session credential in web storage, and stores only the visual theme locally.

`/health` is an unauthenticated liveness endpoint and deliberately performs no
database work, so a database outage does not cause the orchestrator to restart a
healthy process. `/ready` is the unauthenticated readiness endpoint. It performs a
single fixed query of the Alembic revision and expected tables and returns only
`{"status":"ready"}` or a generic `503` body within a two-second application
deadline. PostgreSQL readiness uses a dedicated `NullPool` engine rather than changing
the primary ingestion engine. Its handshake configures a two-second connection
timeout, 1.5-second statement timeout, and one-second lock timeout before the probe
query. SQLite applies a 1.5-second busy timeout only to its probe connection.

Only one daemon probe can be active. Concurrent calls return `503` without creating
more threads or database connections. If a driver, proxy, DNS resolver, or kernel
network path ignores the connection timeout, that one abandoned probe may outlive the
HTTP response and retains the only probe slot until it finishes. Shutdown marks the
runner closed and disposes the dedicated unpooled engine without waiting for the
daemon. Operators must still set an orchestrator timeout slightly above two seconds;
the application deadline is a response bound, not a promise that every underlying
network operation has stopped. Route traffic only while readiness is successful.

Every response has an `X-Request-ID`. An incoming identifier is retained only when it
matches the bounded identifier grammar; otherwise the application generates one.
Internal failures and failed readiness probes produce structured application events
containing only that identifier, a fixed event and code, a constrained method, a
static route template, and a coarse allowlisted exception type. Exception messages,
tracebacks, request bodies, headers, tokens, database URLs, paths, and query strings
are never included. Uvicorn access logging is disabled by `serve` because raw request
targets are untrusted. An ASGI boundary outside FastAPI consumes the framework's
re-raised application exception after emitting the generic response, so Uvicorn
cannot add an exception traceback to its own error log. Operators should configure
redacted access logs, TLS, connection limits, request rate limits, and
trusted-forwarded-header handling at the
reverse proxy or platform ingress. There is intentionally no application-level rate
limiter competing with that boundary.

## Storage

SQLite is the zero-configuration default. PostgreSQL is selected by passing a
`postgresql+psycopg://` URL. Every normal database open upgrades through packaged
Alembic migrations. An unversioned database is adopted only when its columns, SQL types,
nullability, primary and foreign keys, and indexes exactly match a published schema; an
unknown, newer, or locally modified schema is refused. Migration revisions support
SQLite and PostgreSQL and define a bounded downgrade, but application rollback can
still require restoring a pre-upgrade backup. Operators must upgrade the server first
and avoid mixed-version access during migration. The detailed policy is recorded in
[ADR 0001](decisions/0001-versioned-schema-migrations.md).

Snapshot extraction is the deliberate exception to migration-on-open. It accepts only
an existing regular local SQLite file, opens it with URI `mode=ro` and SQLite
`query_only`, starts an explicit read transaction, and requires both Alembic revision
`0006` and the exact current table, column, type, nullability, key, constraint, and
index layout. It never adopts, stamps, migrates, or repairs the source. A normal
read-only SQLite connection keeps committed WAL contents visible; every estimate and
row query therefore observes the same database snapshot while collection may continue.
An orphan `-wal` file without its `-shm` sidecar cannot be opened from a non-writable
directory because rebuilding shared memory would violate this boundary; extraction
fails with `database_unavailable` until the operator restores a consistent sidecar set
or copies the database and WAL into a private writable directory.

The extractor reuses reporting's half-open `since`/`until` selection. A selected
conversation always carries all its normalized child rows, and a subagent edge is
retained when either endpoint belongs to the selection. Unbounded graph-only provider
scopes remain representable. Rows are grouped into strict snapshot-schema-v1 instances
by provider, with stable IDs, relationships, and content hashes unchanged.
`ingestion_runs`, `sync_receipts`, `subagent_scopes`, `dashboard_layouts`, and Alembic state are never
transferred. SQL preflight and serialized-output checks cap one extraction at 10,000
conversations, 250,000 normalized records, and 128 MiB. Incompatible data, invalid
windows, unavailable databases, and limit failures use fixed codes without paths,
database values, or driver messages. The same exact-layout verifier runs against both
SQLite and PostgreSQL even though local database-file extraction itself is SQLite-only.

Conversation records use a provider-qualified stable ID. Repeated ingestion skips an
identical or less complete record. A more complete copy atomically replaces the
conversation and its child records.

Remote idempotency receipts are internal, provider-neutral rows mapping a canonical
client UUIDv4 to one completed ingestion run. Receipt insertion shares the ingestion
transaction and its primary key serializes concurrent replays on SQLite and
PostgreSQL. A replay returns the stored run identifier and counters without creating a
second ingestion run. Receipt rows are not exported and are removed by the foreign-key
cascade when retention removes their ingestion run. New clients remain single-attempt
with older servers; older clients can write to the new optional-header endpoint but do
not receive retry guarantees.

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

Dashboard composition is a separate provider-neutral `DashboardLayout v1` contract,
not part of the reporting query or dataset. A closed widget registry defines allowed
types and size bounds; both React runtimes share its TypeScript validator, default, and
retired-widget resolver, coordinate ordering, and twelve-column grid renderer. Every
registered widget participates, so visibility and relative geometry have identical
semantics online and offline. Widget identifiers are restricted to `type` or bounded
`type-N` structural values, preventing layout persistence or offline HTML from becoming
a free-form label channel. The collector persists one canonical layout for the current
mono-operator deployment in the internal `dashboard_layouts` table. Revision `0006`
creates that table, downgrade to `0005` discards only the preference, and mixed-version
deployments upgrade the collector before the BFF. Read access never grants mutation:
save/reset require the distinct `layout` scope. Snapshot extraction, CSV, reporting
datasets, ingestion, and retention exclude the table. See
[ADR 0005](decisions/0005-versioned-dashboard-layouts.md).

Provider input is bounded before persistence. Monolithic JSON files are capped at
64 MiB, JSONL files at 256 MiB with an 8 MiB per-line limit, actual provider-file reads
at 512 MiB, and discovery at 10,000 candidate entries per provider collection before
sorting. JSON and JSONL readers open a descriptor without following symlinks, verify
its identity, and count bytes as they stream, so later path substitution or growth
cannot redirect or bypass the cap. Provider SQLite readers share 512 MiB across every
database and its active WAL, SHM, or rollback-journal sidecars, 250,000 selected rows,
8 MiB per structured field, and 256 MiB of structured fields in total. SQLite length
preflights reject oversized structured text and blobs before Python materializes them.
The complete in-memory normalized snapshot remains capped at 250,000 records. These
limits use generic error codes and apply to local collection as well as snapshots later
sent to the API.

SQLite databases remain opened by their original path so live WAL contents are not
lost. The reader simultaneously holds no-follow descriptors for the database and
visible sidecars, verifies device/inode identity before and after extraction, and
charges observed growth. Any transient mutation entirely between identity checks,
including an in-place overwrite or growth followed by truncation, is the residual
portable race; reading SQLite through a pinned descriptor would remove it but would
also hide path-relative live WAL state.

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

Before a dashboard streams those ten report tables, one compound SQL statement counts
the complete selected graph and measures its selected scalar byte size. Using one
statement keeps all table estimates on one database snapshot. The preflight, bounded
key/alias prepasses, and streamed row reads then share a transaction. SQLite executes
an explicit `BEGIN DEFERRED` before the preflight, and PostgreSQL uses repeatable-read
isolation. Generation is refused above 250,000 selected rows or 128 MiB of selected
scalar values. Conversation, turn, external-key, and share-safe alias indexes have a
separate conservative 128 MiB allocation budget. Entries are charged before insertion
for dictionary/set capacity, aligned string storage, tuples, integers, and alias
values. Project, machine, and role aliases stream from ordered SQL `DISTINCT` results,
so they never coexist as a Python set, sorted list, and dictionary. Model names found
inside `models_json` still require a set; its entries plus the peak sorted-reference
list, alias dictionary, and alias strings are all charged concurrently. This is a
portable upper-bound model for the supported 64-bit CPython versions, not a claim that
process RSS exactly equals the counter.

Rows are transformed and JSON-encoded one at a time directly into the temporary HTML;
the production path never materializes all SQL rows, the full dashboard payload, the
complete JSON string, or the complete HTML document in memory. Every encoded chunk is
charged before writing and the complete HTML has an exact 128 MiB limit. Limit errors
expose no labels, IDs, paths, or database values and direct the operator to `--since`
and `--until`.

Interactive selection, period, aggregation, percentile, and comparison calculations
are a pure JavaScript contract over that minimized payload. The packaged resource has
no DOM, storage, logging, or network access and returns only selected rows or derived
scalars. It is inlined into the generated document so the production dashboard stays
self-contained while the exact calculation code remains independently testable.

The HTML is written to a uniquely named temporary file in the destination directory,
flushed and synchronized, then installed with an atomic replacement. The destination
directory is synchronized after replacement on platforms and filesystems that support
directory `fsync`; unsupported directory synchronization falls back explicitly to the
atomic rename guarantee. A failure before replacement leaves an existing dashboard
intact and removes only the temporary file owned by that generation attempt; unrelated
stale temporary files are not deleted.
Each CSV independently streams through a uniquely named temporary file, flushes and
synchronizes it, then atomically replaces that table's final file and synchronizes the
directory. A failure before replacement preserves the existing CSV and removes only
the temporary owned by that table export. Replacement preserves an existing CSV's
file mode, while a new CSV retains private temporary-file permissions. Earlier tables
can already be replaced when
a later table or dashboard fails, so `export --csv` plus a dashboard is not an atomic
transaction for the entire output directory.
The temporary text stream disables newline translation, so the encoded-byte counter,
file position, and bytes written remain identical on Windows as well as POSIX systems.

### Offline dashboard continuity gate

Every pull request in the persistent-dashboard migration must preserve the standalone
export before it can merge. CI generates both the detailed and share-safe dashboards
from the installed Python package, opens each generated file directly through a
`file://` URL in headless Chromium, exercises filters and theme changes, and fails on
any HTTP(S) request, WebSocket, browser error, external script, stylesheet, import, or
other network primitive. The gate needs no application server or remote database.

This browser gate runs the production React/Tailwind renderer for both profiles. It
complements the deterministic-generation, privacy-canary, bounded-memory,
complete-conversation, and atomic-replacement tests; it does not replace them. The
standalone offline capability remains supported independently of the Next.js service.

### Shared TypeScript calculation workspace

The repository pins Node and npm and contains four private workspaces: versioned
dashboard contracts, pure analytics, presentation helpers, and the offline browser
entry point. Python still owns SQL selection, bounds, privacy transforms, and HTML
generation. The analytics package accepts only `DashboardDataset v1`, validates the
version and required sections, and contains no DOM, storage, or network primitive.

The analytics and contracts packages build provider-neutral ESM for the persistent
web application. Shared React cards, bars, and sections live in the UI package and are
consumed by both Next.js and the standalone entry point. The offline package produces
one production React bundle and compiled Tailwind stylesheet under
`src/cli_consumption/`. Python streams the same minimized dataset v1 between those
inline assets, so generated reports remain one bounded file and downstream wheel users
do not need Node. CI rebuilds both assets from the lockfile and fails on any diff. The
former Python-string renderer and its compatibility calculation bundle were removed
after detailed/share-safe parity, packaging, privacy, performance, and rollback gates
passed.

The web offline action posts the exact current `DashboardQuery v1` to a same-origin
Next.js route. The BFF authenticates the browser session, keeps its dedicated export
credential server-side, bounds both request and response, and buffers the complete
collector result before returning a fixed-name `no-store` attachment. FastAPI creates
the file from one coherent SQL snapshot with the React renderer and removes its private
temporary after successful streaming, failure, or cancellation.

### Cutover evidence and operating impact

The final cutover was measured with the deterministic public-demo fixture (eight
conversations) on `codex-dev`, Python 3.14, after a warm dependency sync. Five
sequential generations were compared on the revision immediately before and after the
renderer switch:

| Measure | Classic baseline | React/Tailwind | Impact |
| --- | ---: | ---: | ---: |
| Median generation wall time | 0.486 s | 0.489 s | +0.6% |
| Self-contained demo HTML | 67,522 bytes | 236,466 bytes | +168,944 bytes |
| Python dashboard source | 61,674 bytes | 26,144 bytes | -35,530 bytes |
| Packaged compatibility calculation asset | 18,563 bytes | removed | -18,563 bytes |

These figures are a reproducible comparison fixture, not a production latency promise.
The React bundle adds fixed output and browser-parse overhead; SQL selection and JSON
streaming still scale with the selected rows. Existing 250,000-row, 128 MiB scalar,
128 MiB HTML, 60-second API export, and single-concurrent-export ceilings are unchanged.
Operators should narrow large time windows before changing those safety bounds.

The cutover has no schema, snapshot, or API-contract migration. Rollback means running
the preceding application version against the same supported database schema; it does
not require a data downgrade. Node remains a build-time dependency only. Installed CLI
exports, FastAPI exports, and downloaded files remain autonomous and require no Node or
application server to open.

## Adapter qualification

Adapters are introduced one at a time because local data formats are undocumented or
can evolve independently. Each adapter must have synthetic fixtures, format detection,
privacy tests, and a documented support level before it appears as supported.
The central registry also records its qualified provider or schema version, UTC date,
format, synthetic fixture, primary provenance, and known limits. A weekly scheduled
check fails when any qualification is older than 90 days. This is a maintenance
signal, not runtime telemetry: qualification metadata and fixture paths are never
included in provider diagnostics, snapshots, storage, APIs, exports, or logs.

Timestamp fields are canonical fixed-width UTC strings after strict snapshot
validation and schema revision `0003`. Reporting and retention bind values in the same
form and use direct indexed predicates with explicit null branches. The representation,
migration, and rollback boundary are documented in
[ADR 0002](decisions/0002-canonical-utc-timestamps.md).

The bounded chart calculations and identical online/offline rendering semantics are
defined by [ADR 0006](decisions/0006-bounded-dashboard-chart-catalog.md). Daily attribution
uses UTC and never guesses timestamps for aggregate provider counters.
