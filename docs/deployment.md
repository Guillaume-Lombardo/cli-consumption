# Production collector deployment

The reference stack in [`deploy/production`](../deploy/production/) runs one collector
behind a TLS-terminating Caddy proxy with PostgreSQL. It is a reproducible starting
point for a single host, not a managed service or high-availability design.

## Prepare the host

Install a maintained Docker Engine with the Compose plugin, point public DNS at the
host, and allow inbound TCP 80/443 and UDP 443. Keep PostgreSQL and port 8765 private;
the Compose network publishes only Caddy.

Create the local configuration without committing it:

```bash
cd deploy/production
cp .env.example .env
chmod 600 .env
```

Generate the four secret values through the organization's secret manager. The
PostgreSQL password must use only URL-safe unreserved characters because Compose
places it in a SQLAlchemy URL. Use independent, high-entropy ingestion, read, and
export tokens, then fill the public domain and ACME contact address. Never pass the
secrets as command-line arguments or commit the resolved Compose configuration.

This minimal Compose example injects all four secrets into container environment variables;
Docker API and host-root access can inspect them. Restrict Docker daemon access as
equivalent to secret access. Use the platform's native secret-file integration for a
stronger boundary; the reference stack deliberately does not claim that integration.

Validate and start the pinned stack:

```bash
docker compose config --quiet
docker compose build --pull collector
docker compose up -d
docker compose ps
curl --fail --silent --show-error https://usage.example.test/ready
```

Replace the example URL with the configured domain. Caddy obtains and renews the
certificate automatically. The application upgrades the database before becoming
ready; deploy the collector before upgrading sync clients.

## Boundaries and capacity

The container build receives only the package source and lock files allowed by the
root `.dockerignore`; local databases, reports, environment files, Git history, and
provider files cannot enter the build context. Images are pinned by multi-platform
digest and Python dependencies are installed from `uv.lock` with `--frozen`.

One collector uses SQLAlchemy's default pool: at most 5 persistent connections plus 10
overflow connections. Readiness has one separately bounded, unpooled probe. Caddy
allows at most 16 upstream connections and PostgreSQL is capped at 30, leaving headroom
for maintenance. Do not scale the collector replicas without recalculating all three
limits and testing the database workload. The example also bounds snapshot bodies at
32 MiB, reporting bodies at 64 KiB in the application, report/export concurrency,
process counts, temporary export storage, header size, and proxy timeouts. Add an
infrastructure rate limiter and network policy appropriate to the trust boundary; the
application has no rate limiter by design.

Pagination sessions are process-local and expire on collector restart. Keep this
reference deployment at one collector replica; a multi-replica topology needs request
affinity until a shared session backend exists. Clients must restart at page one after
the fixed `pagination_expired` response.

The proxy discards access logs so tokens, request targets, headers, and query strings
cannot leak there. Application error logs contain only fixed event fields and bounded
request IDs. PostgreSQL disables connection logs and error parameter values. If the
platform requires access logs, configure an allowlist of method, static route template,
status, duration, and generated request ID—never log headers, bodies, full URLs, query
strings, database URLs, or exception messages.

## Health and monitoring

`GET /health` is liveness only and never opens the database. Route traffic only while
`GET /ready` returns `200`; it checks the expected schema and PostgreSQL within a
two-second application deadline. Configure the external probe timeout slightly above
two seconds and alert on repeated readiness failure, container restarts, PostgreSQL
capacity, disk space, certificate renewal, and backup age. The endpoints are
unauthenticated and reveal availability, so restrict them by network policy when the
deployment is not meant to be public.

## Rotate scoped API tokens

The service accepts separate ingestion, read, and export bearer tokens. The ingestion
token has only `ingest`, the read token only `read`, and the export token both `read`
and `export`. Overlapping values and zero-downtime rotation are not supported by this
single-replica example. Schedule a brief pause for the affected operation, replace the
corresponding protected `.env` value, recreate the collector, and then update clients
or the server-side BFF from the same secret manager:

```bash
docker compose up -d --force-recreate collector
curl --fail --silent --show-error https://usage.example.test/ready
```

Revoke the old value after clients use the new one. A lost or exposed database password
requires a coordinated PostgreSQL role-password change and collector recreation; do
not merely edit `.env`, because that would strand the application.

