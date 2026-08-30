# Roadmap

This document records durable product direction. The
[CLI Consumption Linear project](https://linear.app/g1lom/project/cli-consumption-b84515055d16)
is the source of truth for actionable tasks, priorities, ownership, blockers, and
current progress. Every new task must be created there, moved to `In Progress` when
work starts, kept current throughout implementation, and moved to `Done` only after
its acceptance criteria and validation are complete.

## Current foundation

- Metadata-only adapters for all providers listed in
  [Provider support](provider-support.md), including local stores and offline copies of
  self-hosted Plandex data
- Offline multi-machine deduplication
- SQLite and PostgreSQL storage
- Metadata-only central collection API
- Opt-in CSV exports, a self-contained dashboard, and a share-safe dashboard profile
- Context-pressure, work-item reliability, configuration-cohort, and delegation views
- Strict snapshot schema v1 with bounded API ingestion and capability negotiation
- Versioned SQLite/PostgreSQL migrations, safe legacy adoption, and retention previews
- Central provider registry with privacy-minimized compatibility diagnostics
- Deterministic, streamed, time-bounded exports with spreadsheet-safe CSV cells
- Exact legacy-schema adoption, bounded provider inputs, authoritative subagent-scope
  replacement, and HTTPS-by-default synchronization
- Deterministic JSON results for collection, export, and retention automation
- Canonical fixed-width UTC timestamp storage with bounded legacy migration and
  indexable reporting and retention predicates

## Next provider increments

1. Stabilize and periodically qualify existing adapters before adding another
   provider.
2. Extend existing adapters only where local metadata has reliable, testable
   semantics.
3. Deepen provider-format diagnostics without exposing provider contents or paths.
4. Expand cross-provider comparisons without treating unavailable dimensions as zero.

## Later operational work

- Signed/compressed offline snapshot files
- Server deployment examples and read-only reporting API
- Cost estimates driven by explicit, versioned pricing inputs, deferred until token
  semantics are sufficiently comparable across providers
