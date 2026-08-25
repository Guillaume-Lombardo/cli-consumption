---
name: add-cli-adapter
description: Add or update an AI coding CLI provider adapter for CLI Consumption. Use for provider discovery, local file or endpoint parsing, normalized conversation/turn/model/tool mapping, provider capability status, compatibility fixtures, and adapter documentation.
---

# Add a CLI adapter

1. Verify the provider's current data sources and terms from primary documentation.
2. Record which fields are stable, optional, inferred, or unavailable. Never invent a
   cross-provider equivalence.
3. Add synthetic fixtures covering the smallest supported format, malformed input, and
   a realistic multi-turn conversation. Do not copy personal provider data.
4. Implement the adapter contract under `src/cli_consumption/adapters/`. Keep all
   provider-specific logic there and emit only the normalized snapshot fields.
5. Apply `$audit-usage-privacy` to the input fields, transient parsing, errors, logs,
   snapshot payload, SQL rows, CSV, and dashboard.
6. Test stable IDs, duplicate copies, model attribution, token semantics, tool names,
   partial records, and format changes.
7. Register the adapter in the CLI only after its tests pass. Update
   `docs/provider-support.md` with precise limitations.
8. Run every quality gate in `AGENTS.md`.
