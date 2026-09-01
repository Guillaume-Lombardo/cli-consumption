# Privacy and security boundary

CLI Consumption measures activity; it does not archive conversations.

The standalone renderer receives exactly the minimized dashboard dataset described
below. Its JavaScript and CSS are compiled locally, embedded in the HTML, and contain
no credential, environment value, provider file, request primitive, or external asset.
The React renderer adds no storage or API surface; filters and theme live only in the
open document. Detailed HTML still discloses approved project, machine, model, tool,
role, and timestamp metadata to anyone holding the file. `--share-safe` continues to
pseudonymize or coarsen those fields and is the appropriate controlled-sharing mode.

## Allowed data

- Provider, source-machine label, project category, and stable provider IDs
- Conversation and turn timestamps, status, and durations
- Model identifiers and token counters emitted by the provider
- Tool names and aggregate call counts
- Whitelisted work-item categories, normalized technical status, and timing
- Model input-to-context-window samples and bounded provider configuration labels
- Timestamped compaction counts without replacement content or window identifiers
- Metadata-only subagent relationships, normalized roles and status, timing, and token
  counters; provider nicknames are not retained
- Content hashes and event counts used only for deduplication
- Internal provider/source-machine scope rows and lock counters used only to serialize
  subagent graph freshness decisions; these rows are not exported

## Prohibited data

- User prompts and assistant responses
- System or developer instructions
- Tool arguments, command lines, patches, and tool results
- Environment variables, credentials, access tokens, and authentication files
- Complete raw provider events or arbitrary metadata blobs
- Commands, exit output, file-change details, MCP arguments/results, and item content
- Raw rate-limit, credit, plan, or spend-control payloads
- Original working directories and rollout paths in shared exports
- Provider-supplied subagent nicknames and arbitrary role, status, or source labels

Adapters may inspect a working directory transiently to apply an explicit project
mapping, but persistence records only the resulting project label and mapping source.
Tool arguments may be inspected transiently to identify nested tool names, but the
arguments themselves are discarded.

Work-item records are reduced to a fixed category, optional constrained tool name,
normalized technical status, relationship keys, and timestamps or durations. Error
objects, exit output, commands, paths, patches, messages, and item-specific payloads are
discarded. Context samples persist only the latest model-call input-token count and the
reported context-window size; cumulative payloads and rate-limit metadata are ignored.
Turn configuration labels accept only bounded identifier-like values. Snapshot
validation rejects unknown fields, unknown work categories, arbitrary roles or
statuses, malformed timestamps, inconsistent token compositions, out-of-range
counters, unconstrained analytics labels, broken relationships, snapshots above
250,000 records, and snapshot API requests above 32 MiB before opening a transaction. Errors
use generic codes and do not echo rejected values. Snapshot schema v1 is advertised by
the collector capabilities endpoint so an incompatible client can stop before upload.

Signed offline snapshot files contain exactly one or more instances of that existing
snapshot schema plus a fixed format name and version. Their Ed25519 signature protects
integrity and authenticity, not confidentiality: provider and machine labels, project
names, stable IDs, model and tool names, timestamps, token counters, statuses, and
activity aggregates remain visible to anyone who obtains the file. Private-key bytes
are read only to sign and are never written to the envelope, database, output, or log.
Verification occurs before bounded decompression and strict parsing. Signed files are
capped at 64 MiB, with 256 MiB decompressed, 64 snapshots, and 250,000 normalized
records in total; malformed inputs and key failures produce fixed error codes without
paths, payload values, or key contents. Newly created files use private permissions,
but operators remain responsible for trusted public-key distribution, key rotation,
access controls, retention, and secure deletion of transferred copies.

The collector transiently reads an optional `X-Request-ID` only when it matches a
bounded identifier grammar and otherwise replaces it with a generated identifier.
The identifier is returned as a response header and may appear in structured
application error events, but it is never persisted in the usage database or exports.
Those events contain only fixed event and error codes, constrained HTTP methods,
static route templates, and coarse allowlisted exception types. They exclude request
and response bodies, arbitrary headers, authentication tokens, URLs and query
strings, database URLs, paths, exception messages, and tracebacks. Uvicorn access logs
are disabled by the `serve` command; independently configured reverse-proxy logs stay
outside this application's retention boundary. A final ASGI exception boundary sits
outside FastAPI and consumes framework-re-raised request exceptions, preventing
Uvicorn's error logger from independently writing their messages or tracebacks.

