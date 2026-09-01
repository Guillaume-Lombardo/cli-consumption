# ADR 0004: Versioned persistent-dashboard contracts

- Status: Accepted
- Scope: database upload, reporting API, Next.js dashboard, and offline HTML export

## Context

CLI Consumption has one self-contained HTML dashboard generated from normalized SQL.
The next product increment adds a persistent web application without weakening the
offline workflow, duplicating metric semantics, exposing PostgreSQL to the frontend,
or widening the privacy boundary.

The persistent application needs four related contracts:

1. transferring a database's validated metadata without uploading SQLite itself;
2. selecting a complete, bounded reporting dataset;
3. listing and inspecting conversations without exposing stable provider identifiers;
4. exporting the exact web selection as a standalone detailed or share-safe HTML file.

These contracts must behave identically on SQLite and PostgreSQL. They also need
explicit authorization, compatibility, size, duration, and concurrency boundaries
before implementation begins.

## Decision

### Component ownership

Python remains the owner of provider parsing, snapshot validation, database schema and
migrations, idempotent ingestion, SQL reporting selection, privacy minimization,
limits, and HTML generation. FastAPI is the only network boundary for ingestion,
reporting, and export. It opens SQLAlchemy engines and applies authentication,
authorization, transactions, timeouts, response bounds, generic errors, and minimized
logging.

PostgreSQL is the central production store. SQLite remains supported for local
collection, offline export, database extraction, development, and all reporting
contracts. Next.js never opens either database and no JavaScript package receives a
database URL.

Next.js is a server-side BFF and presentation layer. It owns browser sessions, server
rendering, navigation, accessible components, and calls to FastAPI. Upstream
credentials stay in the BFF runtime and are never serialized into browser JavaScript,
HTML, URLs, local storage, telemetry, or error pages. The browser calls only same-origin
Next.js routes with its session cookie.

The existing Python renderer remains the supported offline implementation until the
replacement offline runtime has passed the continuity gate and exact metric-parity
fixtures. A persistent-dashboard change cannot disable or weaken `cli-consumption
export`.

```text
provider stores -> Python adapters -> validated snapshots -> SQLAlchemy -> SQLite
                                                        \-> PostgreSQL

SQLite collect database -> read-only Python extraction -> validated snapshots
                                                     -> FastAPI ingestion -> PostgreSQL

browser -> Next.js BFF -> FastAPI reporting -> SQLAlchemy -> PostgreSQL
                         FastAPI export ----> Python standalone HTML renderer
```

### Authorization scopes

Credentials carry a fixed set drawn from `ingest`, `read`, and `export`. Unknown
scopes are invalid.

| Scope | Permitted operations |
| --- | --- |
| `ingest` | Validated snapshot upload, including snapshots extracted by `upload-db`. |
| `read` | Filter options, dashboard datasets, conversation pages, and bounded conversation detail. |
| `export` | Standalone HTML generation; the route requires both `read` and `export`. |

The capabilities route stays unauthenticated and returns only fixed version and limit
metadata; it performs no reporting read. Health and readiness retain their existing
availability-only behavior.

The current single ingestion token remains compatible as `ingest` only. It never
implicitly gains `read` or `export`. A missing or invalid credential returns the same
generic authentication error; an authenticated credential missing a required scope
returns a generic authorization error. Neither response identifies the credential,
available scopes, requested labels, or database values.

Credentials are accepted only in authorization headers between trusted server-side
components. They are prohibited in query strings, reporting bodies, cursors, export
files, logs, and browser-visible state. Production deployments use distinct
credentials for ingestion and the Next.js BFF.

### Database upload contract

`upload-db` never sends a SQLite file, WAL, journal, database URL, filesystem path, or
schema dump. Python opens a local CLI Consumption database read-only, refuses any
unknown or non-current schema before transfer, and reconstructs strict provider-neutral
snapshot schema v1 records inside one coherent transaction. Internal
`sync_receipts`, `subagent_scopes`, migration state, and ingestion-run rows are not
transferred.

