# Privacy and security boundary

CLI Consumption measures activity; it does not archive conversations.

## Allowed data

- Provider, source-machine label, project category, and stable provider IDs
- Conversation and turn timestamps, status, and durations
- Model identifiers and token counters emitted by the provider
- Tool names and aggregate call counts
- Metadata-only subagent relationships, roles, status, timing, and token counters
- Content hashes and event counts used only for deduplication

## Prohibited data

- User prompts and assistant responses
- System or developer instructions
- Tool arguments, command lines, patches, and tool results
- Environment variables, credentials, access tokens, and authentication files
- Complete raw provider events or arbitrary metadata blobs
- Original working directories and rollout paths in shared exports

Adapters may inspect a working directory transiently to apply an explicit project
mapping, but persistence records only the resulting project label and mapping source.
Tool arguments may be inspected transiently to identify nested tool names, but the
arguments themselves are discarded.

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