## Run the persistent dashboard

The persistent dashboard is an optional Node service in `apps/web/`. It must reach the
collector over a private or TLS-protected route, but it must never receive the database
URL or ingestion/export credentials. Install and build it from the locked workspace:

```bash
npm ci
NEXT_TELEMETRY_DISABLED=1 npm run build:web
```

Inject these server-only values through the deployment platform's secret manager:

- `CLI_CONSUMPTION_API_URL`: the collector origin, without credentials or a path;
- `CLI_CONSUMPTION_DASHBOARD_ORIGIN`: the exact public HTTPS dashboard origin;
- `CLI_CONSUMPTION_DASHBOARD_PASSWORD`: an independent operator password of at least
  12 characters;
- `CLI_CONSUMPTION_READ_TOKEN`: the collector's read-scoped token, never the ingestion
  or export token;
- `CLI_CONSUMPTION_SESSION_SECRET`: an independent random value of at least 32 bytes.

Start the built service without placing secrets on the command line:

```bash
NEXT_TELEMETRY_DISABLED=1 npm run start --workspace @cli-consumption/web -- \
  --hostname 127.0.0.1 --port 3000
```

Terminate TLS at a reverse proxy and forward the original `Host` and scheme. Keep the
Node listener private, reject request bodies above 64 KiB, rate-limit login attempts,
and do not log headers, cookies, POST bodies, full URLs, or query strings. Set
`CLI_CONSUMPTION_DASHBOARD_ORIGIN` to exactly the browser-visible origin so the BFF can
reject cross-origin mutations. The session lasts eight hours and is held in a signed,
HTTP-only, same-site cookie; rotate the password, read token, and session secret through
normal secret-manager replacement and service restart procedures.

The browser initially requests only the latest 30 days. Project, machine, provider,
model, and other operational labels travel in POST bodies rather than URL query
parameters. The dashboard intentionally exposes those private labels and usage metrics
to authenticated users; it is not a public or share-safe surface. Keep the standalone
`export --share-safe` workflow for minimized files intended for controlled sharing.

## Back up and restore

Backups contain private operational metadata. Write them to encrypted, access-controlled
storage outside the repository and define retention plus restore testing. From the
deployment directory, a custom-format logical backup can be streamed without embedding
credentials in the command:

```bash
umask 077
mkdir -p /secure/backups/cli-consumption
docker compose exec -T postgres pg_dump \
  --username cli_consumption --dbname cli_consumption --format custom \
  > /secure/backups/cli-consumption/usage.dump
```

Test restoration on an isolated host. For a planned restore, stop writers, preserve the
current database, restore into a newly created empty database, then start the same
application version and require readiness before enabling clients:

```bash
docker compose stop collector
docker compose exec -T postgres dropdb --username cli_consumption cli_consumption
docker compose exec -T postgres createdb --username cli_consumption cli_consumption
docker compose exec -T postgres pg_restore --exit-on-error \
  --username cli_consumption --dbname cli_consumption \
  < /secure/backups/cli-consumption/usage.dump
docker compose up -d collector
```

The destructive restore commands are examples only: confirm the target and a tested
backup before running them. Application downgrade may require restoring the matching
pre-upgrade backup even when schema downgrade exists.

## Apply retention and upgrades

Choose a metadata retention window with privacy and operational owners. Preview first,
record the counts, and apply only after checking the target database:

```bash
docker compose run --rm collector retention --keep-days 90
docker compose run --rm collector retention --keep-days 90 --apply
```

This removes normalized metadata, not backups, proxy/database logs, provider files, or
previous exports. Those need independent lifecycle policies and secure disposal.

For upgrades, back up first, update the pinned application source and image digests,
review release notes, build, stop or drain writers, and recreate the collector. Never
run mixed application versions through a migration. Roll back the image only when its
schema is compatible; otherwise restore the pre-upgrade backup.

## Operator responsibilities and limits

The operator owns host patching, DNS, firewall and network policy, certificate alerts,
secret storage and rotation, rate limiting, monitoring, backup encryption and restore
tests, storage capacity, retention, upgrades, and incident response. The example does
not provide high availability, horizontal scaling, off-host backups, a web application
firewall, denial-of-service protection, or a secrets manager integration. Normalized
metadata and backups remain private even though prompts and responses are excluded.
