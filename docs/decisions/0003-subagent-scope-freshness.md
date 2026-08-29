# ADR 0003: Conservative subagent-scope freshness

- Status: Accepted and implemented in schema revision `0004`
- Scope: provider-neutral subagent relationship replacement

## Context

Subagent relationships are authoritative per `(provider, source_machine)` scope, but
snapshot schema v1 has no graph revision or capture timestamp. Replacing the graph
before conversation deduplication allowed an older copied snapshot to erase newer
relationships even though its conversations were correctly skipped. An identical
conversation version, or a graph-only snapshot, also provides no evidence that its
graph is newer.

Concurrent ingestion must make the conversation comparison and graph replacement as
one serialized decision on both supported databases.

## Decision

Keep snapshot schema v1 unchanged and use conversation `event_count`, the existing
provider-neutral completeness signal, conservatively:

1. The first snapshot representing a scope may create its graph, including a
   graph-only scope.
2. Once the scope exists, replace its complete graph only when at least one incoming
   conversation has a strictly greater `event_count` than its stored copy and no
   incoming conversation has a lower count.
3. Identical versions, equal-count divergent hashes, graph-only snapshots, wholly
   older snapshots, and scopes mixing richer and stale conversations do not replace
   the graph.
4. A strictly richer authoritative snapshot with no edges represents a real deletion
   and removes the stored graph.

Revision `0004` adds `subagent_scopes`, keyed by provider and source-machine label,
with an internal `lock_version`. Ingestion inserts each first-seen scope and updates
that row before reading conversation versions. The write serializes competing scope
decisions under PostgreSQL row locking and SQLite's single-writer transaction model.
Scopes are acquired in sorted order to avoid cross-scope deadlocks.

The migration seeds scope rows from the union of existing conversations and subagent
relationships. The table is internal: it is absent from snapshot, API, CSV, dashboard,
and reporting surfaces. Retention deliberately preserves these replay guards; deleting
them would let an old graph-only copy become first-seen again. Their fields contain no
content beyond labels already allowed in normalized storage and an operational counter.

## Downgrade and deployment

Downgrade to revision `0003` drops only `subagent_scopes`; normalized relationships
remain intact. After a later upgrade, existing scopes are seeded again and therefore
require strict evidence before replacement.

Stop writers during migration and do not run mixed application versions. A pre-`0004`
writer neither acquires the scope lock nor follows this freshness policy.

## Consequences

- Older, identical, graph-only, and partially stale copies cannot regress a known
  relationship graph.
- A demonstrably richer snapshot can still remove relationships.
- A graph change unaccompanied by a strictly richer conversation is intentionally
  deferred because schema v1 cannot prove its ordering.
- Scope lock counters grow with ingestion but are not analytical data or exports.
