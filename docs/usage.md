# Usage and operations

This guide expands the quick start with collection, reporting, storage, and central
sync details. Read the [privacy boundary](privacy.md) before copying provider data or
sharing any output.

## Collect trusted offline copies

`--source [LABEL=]PATH` points to a provider home directory and can be repeated. With
`--provider all`, every path is inspected and unmatched sources are rejected. With one
provider selected, every path must contain that provider's expected store.

```bash
uv run cli-consumption collect --provider codex \
  --source desktop=/data/codex/desktop \
  --source laptop=/data/codex/laptop \
  --source server=/data/codex/server \
  --database usage.sqlite
```

Copy only the provider directory named in the [support ledger](provider-support.md),
never adjacent credentials. Globally identical conversation IDs are deduplicated and
the most complete copy wins. Subagent graphs are replaced only by a demonstrably more
complete snapshot for the same provider and source machine.

Map original working-directory prefixes to stable project labels. The longest matching
prefix wins:

```bash
uv run cli-consumption collect --provider codex \
  --source desktop=/data/codex/desktop \
  --project cli-consumption=/home/me/dev/cli-consumption
```

Plandex auto-detection checks `/plandex-server`. Pass any other trusted offline copy of
a self-hosted server directory explicitly:

```bash
uv run cli-consumption collect --provider plandex \
  --source server=/srv/plandex-server --database usage.sqlite
```

Provider inputs are untrusted. Monolithic JSON is capped at 64 MiB, JSONL at 256 MiB
with an 8 MiB line limit, actual provider reads at 512 MiB, and discovery at 10,000
candidates per collection. SQLite inputs share a cumulative 512 MiB file-and-sidecar
limit, 250,000 selected rows, 8 MiB per structured field, and 256 MiB across structured
fields. A snapshot may contain at most 250,000 normalized records. Direct provider-file
symlinks are refused. Add `--strict` to refuse ingestion when malformed records were
skipped.

## Transfer signed offline snapshots

Install the `snapshots` extra on both machines. Generate an Ed25519 PEM key pair with
your approved key-management tooling, keep the private key only on the source machine,
and copy the public key to the destination through a trusted channel. Then create a
compressed metadata-only file without copying the raw provider store:

```bash
uv run cli-consumption snapshot create --provider all --strict \
  --signing-key /secure/source-private.pem \
  --output /transfer/usage.snapshot
```

Verify the signature and ingest every included provider snapshot through the same
idempotent storage path as `collect`:

```bash
uv run cli-consumption snapshot ingest \
  --input /transfer/usage.snapshot \
  --verification-key /secure/source-public.pem \
  --database usage.sqlite
```

Signature verification happens before decompression and parsing. Signed files are
limited to 64 MiB, with 256 MiB decompressed, 64 snapshots, and 250,000 normalized
records in total. New outputs use mode `0600`; replacing an existing regular file
preserves its mode and leaves the previous file intact if installation fails. The
envelope is deterministic for identical snapshots and key, but it is signed rather
than encrypted: anyone holding the file can read its private operational metadata.
Protect snapshot files like detailed CSV or a normalized database, rotate signing
keys according to local policy, and remove transferred copies according to the
applicable retention policy. The application never prints or stores private-key
contents.

## Explore and share reports

The dashboard filters by time, provider, machine, project, and model. It covers token
composition, cache efficiency, latency and duration percentiles, turn rate, context
pressure, work-item reliability, configuration cohorts, compactions, delegation, and
ingestion quality where the selected providers expose those dimensions.

Generate a more shareable dashboard by pseudonymizing labels, grouping tool names,
rounding timestamps to days, and hiding small cohorts:

```bash
uv run cli-consumption export --output shared-report --share-safe
```

Share-safe reports still disclose aggregate work patterns. CSV is never a share-safe
format. Limit an export to conversations overlapping a half-open UTC window:

```bash
uv run cli-consumption export --output reports \
  --since 2026-08-01 --until 2026-09-01
```

Dates denote UTC calendar boundaries; timestamps must include a timezone. An included
conversation retains its complete child graph. CSV rows use stable primary-key order,
and spreadsheet formula prefixes in text cells receive a leading apostrophe.

Dashboard generation preflights at 250,000 rows and 128 MiB of selected scalar values;
the final HTML is capped at 128 MiB. Narrow large databases with `--since` and
`--until`. Every output uses a synchronized temporary file and atomic replacement. A
combined `--csv` export is atomic per file, not across the whole directory, so an early
CSV can be replaced before a later table or dashboard fails.

## SQLite, PostgreSQL, migrations, and retention

