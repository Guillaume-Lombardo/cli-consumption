# ADR 0001: Versioned schema migrations

- Status: Accepted
- Scope: SQLite and PostgreSQL normalized storage

## Context

`create_all` can create missing tables but cannot safely evolve published columns,
constraints, indexes, or data. CLI Consumption must preserve repeatable ingestion on
both supported databases while rejecting unknown schemas rather than guessing how to
modify them.

## Decision

Package Alembic revisions with the application and upgrade the database to the known
head whenever storage is initialized. Every schema change must provide SQLite and
PostgreSQL coverage, deterministic data conversion, and an explicit downgrade boundary.

For databases created before revision tracking, inspect every recognized table before
adoption. Stamp and migrate only an exact published layout, including explicitly known
transitional layouts. Refuse databases with unknown columns, missing columns, unknown
revision heads, or revisions newer than the running application. Do not fall back to
`create_all` after a compatibility failure.

The initial baseline records the published normalized schema. The next migration
normalizes retained subagent roles and statuses and removes `agent_nickname`, which has
no required analytical purpose. Its downgrade can recreate the column only with a
content-free placeholder; it cannot recover discarded nicknames.

Revision `0004` adds internal subagent-scope coordination rows and seeds them from
existing conversation and relationship scopes. New and old application versions must
not write concurrently: older writers do not take the scope lock or apply the
conservative graph-freshness policy. Downgrade removes only this internal table and
does not alter normalized conversations or relationships. An unversioned database
whose complete `0004` layout passes the strict adoption preflight retains its validated
scope rows and is stamped directly at head. Older recognized layouts still replay the
required forward migrations.

## Deployment and rollback

Back up a production database before upgrading. Stop or drain writers, deploy and
initialize the server first, then upgrade sync clients. Do not run mixed application
versions against a database during migration: an older writer may not understand the
new layout or replacement semantics.

Use the packaged downgrade only to a documented known revision and only after stopping
newer writers. A schema downgrade does not guarantee application-level recovery when a
migration deliberately discarded or transformed metadata. Restore the pre-upgrade
backup when exact rollback is required.

## Consequences

- Fresh, legacy SQLite, legacy PostgreSQL, and already-versioned databases follow one
  migration history.
- Unknown or locally modified schemas fail closed with a generic compatibility error.
- Releases that change storage must include migration, adoption, idempotency,
  upgrade/downgrade, and both-dialect tests.
- Operators gain automatic upgrades but remain responsible for backups, writer
  coordination, and retention of those backups.
