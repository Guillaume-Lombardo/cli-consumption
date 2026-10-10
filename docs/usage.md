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

When a provider store exceeds the aggregate candidate, provider-read, or
normalized-record limit, `collect` switches that provider automatically to bounded,
restart-safe batches written to the same database. Other providers in the same
command keep their normal single snapshot. Forbid the switch to restore the
all-or-nothing `provider_limit_exceeded` failure, or force batches from the start:

```bash
uv run cli-consumption collect --provider claude --no-incremental \
  --source desktop=/data/claude/desktop --database usage.sqlite
uv run cli-consumption collect --provider codex --incremental \
  --source desktop=/data/codex/desktop --database usage.sqlite
```

Batching is available for Claude Code, Codex, Amp, Continue CLI, Gemini CLI, Pi, and
Qwen Code; the [support ledger](provider-support.md#incremental-collection) lists the
reason each other provider keeps one snapshot. The overflowing first attempt is
discarded before any write, so the switch reads part of the store twice. Each batch
is independently committed, so an interrupted non-strict run may leave a valid
partial import. The failure output reports the number of committed batches, and
rerunning the same command safely converges without duplicate conversations or child
records. `--strict` instead validates every metadata-only batch in a private
temporary staging directory before opening the destination database; any malformed
provider record leaves the database untouched. Staging is removed on success or
failure.

Batching resets only aggregate candidate, read, and normalized-record budgets. An
individually oversized JSONL file or line, an unsafe symlink or file type, an
oversized single conversation, or a Claude Code session whose transcripts across all
sources and project directories, including its subagent transcripts, exceed one
batch budget still fail with the generic `provider_limit_exceeded` code. A separate 10,000-batch command ceiling bounds total
work, one directory listing is capped at 1,000,000 entries, and strict metadata
staging is capped at 4 GiB.

Claude Code subagent relationships converge across batches: each relationship
follows the stored copy of its child conversation, and batches never delete a
relationship. Normal collection remains responsible for removing relationships whose
transcripts were deleted. Batched Codex collection deliberately leaves the Codex
SQLite subagent graph unchanged because safely refreshing that authoritative scope
requires whole-collection freshness.

Batched JSON summaries contain `"incremental": true`, an `incremental_trigger` of
`requested` (`--incremental`) or `automatic` (aggregate limit exceeded), and one entry
per provider with a `batched` flag, the batch count, and aggregate counters. They omit
ingestion-run identifiers and paths, so identical inputs produce identical output.
The adapter-level counter is called `batch_duplicates` because duplicate copies
separated by a batch boundary are resolved deterministically by SQL replacement and
therefore appear in the actual `written` or `skipped` totals. The database result is
independent of the boundary even though physical ingestion-run counts necessarily
are not.

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

## Report usage in the terminal

`report` reads an existing database and prints one table. It never collects provider
data; run `collect` or `quick` first.

```bash
uv run cli-consumption report daily
uv run cli-consumption report weekly --timezone Europe/Paris
uv run cli-consumption report monthly --by model --since 2026-08-01 --until 2026-09-30
uv run cli-consumption report session --provider codex --project api --json
```

The view is `daily` (the default), `weekly` (ISO weeks starting on Monday), `monthly`,
or `session` (one row per conversation, numbered in start order; provider IDs and
paths are never shown). `--provider` accepts canonical names and documented aliases;
`--provider`, `--project`, `--machine`, and `--model` can be repeated.
`--by model|provider|project|machine` adds sub-rows under every period or session.

`--timezone` takes an IANA name and defaults to `UTC`. It sets period boundaries and
the meaning of plain `--since` and `--until` dates: `--since` is inclusive, and a plain
`--until` date is included in the report. Timestamps must carry an offset and at most
millisecond precision, the precision of dashboard calculations; finer fractional
digits are rejected as `invalid_window`, since the dashboard would truncate them.
Stored timestamps keep their microseconds, and with millisecond bounds both
selections are identical. The window selects conversations exactly like `export`,
then counts only the activity inside it.

The columns follow the normalized token model. Input includes cache-read and
cache-write tokens, output includes reasoning tokens, and the cache rate is cache
reads divided by input. Conversations are counted once, in the period where they
start, or in the first period of the window when they started earlier. Turns are
counted where they start and calls at their timestamp. A conversation or turn without
any timestamp appears in an `undated` row. With `--by model`, token and call sub-rows
add up to their period, while turns and conversations that used several models appear
under each of them.

Token selection is identical to the dashboard for the same window and filters, and an
automated cross-check runs the dashboard calculations against the report:

- Calls from `additive` providers count when they have a timestamp inside the window
  and belong to no turn or to a completed or aborted turn that starts inside it.
- Counters from `conversation-aggregate` and `context-snapshot` providers have no
  per-call time. They count when their conversation has a start or end timestamp; a
  conversation without either is excluded, as in the dashboard, unless the selection
  contains no timestamp at all (including ingestion runs). Period views attribute
  these counters to their conversation's period and flag the row as `agg` or `snap`.
- Without `--since` and `--until`, a conversation without any timestamp is counted
  only when it has a turn, a counted call, or a tool call, again unless the selection
  contains no timestamp at all. A provider whose token semantics are
`unavailable` contributes conversation, turn, and call counts, but its token cells
show `n/a`, never `0`; a row mixing it with measured providers is flagged `partial`.
Token counters are local usage metadata, not billing data, and the report contains no
cost estimate.

The table fits the terminal width. When it is too narrow, numbers are abbreviated and
less essential columns are hidden and listed below the table. Colors are used only on
an interactive terminal and never when `NO_COLOR` is set, `TERM=dumb`, or the output
is piped. Control characters in stored labels are replaced before printing.

`--share-safe` replaces project, machine, and model labels with the aliases a
share-safe dashboard uses for the same selection. Like that dashboard, it evaluates
the window and every timestamp on UTC days: activity on the first and last day of a
window counts even outside its exact hours, periods and session dates use UTC
calendar days whatever `--timezone` says (which then only interprets plain dates),
and the JSON window and `timezone` show the rounded UTC values. Provider names and
aggregate activity remain visible.

`--json` prints one deterministic line with sorted keys. Its contract is versioned;
this example is formatted for reading:

```json
{
  "schema": "cli-consumption/usage-report",
  "schema_version": 1,
  "view": "daily",
  "timezone": "UTC",
  "window": {"since": null, "until": null},
  "filters": {"providers": [], "projects": [], "machines": [], "models": []},
  "breakdown": null,
  "share_safe": false,
  "notice": "Token counters are local usage metadata, not billing data.",
  "rows": [
    {
      "period": "2026-08-03",
      "session": null,
      "conversations": 1,
      "turns": 1,
      "calls": 1,
      "tokens": {
        "input": 5500, "cache_read": 4000, "cache_write": 500,
        "uncached_input": 1000, "output": 500, "reasoning": 200,
        "visible_output": 300, "unattributed": 0, "total": 6000
      },
      "cache_rate": 0.727273,
      "token_semantics": ["additive"],
      "flags": [],
      "breakdown": []
    }
  ],
  "totals": {
    "conversations": 1, "turns": 1, "calls": 1, "tokens": {"total": 6000},
    "cache_rate": 0.727273, "token_semantics": ["additive"], "flags": []
  }
}
```

The `totals.tokens` object has the same nine counters as a row; it is shortened here.
`period` is `YYYY-MM-DD` for days, the Monday date for weeks, `YYYY-MM` for months,
and `null` for the undated row and for sessions. Session rows carry `session` with
`number`, `started_at`, `provider`, `project`, `machine`, and `models`. `tokens` and
`cache_rate` are `null` when unavailable. `flags` contains `conversation-aggregate`,
`context-snapshot`, `tokens-unavailable`, or `partial-tokens`. Breakdown items repeat
the metrics with a `label`. Additive changes keep version 1; any removal or change of
meaning increments `schema_version`.

Errors exit with status 2 and fixed codes, as `{"error":{"code":...}}` with `--json`:
`database_not_found`, `database_unavailable`, `database_driver_missing`,
`invalid_timezone`, `invalid_window`, `unknown_provider`, or `report_limit_exceeded`.
Invalid database URLs, filesystem errors, failed migrations, and failed ingestion
during `quick` all become `database_unavailable`. Errors never include paths, URLs,
SQL statements, parameters, or rejected values. Bounds that leave the supported
calendar after timezone conversion or day rounding are `invalid_window`.

### First run with `quick`

```bash
uv tool run cli-consumption quick
```

`quick` detects providers like `collect --provider all` without `--source`, collects
each of them into the default `cli-consumption.sqlite` in the current directory (or
`--database`), then prints `report daily` for all recorded activity. It is a dedicated
command so that running `cli-consumption` without arguments keeps printing help.

Collection follows the automatic mode of `collect`. An incremental-capable provider
that exceeds an aggregate candidate, read, or normalized-record limit switches to
bounded, restart-safe batches; only that provider is batched, and per-file, per-line,
symlink, and single-conversation limits still apply. One `quick` run is capped at
10,000 batches in total. Collection is idempotent, so rerunning `quick` refreshes the
same database and resumes after an interrupted batch run.

Unlike `collect`, a provider that fails does not stop the others. Its fixed collection
message goes to standard error, with the number of batches already committed when it
was batched; the remaining providers are still collected, the report is printed, and
the command exits with status 2. Collection messages go to standard error and the
report to standard output. `--json` prints
`{"collection":{"incremental":...,"ingestions":[...],"failures":[...]},"report":{...}}`.
Each ingestion carries the same counters as `collect --json` in batch mode:
`provider`, `batched`, `batches`, `received`, `written`, `skipped`, `malformed`, and
`batch_duplicates`. `incremental` is `true` when any provider was batched, and each
failure has only `provider` and a fixed `code`.

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
export CLI_CONSUMPTION_READ_TOKEN="$(your-secret-provider)"
export CLI_CONSUMPTION_EXPORT_TOKEN="$(your-secret-provider)"
export CLI_CONSUMPTION_LAYOUT_TOKEN="$(your-secret-provider)"
uv run cli-consumption serve \
  --database postgresql+psycopg://usage@localhost/cli_consumption \
  --host 0.0.0.0
```

For a local, single-command dashboard, install Node.js 20.9 or newer and run:

```bash
uv tool run cli-consumption serve --front
```

The wheel contains the production Next.js application, so this path needs neither a
repository checkout nor npm. The command binds both services to loopback, prompts for
an independent dashboard password of at least 12 characters, and creates transient
scoped API and session credentials when their normal environment variables are not
set. Set `CLI_CONSUMPTION_DASHBOARD_PASSWORD` for non-interactive startup. Use
`--front-port` to change the dashboard port; `--front-host` deliberately accepts only
a loopback address. Production deployments should continue to run the two services
separately as described in the deployment guide.

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

The ingestion token grants only `ingest`. The read token grants only `read`; the export
token grants both `read` and `export`. Reporting filters are accepted only in strict,
bounded POST bodies. For example, a server-side client can request a dataset with:

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${CLI_CONSUMPTION_READ_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{"version":1,"window":{"since":"2026-08-01T00:00:00Z","until":"2026-09-01T00:00:00Z"},"filters":{"providers":["codex"],"machines":[],"projects":[],"models":[]},"profile":"detailed"}' \
  https://usage.example.test/api/v1/reporting/dashboard
```

The related routes are `/api/v1/reporting/filters`,
`/api/v1/reporting/conversations`, `/api/v1/reporting/conversation`, and
`/api/v1/reporting/export`.
Conversation cursors and references are opaque and expire; restart pagination after a
fixed `pagination_expired` response. They are process-local and therefore also expire
when the service restarts. All reporting responses disable caching. Requests,
responses, and errors never include SQL/provider identifiers, hashes, source labels,
receipt keys, paths, tokens, or exception text.

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

`collect`, `snapshot create`, `snapshot ingest`, `sync`, `upload-db`, `export`,
`retention`, `report`, and `quick` accept `--json`. Run
`uv run cli-consumption COMMAND --help` for complete options.