Idempotent sync uses a separate client-generated canonical UUIDv4. It is unrelated to
provider content, machine paths, labels, and the request-correlation identifier. The
collector persists only that opaque UUID and its ingestion-run foreign key in an
internal `sync_receipts` table; it is absent from CSV, dashboards, API error bodies,
and logs, and retention deletes it with the associated ingestion run. Capabilities
are negotiated once per endpoint. Retries are limited to three attempts and are
enabled only when the collector advertises this receipt mechanism, so a legacy server
is never retried after an ambiguous transport failure.

`upload-db` uses a distinct content-bound UUIDv4 derived from a domain-separated digest
of each canonical minimized snapshot. This lets separate invocations resume safely and
lets richer snapshots replace older copies without duplicating identical uploads. The
key discloses equality of identical provider fragments to the collector, which already
receives those fragments, but reveals neither their content nor the local database path.
It is sent only in the idempotency header and is never printed or logged. Receipt
capability is required before the first upload.

Sync automation output contains only provider names, opaque ingestion-run IDs,
received/written/skipped counts, local malformed and duplicate counts, bounded status
labels, and fixed error codes. Partial multi-provider results preserve successful
entries without including remote response bodies or exception text. Strict mode
refuses every upload before creating the HTTP client when any local snapshot reports
malformed records. Paths, snapshot payloads, tokens, provider record values, and
collector error details are excluded from both human and JSON output.

Read-only database extraction reconstructs only the existing strict snapshot-schema-v1
fields. It keeps the approved detailed operational labels, stable provider-qualified
IDs, content hashes, relationships, token counters, statuses, and timestamps because
the receiving ingestion path needs them for deterministic replacement. It excludes
the SQLite file and sidecars themselves, database paths, Alembic state,
`ingestion_runs`, replay receipts, subagent-scope locks, SQL metadata, and exception
text. A SQL byte/row preflight runs before values are materialized, the reconstructed
payload is validated again, and every failure exposes only a fixed code. Project and
machine labels plus activity timestamps remain private operational disclosure; this
transfer is minimized, not anonymous.

## Threat model

Provider files and incoming API payloads are untrusted. Parsers must tolerate malformed
records, constrain accepted fields, and never evaluate embedded content. The API uses a
constant-time bearer-token comparison when authentication is configured and refuses an
unauthenticated non-local bind through the CLI.

Local parsing caps monolithic JSON files at 64 MiB, JSONL files at 256 MiB with an
8 MiB per-line limit, 512 MiB of actual provider-file reads, and candidate discovery at
10,000 entries per provider collection. JSON and JSONL descriptors refuse symlinks,
verify file identity, and count bytes during reading. Untrusted provider SQLite inputs
share 512 MiB cumulatively across databases and active WAL, SHM, or rollback-journal
sidecars, 250,000 selected rows, 8 MiB per structured field, and 256 MiB for structured
fields. SQLite length preflights apply before values reach Python. Database and sidecar
identities and growth are checked around extraction while retaining live WAL support;
the residual transient-mutation race is documented in the architecture. The
normalized snapshot remains capped at 250,000 records during construction. Limit
failures expose only generic codes, never paths or record content. The sync client
requires HTTPS beyond loopback unless the operator uses the explicit
`--allow-insecure` override.

A normalized database selected for snapshot extraction is also untrusted. Extraction
requires the exact current revision and physical layout without running migrations,
uses an explicit read-only transaction that includes live committed WAL data, and caps
the selection at 10,000 conversations, 250,000 records, and 128 MiB before reading its
rows. Strict snapshot validation rejects invalid identifiers, relationships, token
composition, timestamps, models, and labels after reconstruction. Tests place a
synthetic secret in excluded ingestion, receipt, and scope rows and assert that it is
absent from snapshots, errors, and logs.