Each extracted provider or coherent fragment is submitted to the existing snapshot
ingestion boundary after capability negotiation. It receives a canonical UUIDv4
idempotency key derived from a domain-separated SHA-256 digest of the canonical strict
snapshot. The same logical fragment therefore reuses its key across invocations while
a richer replacement receives a new key. Receipt support is required before the first
POST. Upload ordering is deterministic; the default mode continues after an independent
provider failure, while strict mode stops and marks remaining providers as skipped.
Successful fragments survive later failures. The client reports only provider names,
bounded status labels, fixed error codes, opaque ingestion-run IDs, and aggregate
counts; idempotency keys never appear in output or logs.

### `DashboardQuery v1`

Reporting and export routes accept a JSON body rather than label-bearing URL query
parameters. The strict contract rejects unknown fields and has this shape:

```json
{
  "version": 1,
  "window": {
    "since": "2026-08-01T00:00:00+00:00",
    "until": "2026-09-01T00:00:00+00:00"
  },
  "filters": {
    "providers": ["codex"],
    "machines": ["workstation"],
    "projects": ["cli-consumption"],
    "models": ["gpt-5.6"]
  },
  "profile": "detailed"
}
```

- `version` is exactly `1`.
- All four top-level fields are required. `window` may contain null bounds and
  `filters` may be empty; this keeps canonical serialization and cursor binding
  unambiguous.
- `window.since` is inclusive and `window.until` is exclusive. Values are canonical
  timezone-aware timestamps or `null`; `since` must precede `until`.
- Filter lists contain exact normalized labels. Values are ORed within one dimension
  and the four dimensions are ANDed. An absent or empty list means all values for that
  dimension.
- A list contains at most 100 unique values. Provider labels are at most 64 UTF-8
  bytes, machine and model labels 255 bytes, and project labels 512 bytes.
- The complete encoded query body is at most 64 KiB.
- `profile` is `detailed` or `share-safe`. Interactive web reads normally use
  `detailed`; export may use either profile.
- The server applies no hidden time default. The web application sends an explicit
  latest-30-day window for its initial view. An unbounded selection is allowed only
  when it passes every preflight limit.

Time filtering first selects conversations whose recorded activity overlaps the
half-open window. Every selected conversation retains its complete child graph, even
when a child timestamp lies outside the window. Related subagent edges can therefore
reveal activity around a boundary. Model filtering selects conversations with that
model and still retains their complete graph; it does not delete other calls inside an
included conversation. This matches the offline selection contract.

The canonical query is the sole selection input for the dashboard dataset,
conversation list, conversation detail, and web-triggered export. A downloaded export
must not reinterpret browser state or reconstruct a query from URL text.

### Dashboard dataset v1

`POST /api/v1/reporting/dashboard` requires `read` and returns a strict envelope with
`contractVersion: 1`, applied window/profile metadata, available filter labels for the
authorized selection, and the minimized row sections already used by the offline
renderer:

- `conversations`: response-local numeric key, provider, token semantics, machine,
  project, start/end, duration, model labels, child counts, compaction count, and the
  normalized token composition;
- `turns`: response-local key and conversation key, start/end, normalized status,
  duration, time to first token, child counts, and token composition;
- `modelCalls`: response-local conversation/turn keys, timestamp, model, and token
  composition;
- `toolCalls`: response-local conversation/turn keys, sequence, timestamp, and tool
  name or share-safe category;
- `workItems`: response-local conversation/turn keys, fixed category, constrained tool
  name or category, start, duration, and normalized status;
- `contextSamples`: response-local keys, timestamp, input tokens, and context-window
  tokens;
- `turnSettings`: response-local keys, model, bounded effort/mode/tier labels, and
  context-window tokens;
- `compactions`: response-local keys and timestamp only;
- `subagents`: response-local parent/child keys, provider, machine, normalized status,
  start/end, normalized role, and optional token count;
- `ingestionRuns`: provider, timestamp, and received/written/skipped/malformed/
  duplicate counts.

Token composition means `input_tokens`, `cached_input_tokens`,
`cache_write_input_tokens`, `uncached_input_tokens`, `output_tokens`,
`reasoning_output_tokens`, `visible_output_tokens`, `unattributed_tokens`, and
`total_tokens`. Local keys exist only within one response and cannot be used as stable
identifiers on another route.

The API never returns normalized SQL primary keys, provider external IDs, content
hashes, source-format labels, project-mapping sources, ingestion-run IDs, receipt keys,
scope-lock rows, raw model JSON, or arbitrary metadata. It never silently truncates a
section. A selection either returns one coherent complete dataset or a generic limit,
timeout, or cancellation error.

