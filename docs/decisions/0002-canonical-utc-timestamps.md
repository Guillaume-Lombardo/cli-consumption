# ADR 0002: Canonical UTC timestamp storage

- Status: Accepted and implemented in schema revision `0003`
- Scope: normalized SQL timestamps and time-window queries

## Context

Snapshot schema v1 accepts timezone-aware ISO 8601 timestamps. Before revision `0003`,
SQL stored those values as text exactly as received. Reporting and retention therefore
called SQLite `datetime(...)` or cast PostgreSQL text to `timestamptz` for every
comparison. Those expressions prevented the existing timestamp indexes from serving
the common range predicates. Equivalent instants could also have different lexical
forms because offsets and fractional-second precision were not canonicalized.

The affected SQL fields are conversation and turn start/end times, model/tool/context/
compaction event timestamps, and ingestion-run timestamps. Millisecond epoch fields on
work items and subagent relationships already have a separate, explicit provider
meaning and are outside this decision.

An SQLite 3.53.1 query-plan check against the published schema confirmed that
`datetime(coalesce(...))` performs a table scan, while a direct comparison against a
canonical timestamp uses the timestamp index. This is a query-shape limitation, not a
SQLite version-specific parsing bug.

## Decision

Keep timezone-aware ISO strings in snapshot schema v1 and in CSV exports, but make the
stored representation canonical:

```text
YYYY-MM-DDTHH:MM:SS.ffffff+00:00
```

At the strict snapshot boundary, parse each timestamp, convert it to UTC, and serialize
it with `datetime.isoformat(timespec="microseconds")`. Generate ingestion timestamps
through the same helper. Fixed-width UTC strings sort in instant order on both SQLite
and PostgreSQL, remain human-readable, preserve Python datetime precision, fit the
existing `VARCHAR(64)` columns, and stay compatible with snapshot schema v1.

Do not replace the fields with epoch integers or PostgreSQL-native timestamps. Epoch
columns would either lose sub-millisecond precision or introduce JavaScript-safe-range
considerations, while native types would create avoidable dialect and CSV differences.

## Migration

1. Add revision `0003` without changing column names or snapshot schema.
2. Before mutation, read every non-null timestamp in bounded primary-key batches. Parse
   it with the same strict helper and fail with a generic schema-compatibility error if
   any published database contains an invalid or timezone-naive value.
3. Rewrite valid values to fixed-width UTC form. This changes representation, never the
   represented instant.
4. Add an index on `conversations.ended_at`; retain the existing start/event/run
   indexes.
5. Change reporting overlap predicates from function-wrapped columns to direct text
   predicates with explicit null branches. For example, “last activity at or after the
   lower bound” becomes `ended_at >= :since OR (ended_at IS NULL AND started_at >=
   :since)`.
6. Change retention predicates in the same way and bind canonical UTC strings on both
   dialects.
7. Keep dashboard and CSV values as canonical ISO strings; no export column changes are
   required.

The migration must run with writers stopped. An older writer could reintroduce a
non-canonical offset after revision `0003`, so the existing prohibition on mixed
application versions remains mandatory.

## Downgrade and rollback

The downgrade removes the new `ended_at` index but leaves timestamps canonical. The
original offset spelling and fractional precision cannot be reconstructed, although
the instant is preserved exactly. Restore the pre-upgrade backup if byte-for-byte
lexical rollback is required.

## Verification

- Property tests proving chronological and lexical ordering agree across offsets,
  daylight-saving transitions, negative offsets, and microsecond boundaries.
- Snapshot/API tests proving equivalent offsets serialize identically.
- SQLite upgrade tests for valid legacy values, invalid-value fail-closed behavior,
  idempotence, query results, and `EXPLAIN QUERY PLAN` index use.
- PostgreSQL migration and runtime tests for canonical backfill, direct predicates,
  indexes, and transaction rollback on malformed legacy data.
- Export-window and retention regression tests around exact half-open boundaries and
  null start/end combinations.
- Privacy assertions confirming rejected legacy values never appear in errors or logs.

## Consequences

- No snapshot-version bump, public field rename, or CSV compatibility break.
- Range queries become indexable and dialect-specific timestamp casts disappear.
- Stored timestamps no longer retain the provider's original offset notation; CLI
  Consumption analyzes instants, so that notation has no approved analytical purpose.
- Upgrading to revision `0003` performs a one-time bounded rewrite before the direct
  query predicates are used.
