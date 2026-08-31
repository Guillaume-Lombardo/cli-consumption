# Changelog

Notable user-visible changes to CLI Consumption are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases
use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Version releases now attach the validated wheel and source distribution to an
  automatically generated GitHub Release after PyPI publication succeeds.

## [0.3.1] - 2026-08-31

### Changed

- Python 3.11 is now supported and tested alongside Python 3.12, 3.13, and 3.14.
- Collection errors now distinguish provider limits, incompatible formats, invalid
  snapshots, and unexpected adapter failures with privacy-safe human messages and
  deterministic JSON codes.
- A pinned, privacy-preserving single-host production deployment example now covers
  the central collector, PostgreSQL, and automatic TLS proxy operations.
- The README now leads with a reproducible synthetic dashboard preview and routes
  operational and provider-qualification detail to dedicated guides.
- CI now performs scheduled Python security, locked dependency, and offline workflow
  audits; third-party actions are pinned to immutable commits with least privileges.
- Provider adapters now carry auditable format qualification metadata backed by
  synthetic fixtures, with a weekly check that flags qualifications older than 90
  days and homogeneous privacy-minimized compatibility criteria.
- Linear is now the source of truth for new repository tasks and their progress, while
  the roadmap remains focused on durable product direction.
- Dashboard token totals now include conversation aggregates and latest-context
  snapshots for conversations overlapping the selected period, with explicit labels
  distinguishing them from additive usage.
- Dashboard selection, period, aggregation, percentile, and comparison calculations
  now use an independently tested JavaScript contract embedded in the offline report.
- Each detailed CSV export now uses a synchronized temporary file and atomic
  replacement, preserving that table's previous file when generation fails early.
- Multi-provider sync now reuses one HTTP client, negotiates capabilities once, and
  performs bounded idempotent retries when the collector advertises replay receipts.
- Sync automation now supports strict preflight refusal and deterministic JSON with
  malformed/duplicate diagnostics, explicit partial success, and generic remote
  errors that do not expose response or provider content.

### Fixed

- Reject timezone-naive provider timestamps instead of interpreting them in the host
  timezone.

## [0.3.0] - 2026-08-29

### Added

- Added privacy-safe database readiness checks and bounded request correlation for the
  collector service ([#35]).

### Changed

- Provider input processing now enforces cumulative discovery, byte, SQLite row, and
  structured-field limits across a complete collection ([#31]).
- Dashboard generation now preflights bounded selections, streams its output, and
  atomically replaces an existing report only after a successful write ([#32]).
- Concurrent schema initialization, upgrade, and downgrade are now serialized on
  SQLite and PostgreSQL, with SQLite lock waits capped at 15 seconds ([#34]).
- Published source distributions exclude repository-only tests and automation while
  retaining release metadata, the changelog and README, license notices, the runtime
  package, and Hatchling's rebuild `.gitignore` ([#33]).
- Identical adapter primitives are shared while provider-specific parsing semantics
  remain isolated ([#36]).

### Fixed

- Older, identical, graph-only, and partially stale snapshots can no longer erase a
  newer subagent relationship graph ([#30]).

## [0.2.1] - 2026-08-29

### Changed

- Hardened ingestion privacy and normalized persisted timestamps to canonical,
  fixed-width UTC values ([#29]).

## [0.2.0] - 2026-08-29

### Added

- Added versioned SQLite and PostgreSQL schema migrations, retention previews, and a
  metadata-only central collector and synchronization client ([#28]).
- Added streamed CSV exports, time-window selection, and a share-safe dashboard
  profile ([#28]).

### Changed

- Strengthened snapshot validation, transport defaults, storage deduplication, and
  privacy regression coverage ([#28]).

## [0.1.1] - 2026-08-27

### Added

- Added the Mistral Vibe CLI adapter ([#27]).

## [0.1.0] - 2026-08-27

### Changed

- Refreshed the provider guide for the first minor release ([#26]).

[Unreleased]: https://github.com/Guillaume-Lombardo/cli-consumption/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Guillaume-Lombardo/cli-consumption/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Guillaume-Lombardo/cli-consumption/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Guillaume-Lombardo/cli-consumption/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Guillaume-Lombardo/cli-consumption/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Guillaume-Lombardo/cli-consumption/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Guillaume-Lombardo/cli-consumption/compare/v0.0.18...v0.1.0
[#26]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/26
[#27]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/27
[#28]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/28
[#29]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/29
[#30]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/30
[#31]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/31
[#32]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/32
[#33]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/33
[#34]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/34
[#35]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/35
[#36]: https://github.com/Guillaume-Lombardo/cli-consumption/pull/36