Every reporting response uses `Cache-Control: no-store`, has no validator that embeds
database values, and is ineligible for shared caching. The Next.js BFF applies the same
policy to its browser responses.

The shared TypeScript analytics package consumes this dataset and owns the pure
selection and metric calculations used by Next.js and the replacement offline runtime.
Python fixtures remain the reference input. Exact expected metrics are asserted for
both surfaces so unavailable token dimensions are never treated as zero and metric
semantics cannot drift.

### Conversation list and detail

`POST /api/v1/reporting/conversations` requires `read` and combines
`DashboardQuery v1` with an allowlisted sort, direction, an opaque cursor, and a page
size from 1 to 200 (default 50). Ordering always appends a stable internal tie-breaker.
The response contains minimized conversation summary fields and server-minted opaque
conversation references, never database or provider identifiers.

The first page creates a server-side pagination session containing the selected
conversation membership and order from one coherent database snapshot. The session
expires five minutes after its last successful page read and no later than thirty
minutes after creation. Every following page uses that retained selection, so
concurrent ingestion cannot introduce duplicates or omissions. Expired sessions are
deleted and return the fixed `pagination_expired` response; the client must restart at
page one. Sessions count toward the reporting row, byte, duration, and concurrency
limits and are removed after the final page or cancellation.

The cursor is a versioned server-side opaque handle, or equivalently an authenticated-
encrypted value, protected for both confidentiality and integrity and bound to the
pagination session, canonical query, and sort. It is rejected generically when
malformed or reused with different inputs. Its external form contains no canonical-
query labels, sort keys, stable internal identifiers, or database values.

`POST /api/v1/reporting/conversation` requires `read`, the same canonical query, and
one opaque conversation reference from the list response. It returns only that
conversation's complete minimized graph when it belongs to the selection. Missing,
expired, malformed, unauthorized, and out-of-selection references share one generic
not-found response.

### Filter options

`POST /api/v1/reporting/filters` requires `read` and accepts a strict filter request
envelope containing exactly `version`, `window`, and `filters`. Version `1` is the only
initial supported payload version. `window` and `filters` have the same representation
and semantics as `DashboardQuery v1`; `filters` contains the already selected
dimensions, and unknown or omitted fields are rejected. The response returns sorted
distinct provider, machine, project, and model labels that remain available. It does
not return counts, stable identifiers, paths, hashes, or hidden values. These labels
and their existence are private operational metadata and receive the same
authorization and response bounds as the dataset.

### Web export

`POST /api/v1/reporting/export` requires both `read` and `export` and accepts exactly
`DashboardQuery v1`. Python starts one coherent read transaction, applies the same
selection and transformations as the dashboard dataset, and streams the standalone
HTML through a private temporary file. The response sets an attachment disposition
and headers preventing intermediary and browser caching.

The export is returned only after complete generation succeeds. Disconnect, timeout,
limit, and rendering failures remove the owned temporary and expose only fixed codes.
Detailed output retains the documented operational metadata. Share-safe output uses
the existing aliases, tool categories, day rounding, and small-cohort suppression. It
remains a minimization profile, not anonymization.

### Limits and overload behavior

The first implementation uses fixed safe ceilings that may later become configurable
only within an equal-or-lower deployment cap:

| Boundary | Ceiling |
| --- | ---: |
| Reporting/export request body | 64 KiB |
| Values per filter dimension | 100 |
| Selected normalized rows | 250,000 |
| Selected scalar values before transformation | 128 MiB |
| Dashboard JSON response | 32 MiB |
| Standalone HTML response | 128 MiB |
| Conversation page size | 200 |
| Reporting SQL plus serialization | 15 seconds |
| Standalone export generation | 60 seconds |
| Concurrent reporting reads per process | 4 |
| Concurrent exports per process | 1 |

Preflight counts rows and scalar bytes inside the same database snapshot used for the
read. Oversized selections fail before response serialization and recommend narrowing
the time window without echoing any filter or database value. There is no in-process
queue: excess concurrency fails immediately with a fixed busy code. Timeouts,
disconnects, and database errors cancel work where the driver permits, dispose of
owned resources, and return fixed generic codes. Logs retain only the bounded request
ID, static route, method, status class, fixed code, duration bucket, and coarse
exception type.

### Transaction and dialect behavior

