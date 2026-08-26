# Roadmap

## Current foundation

- Complete Codex local collector
- Core Claude Code local transcript collector
- Core Pi local session collector
- Core Kilo Code local SQLite collector
- Core Gemini CLI local chat-history collector
- Core Goose local SQLite collector
- Core Qwen Code local transcript collector
- Core Crush per-project SQLite collector
- Core Amp local thread-mirror collector
- Offline multi-machine deduplication
- SQLite and PostgreSQL storage
- Metadata-only central collection API
- Opt-in CSV exports, a self-contained dashboard, and a share-safe dashboard profile
- Context-pressure, work-item reliability, configuration-cohort, and delegation views

## Next provider increments

1. Extend Claude Code support only where local metadata has reliable semantics.
2. Add OpenCode with the same normalized contract.
3. Add cross-provider comparison views once at least two adapters expose reliable model
   and token dimensions.

## Later operational work

- Versioned database migrations and retention commands
- Signed/compressed offline snapshot files
- Server deployment examples and read-only reporting API
- Cost estimates driven by explicit, versioned pricing inputs
- Provider format health checks and compatibility reporting