A file path selects SQLite; a SQLAlchemy URL selects PostgreSQL:

```bash
uv run cli-consumption collect --provider all --database usage.sqlite
uv run cli-consumption collect --provider all \
  --database postgresql+psycopg://usage@localhost/cli_consumption
```

Pass credentials through environment variables or a secret manager rather than shell
history. `CLI_CONSUMPTION_DATABASE` can supply the database setting.

Commands upgrade schemas automatically. Exact published legacy schemas can be adopted;
unknown or modified schemas are refused. Back up production databases before upgrades,
stop or drain writers, and never run mixed application versions through one migration.
The [migration decision](decisions/0001-versioned-schema-migrations.md) documents
rollback and compatibility rules.

Preview retention before applying it:

```bash
uv run cli-consumption retention --keep-days 90 --database usage.sqlite
uv run cli-consumption retention --keep-days 90 --database usage.sqlite --apply
```

The dry run reports what would be removed. `--apply` deletes old normalized metadata,
not provider sources or existing exports. Internal replay guards remain to prevent an
older graph-only copy from recreating retained relationships.

## Central collector and synchronization

Copied files are simplest for personal or air-gapped use. For recurring collection,
start the metadata-only API:

```bash
export CLI_CONSUMPTION_API_TOKEN="$(your-secret-provider)"
uv run cli-consumption serve \
  --database postgresql+psycopg://usage@localhost/cli_consumption \
  --host 0.0.0.0
```

Send locally detected snapshots from another machine:

```bash
export CLI_CONSUMPTION_API_TOKEN="$(your-secret-provider)"
uv run cli-consumption sync --provider all \
  --endpoint https://usage.example.test
```

For automation, require clean parsing and emit one deterministic result:

```bash
uv run cli-consumption sync --provider all --strict --json \
  --endpoint https://usage.example.test
```

Upload an existing normalized SQLite database without copying the database or its
sidecars:

```bash
uv run cli-consumption upload-db \
  --database ./cli-consumption.db \
  --endpoint https://usage.example.test \
  --since 2026-08-01T00:00:00Z \
  --until 2026-09-01T00:00:00Z \
  --json
```

The command validates and extracts the complete selection before opening the HTTP
client, then uploads providers in deterministic order. Identical fragments reuse a
stable idempotency key across invocations; a richer fragment receives a new key and
atomically replaces the retained copy. The collector must advertise replay receipts.
Default mode continues after an independent provider failure, while `--strict` stops
and marks remaining providers as skipped. Output never includes the database path,
time bounds, endpoint, token, idempotency key, payload, remote body, or exception text.

Independent providers continue after an upload failure, so JSON reports ordered
per-provider outcomes and an explicit `complete` flag. Remote failures use fixed codes
and omit bodies, paths, payloads, tokens, and exception text. Idempotent collectors
allow three bounded attempts for transient failures; legacy collectors receive one.

The application refuses non-loopback binding without a token. The client refuses plain
HTTP beyond loopback unless `--allow-insecure` is explicit. Production requires a
TLS-terminating reverse proxy or ingress, rate and connection limits, trusted proxy
configuration, token rotation, backups, monitoring, and access-log redaction. Uvicorn
access logs are disabled because URLs and query strings are untrusted.

The [production deployment guide](deployment.md) provides a pinned single-host example
with PostgreSQL, automatic TLS, explicit capacity bounds, secret rotation,
backup/restore, monitoring, and retention procedures.

Use `GET /health` for process liveness; it never opens the database. Use `GET /ready`
for traffic readiness; it returns `200` only when the database and expected schema are
available, otherwise a generic `503`, within a two-second application deadline. Both
routes are intentionally unauthenticated for infrastructure probes and every response
has a bounded `X-Request-ID`.

Snapshots use strict schema version 1 and a 32 MiB request-body limit. Upgrade the
server before clients whenever supported snapshot schemas change. The
[architecture guide](architecture.md) specifies retries, replay receipts, database
timeouts, readiness locking, and deployment order.

## Diagnostics and automation

`providers` reads the canonical adapter registry and can inspect local default stores:

```bash
uv run cli-consumption providers --json
```

Results are deterministic and contain only provider names, support metadata, and fixed
compatibility states: `no-data`, `detected`, `compatible`, `degraded`, or
`unsupported-schema`. They never expose paths, identifiers, record content, counts, or
parser errors.

`collect`, `snapshot create`, `snapshot ingest`, `sync`, `upload-db`, `export`, and `retention`
accept `--json`. Run
`uv run cli-consumption COMMAND --help` for complete options.
