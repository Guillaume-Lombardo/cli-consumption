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
- Metadata-only subagent relationships, roles, status, timing, and token counters
- Content hashes and event counts used only for deduplication

## Prohibited data

- User prompts and assistant responses
- System or developer instructions
- Tool arguments, command lines, patches, and tool results
- Environment variables, credentials, access tokens, and authentication files
- Complete raw provider events or arbitrary metadata blobs
- Commands, exit output, file-change details, MCP arguments/results, and item content
- Raw rate-limit, credit, plan, or spend-control payloads
- Original working directories and rollout paths in shared exports

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
validation rejects unknown work categories, arbitrary statuses, malformed timestamps,
out-of-range counters, and unconstrained analytics labels before opening a transaction.

## Threat model

Provider files and incoming API payloads are untrusted. Parsers must tolerate malformed
records, constrain accepted fields, and never evaluate embedded content. The API uses a
constant-time bearer-token comparison when authentication is configured and refuses an
unauthenticated non-local bind through the CLI.

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
