---
name: audit-usage-privacy
description: Audit CLI Consumption changes for metadata minimization and sensitive-content leakage. Use for adapters, API payloads, storage, errors, logging, exports, dashboards, fixtures, documentation, or any change that handles provider files or conversation-derived data.
---

# Audit usage privacy

1. Enumerate every input field read and every output surface changed: memory snapshot,
   SQL, API, CSV, HTML, logs, errors, fixtures, and documentation examples.
2. Require an explicit analytical purpose for each persisted field. Prefer derived
   labels and counts over raw values.
3. Reject prompts, responses, instructions, raw events, tool arguments or results,
   commands, patches, environment values, credentials, original paths, and arbitrary
   metadata blobs.
4. Allow transient inspection only when required to derive an approved value, then
   discard the source content immediately.
5. Check malformed-input errors and debug paths for accidental raw-record output.
6. Add a synthetic canary secret to fixtures and assert that it is absent from
   snapshots, databases, API bodies, CSV files, dashboards, and logs.
7. Re-read `docs/privacy.md` and update it if the approved boundary changes.
8. Report each reviewed surface and any residual operational disclosure such as project
   names or activity timestamps.
