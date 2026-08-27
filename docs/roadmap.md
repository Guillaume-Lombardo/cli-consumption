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

## Next provider increments

1. Extend existing adapters only where local metadata has reliable, testable semantics.
2. Add provider-format health checks and compatibility reporting.
3. Expand cross-provider comparisons without treating unavailable dimensions as zero.

## Later operational work

- Versioned database migrations and retention commands
- Signed/compressed offline snapshot files
- Server deployment examples and read-only reporting API
- Cost estimates driven by explicit, versioned pricing inputs
