---
name: evolve-storage-schema
description: Evolve CLI Consumption persistence safely across SQLite and PostgreSQL. Use for tables, columns, indexes, constraints, migrations, retention, deduplication, stable identifiers, ingestion transactions, or storage compatibility changes.
---

# Evolve the storage schema

1. Define the provider-neutral meaning, nullability, cardinality, and lifecycle of each
   proposed field before editing the schema.
2. Apply `$audit-usage-privacy`; reject fields that expose content or machine-private
   paths without an explicit accepted design change.
3. Specify upgrade, downgrade, and mixed-version behavior. Introduce a migration tool
   before the first incompatible released schema change.
4. Preserve provider-qualified stable IDs and atomic replacement of a conversation with
   its child records.
5. Test SQLite directly. Test PostgreSQL for SQL, types, constraints, indexes, and
   transaction behavior whenever the change can differ by dialect.
6. Cover repeated ingestion, less-complete duplicates, more-complete replacements,
   partial failure, empty datasets, and concurrent writers as applicable.
7. Update architecture, privacy, export, and API documentation together with the code.
8. Run every quality gate in `AGENTS.md`.
