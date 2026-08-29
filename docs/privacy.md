# Privacy and security boundary

CLI Consumption measures activity; it does not archive conversations.

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
250,000 records, and API requests above 32 MiB before opening a transaction. Errors
use generic codes and do not echo rejected values. Snapshot schema v1 is advertised by
the collector capabilities endpoint so an incompatible client can stop before upload.

The collector transiently reads an optional `X-Request-ID` only when it matches a
bounded identifier grammar and otherwise replaces it with a generated identifier.
The identifier is returned as a response header and may appear in structured
application error events, but it is never persisted in the usage database or exports.
Those events contain only fixed event and error codes, constrained HTTP methods,
static route templates, and coarse allowlisted exception types. They exclude request
and response bodies, arbitrary headers, authentication tokens, URLs and query
strings, database URLs, paths, exception messages, and tracebacks. Uvicorn access logs
are disabled by the `serve` command; independently configured reverse-proxy logs stay
outside this application's retention boundary.

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

The unauthenticated liveness endpoint does not touch the database. The unauthenticated
readiness endpoint reads only a bounded schema revision and fixed table probes; it
returns generic ready/not-ready state without database values or errors. These
endpoints expose process and database availability to callers and should be scoped by
network policy even though they disclose no usage metadata.

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
values. `--json` returns only a deterministic error code and generic hint. Atomic
replacement prevents a failed generation from truncating a previous dashboard. With
`--csv`, CSV files remain separate detailed exports and may have been written before a
later dashboard failure.

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