SQLite and PostgreSQL execute the same provider-neutral SQLAlchemy selection and stable
ordering. Each dataset or export is read from one coherent snapshot. SQLite begins an
explicit read transaction before preflight; PostgreSQL uses repeatable-read isolation.
Concurrent ingestion may finish before or after that snapshot but cannot split one
response across database states.

SQL dialect differences are implementation details and never appear in the contract.
Tests run selection, pagination, limits, null timestamps, concurrent ingestion, and
metric parity against SQLite and PostgreSQL. Unsupported, old, new, or locally modified
database schemas fail closed before any reporting row is returned.

### Versioning and compatibility

The URL major version and payload contract version are independent and explicit. The
capabilities endpoint advertises supported snapshot schemas, dashboard query versions,
dashboard dataset versions, cursor versions, limits, and idempotency support.

Changing field meaning, required fields, privacy profile behavior, selection semantics,
pagination semantics, or metric definitions requires a new contract version. A server
may support consecutive versions during migration. Clients send exactly one supported
version and fail before a data request when no overlap exists. Unknown request fields
remain invalid. Additive response fields are introduced only when versioned client
fixtures prove older consumers safely ignore them; otherwise they require a new
dataset version.

Deploy server support first, then the Next.js BFF and CLI clients, then remove an old
version only after its usage window has ended. Mixed database-writer versions remain
unsupported under ADR 0001.

### Progressive delivery and rollback

Delivery proceeds in this order:

1. keep the browser-based offline continuity gate mandatory;
2. add read-only database extraction and `upload-db` using snapshot ingestion;
3. add scoped FastAPI reporting and export contracts;
4. introduce the shared pure TypeScript analytics package;
5. add the Next.js BFF and persistent UI behind an explicit deployment choice;
6. add the autonomous React offline runtime while retaining the Python renderer;
7. enable web-triggered offline export;
8. remove the old renderer only after detailed/share-safe parity, packaging,
   performance, privacy, and rollback validation.

Every phase can roll back its new consumer while the Python offline exporter remains.
No database downgrade is needed for this decision. A later schema change still follows
ADR 0001 and preserves both SQLite and PostgreSQL.

The migration completed with the React/Tailwind runtime as the sole standalone
renderer. Python continues to own dataset selection, minimization, limits, streaming,
atomic replacement, and the offline file contract; only the browser presentation
implementation changed. Detailed/share-safe Chromium gates, shared analytics fixtures,
packaging reproducibility, privacy assertions, and measured generation overhead passed
before the compatibility renderer and CLI selector were removed.

## Privacy and threat analysis

The approved inputs are only the normalized metadata fields enumerated in the dataset
contract. Their analytical purposes are filtering, time-series activity, provider
token semantics, latency/duration distributions, tool/workflow aggregation, context
pressure, configuration cohorts, delegation, and ingestion quality.

Prompts, responses, system or developer instructions, raw events, arbitrary provider
metadata, tool arguments/results, commands, patches, environment values, credentials,
database URLs, original paths, external response bodies, and exception messages remain
prohibited. Implementations add a synthetic secret canary and assert its absence from
snapshot extraction, SQL transfer, API bodies, HTML, browser state, logs, errors, and
fixtures.

The reporting API and detailed export still disclose private operational metadata:
provider, machine, project, model and tool labels, normalized roles and settings,
timestamps, durations, token counters, statuses, and activity aggregates. Share-safe
reduces label and timestamp precision but still reveals aggregate work patterns. TLS,
server-side authorization, BFF session security, cache prevention, access controls,
backups, retention, and reverse-proxy log redaction remain operator responsibilities.
Cookie-authenticated BFF mutations require same-origin validation and CSRF protection;
session cookies are `Secure`, `HttpOnly`, and `SameSite` constrained. The first
implementation is single-tenant: a credential with `read` can read the complete
authorized database, and no row-level tenant isolation is claimed.

## Consequences

- Next.js and browser code have no SQL or ingestion-token access.
- Upload, read, filters, pagination, detail, and export share explicit versioned
  contracts and privacy limits.
- Web and offline metrics derive from one minimized dataset and one shared pure
  calculation contract.
- Large selections fail closed instead of truncating graphs or leaking values through
  diagnostics.
- The persistent application adds operational complexity and a separate BFF session
  boundary, but the existing offline product remains independently deployable and
  reversible throughout migration.
