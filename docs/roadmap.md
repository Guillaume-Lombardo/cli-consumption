# Roadmap

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

1. Extend existing adapters only where local metadata has reliable, testable semantics.
2. Deepen provider-format diagnostics without exposing provider contents or paths.
3. Expand cross-provider comparisons without treating unavailable dimensions as zero.

## Later operational work

- Signed/compressed offline snapshot files
- Server deployment examples and read-only reporting API
- Cost estimates driven by explicit, versioned pricing inputs
