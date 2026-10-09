# ADR 0009: Optional user identity and external usage sources

## Status

Accepted on 2026-10-09 by the maintainer. Implementation is tracked by G1L-661,
G1L-662, G1L-663, and G1L-666.

## Context

Team deployments want per-user views and usage sources beyond local provider files:
OpenTelemetry metrics emitted by the CLIs, vendor admin APIs (Anthropic, GitHub
Copilot, Cursor), local git activity, and a read-only MCP server for agents. Several of
these sources carry user emails or account identifiers, which the current privacy
boundary does not collect.

## Decision

### User identity is optional and off by default

- No identity is collected unless an operator explicitly enables it for a source.
- When disabled, identity attributes are dropped at ingestion, before validation
  errors, logs, or storage can observe them.
- When enabled, identity is stored as a separate, explicitly labeled dimension. The
  operator chooses between a keyed pseudonym (HMAC with a deployment secret) and the
  raw identifier; pseudonymization is the recommended default.
- Share-safe output never contains raw identities.
- `docs/privacy.md` must describe each identity field, its source, the setting that
  enables it, and its retention before any implementation ships.

### External sources

- **OpenTelemetry**: the collector accepts OTLP/HTTP metrics only, through a dedicated
  scope, with an allowlist of metric names and attributes. OTel logs and events are
  rejected because they can carry prompts. Collected provider files remain
  authoritative for tokens: OTLP token metrics whose session identifier matches a
  collected conversation are excluded from totals. Metrics that files do not provide,
  such as lines of code, commits, pull requests, edit decisions, and active time, always
  come from OTLP.
- **Vendor admin APIs**: imports are read-only, use secrets supplied by the deployment
  environment, never persist those secrets, and keep vendor data in tables separate
  from locally collected conversations.
- **Git activity**: counters (commits, lines added and deleted, files changed) are read
  on the machine that holds the repository, during collection, and travel in the same
  snapshot under the same project label. The label derives from the normalized `origin`
  URL so that clones on different machines match. No project-to-repository mapping is
  attempted centrally. Each commit carries a truncated SHA-256 fingerprint of its commit
  identifier so that clones on several machines are counted once. Commit identifiers,
  messages, file names, and branch names are never stored. Commits may be filtered to
  the local `user.email` at collection time without storing it; authors are stored only
  when optional identity is enabled.
- **MCP server**: it runs over stdio, reads local aggregates, and returns detailed
  labels by default because the calling agent already sees the same project context. A
  share-safe option pseudonymizes labels. Responses never include paths, conversation
  identifiers, or user identity, and `docs/privacy.md` must state that MCP results are
  sent to the model provider.
- File-collected and externally reported usage stay distinguishable so that totals are
  never double counted.

### Dependencies

The project accepts optional extras for an MCP server SDK and for OTLP protobuf
decoding. They are not installed by default.

## Consequences

The privacy boundary gains an opt-in identity dimension and three ingestion paths.
Each one needs SQLite and PostgreSQL migrations, adversarial input tests, and privacy
assertions for every new field.