Database upload output contains only provider names, bounded status labels, fixed error
codes, opaque ingestion-run IDs, and aggregate counts. It excludes the database path,
time-bound values, endpoint URL, token and token environment value, idempotency key,
snapshot payload, remote response body, and exception text. Default mode preserves
successful provider results after another provider fails; strict mode stops before later
providers and reports only the fixed `strict_upload_stopped` code for them.

The unauthenticated liveness endpoint does not touch the database. The unauthenticated
readiness endpoint reads only a bounded schema revision and fixed table probes. It
uses one consolidated query and a two-second application response deadline, returning
generic ready/not-ready state without database values or errors. PostgreSQL uses a
dedicated unpooled engine with startup connection, statement, and lock timeouts. At
most one daemon probe can continue after a timed-out response; concurrent probes
return not-ready without opening another connection, and shutdown does not wait for
that residual operation.
These endpoints expose process and database availability to callers and should be
scoped by network policy even though they disclose no usage metadata.

An exported database still reveals work patterns, model choices, project names, and
activity times. Treat it as private operational data. Restrict filesystem and database
access, use TLS for remote collection, rotate tokens, and define a retention policy
appropriate to the environment.

The self-contained dashboard embeds only the normalized fields needed for its charts
and filters. It replaces conversation and turn identifiers with document-local numeric
keys and excludes provider IDs, content hashes, source values, project mapping sources,
subagent nicknames, and ingestion-run IDs. Derived rates, percentiles, tool categories,
and period comparisons are computed inside the document. The remaining project names,
machine labels, model names, tool names, roles, token counters, statuses, and activity
timestamps are still private operational metadata.

The normal `export` command writes only the HTML dashboard; detailed CSV tables require
the explicit `--csv` option. `export --share-safe` rejects that option. Within its HTML
document, machine, project, model, and subagent-role labels are replaced with local
aliases; tools are reduced to broad categories; timestamps are rounded to UTC days;
and the comparison table hides cohorts smaller than five closed turns. The underlying
pseudonymized per-turn rows remain embedded so local filtering works. Provider names,
daily activity, durations, counts, token counters, configuration labels, statuses, and
aggregate work patterns remain disclosed. Share-safe is a minimization profile, not
anonymization.

The reporting API uses the same minimized row contract behind scoped FastAPI bearer
credentials. The ingestion credential never gains read access implicitly, and export
requires a credential carrying both `read` and `export`. Next.js is a server-side BFF
and never receives a database URL or exposes FastAPI credentials to the browser. Reporting
requests place filters in bounded POST bodies rather than URLs; responses replace SQL
and provider identifiers with response-local keys or integrity-protected opaque
references. Stable IDs, content hashes, source values, mapping sources, receipt keys,
scope rows, prompts, responses, tool arguments, raw events, credentials, paths, and
arbitrary metadata remain excluded. The exact fields, scopes, limits, cursors, generic
errors, compatibility rules, and residual disclosures are recorded in
[ADR 0004](decisions/0004-persistent-dashboard-contracts.md).

The persistent dashboard renders project, machine, provider, model, tool, role, status,
timestamp, token, and workflow aggregates only after authentication; those values
remain private operational metadata. Its URL may contain the selected period and UTC
date bounds, which can consequently reach browser history or an explicitly enabled
access log, but never operational labels, opaque conversation references, or cursors.
The BFF uses an eight-hour signed HTTP-only, same-site session cookie and stores no
collector token or session value in browser storage. Only the theme preference is kept
in local storage. Login and reporting mutations require an exact configured origin;
responses are `no-store`, bodies are bounded, and upstream response bodies, exception
text, credentials, and request values are reduced to fixed generic errors. The server
still receives selected operational labels in POST bodies and returns them to the
authenticated browser, so TLS, log minimization, session-secret rotation, workstation
access control, and an appropriate collector retention policy remain operator duties.

