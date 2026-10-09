# ADR 0010: Reporting-time deduplication of replicated model calls

## Status

Accepted on 2026-10-09 by the maintainer. Implementation is tracked by G1L-642.

## Context

Claude Code can copy one model response into several session transcripts, for example
when a session is resumed or forked. The copies keep the original message identifier,
request identifier, and timestamp. Counting each copy inflates token totals and, since
[ADR 0008](0008-estimated-cost.md), estimated cost.

Deduplicating during ingestion would make totals depend on collection order. Offline
copies from several machines can deliver a copy before its original. Claude Code also
deletes transcripts older than its `cleanupPeriodDays` setting, so a later collection
may only see the copy while the database still holds the original. Moving token
ownership between conversations after the fact would break idempotent replacement.

## Decision

- Each normalized model call may carry an optional `dedup_key`: a SHA-256 digest of the
  canonical provider name and the provider's response and request identifiers. Raw
  identifiers are never stored in this column. Providers that do not replicate
  responses leave it null.
- Ingestion stores every observed copy unchanged, so it stays idempotent and
  independent of order.
- Reporting, exports, and dashboards count one call per non-null `dedup_key`: the
  earliest by timestamp, then by stable conversation identifier, using a window
  function that works on SQLite and PostgreSQL. Calls with a null key are always
  counted.
- Token totals shown for a conversation derive from its owned model calls rather
  than from stored conversation counters whenever the provider can replicate calls.
- Claude Code sidechain replays that repeat a parent message identifier are dropped by
  the adapter before storage, because they are never independent usage.

## Consequences

Totals become order-independent and survive provider-side deletion of originals. The
schema gains one nullable, indexed column through a SQLite and PostgreSQL migration.
Reporting queries gain a deduplication step, and stored conversation token counters no
longer serve as the authoritative total for replicating providers.