Offline downloads reuse the exact bounded POST query visible in the dashboard and add
only the selected `detailed` or `share-safe` profile. A dedicated server-side token
carrying `read` and `export` never reaches browser state or the generated file. The BFF
buffers at most 128 MiB before sending any HTML, uses fixed generic errors and
`no-store` headers, and recommends narrowing an oversized selection. FastAPI writes a
mode-0600 temporary, renders from one coherent SQL snapshot, and removes the temporary
after success, error, or client cancellation. The resulting HTML remains a portable
copy: detailed exports disclose the selected operational metadata, while share-safe
exports retain the documented aggregate-disclosure boundary.

Dashboard generation first evaluates only aggregate row counts and scalar byte
lengths for the selected report. These internal estimates are not exported or logged.
The command refuses selections above 250,000 rows or 128 MiB of selected scalar values,
bounds its required local-key and alias indexes, and charges every JSON/HTML chunk
before writing up to 128 MiB. It never materializes the full report or document in the
production path. The index budget charges persistent Python object allocations before
insertion and avoids set-and-sort duplication for project, machine, and role aliases;
it reveals no labels because only its private byte counter is retained. Its public
limit error is generic and recommends a bounded time
window without echoing project or machine labels, identifiers, paths, or other database
values. Reporting pagination keeps stable internal membership and database identifiers
only in bounded, expiring process memory; external cursors and conversation references
are random opaque handles. Responses use `Cache-Control: no-store`, and validation,
authorization, cursor, limit, timeout, busy, and database failures expose fixed codes
without request values or exception text. `--json` returns only a deterministic error code and generic hint. Atomic
replacement prevents a failed generation from truncating a previous dashboard. Each
CSV also replaces its own previous file atomically, but CSV files remain separate
detailed exports and earlier tables may have been replaced before a later table or
dashboard failure. Replacement preserves an existing CSV's file mode; new CSV files
retain private temporary-file permissions.

## Public synthetic demo

The repository preview is generated only by `docs/demo/generate.py`. Its provider,
machine, project, model, timestamp, token, tool, and relationship values are explicit
invented constants; it never invokes an adapter or reads provider directories. The
temporary SQLite database is created outside the repository and removed after the
self-contained HTML is written. A regression test rebuilds the HTML byte for byte,
checks privacy canaries and network primitives are absent, and validates the tracked
PNG preview. The packaging contract excludes `docs/`, tests, generated demo assets,
and every other repository-only file from wheels and source distributions.

The TypeScript workspace consumes only the already minimized, response-local
`DashboardDataset v1` contract. Its analytics package has no DOM, storage, or network
primitive and cannot open provider files or databases. Only the generated React
browser bundle and Tailwind stylesheet enter Python distributions; workspace sources,
tests, dependency caches, and ESM build directories remain repository-only. Synthetic
fixtures contain invented operational labels and assert contract rejection without
copying prompts, responses, tool arguments, credentials, paths, or stable identifiers.

Time filters select complete conversations that overlap the requested window. They do
not redact child records whose individual timestamps fall outside it; related subagent
edges can also reveal activity around the boundary. Ingestion-run rows are selected by
their own timestamp. CSV output neutralizes leading spreadsheet formula and control
prefixes with an apostrophe, but still contains detailed normalized operational data.

Provider diagnostics inspect local stores transiently and emit only provider name,
documented aliases, support state, documented token semantics, and one coarse
compatibility status. They do not persist snapshots or reveal paths, conversation
identifiers, record counts, malformed values, or exception text.

Every registered adapter has a synthetic canary regression at the extraction boundary.
Shared contract tests then check that prohibited content is absent from snapshots, SQL,
API requests and errors, CSV, HTML, logs, and malformed-input diagnostics. This layered
contract is required for every newly registered provider.

Retention removes normalized rows, not provider source files, existing exports,
database backups, database engine logs, reverse-proxy logs, or snapshots already sent
elsewhere. A dry run reports aggregate deletion counts; `--apply` is required to
delete. Operators remain responsible for retention and secure disposal of those
residual copies. Project names, machine labels, model/tool names, stable IDs,
timestamps, and activity aggregates remain sensitive wherever normalized databases or
detailed CSV files survive. Internal subagent-scope replay guards also survive
retention and retain provider and source-machine labels plus a lock counter; they are
not included in reports or exports.
