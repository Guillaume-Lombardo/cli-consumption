# Security policy

## Supported versions

Security fixes are made on the latest released version and on `main`. Older releases
are not maintained as separate security branches. Upgrade to the latest release before
reporting a problem that may already have been fixed.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Email
`lombardo.guillaume@gmail.com` with the subject `CLI Consumption security report` and
include:

- the affected version and operating system;
- the provider, command, or API surface involved;
- reproduction steps using synthetic data;
- the expected and observed impact;
- any suggested mitigation.

Do not send real prompts, responses, credentials, provider databases, or other private
conversation data. Use a synthetic canary when demonstrating a disclosure.

The maintainer will acknowledge the report, assess whether it crosses the documented
privacy or security boundary, and coordinate a fix and disclosure when appropriate.

## Security boundary

Provider files and incoming snapshots are untrusted input. Relevant reports include
content or credential disclosure, unsafe filesystem traversal, denial of service,
authentication bypass, cross-machine data corruption, and generated dashboards that
perform network requests or execute provider-controlled content.

Operational exposure caused solely by publishing a normalized database or detailed CSV
is outside the vulnerability boundary: those artifacts intentionally contain private
operational metadata. The precise allowed and prohibited fields are documented in the
[privacy boundary](https://github.com/Guillaume-Lombardo/cli-consumption/blob/main/docs/privacy.md).

## Automated security checks

The `Security` workflow runs for relevant pull requests, every Tuesday, and on manual
request. It runs three blocking checks from the locked development environment:

- Ruff's `S` rules inspect Python source for common security mistakes. Narrow file-level
  exceptions cover test assertions, controlled schema identifiers, and non-secret
  metadata names; new exceptions require a security review and an explanatory change.
- `pip-audit` checks the installed, locked environment against the Python Packaging
  Advisory Database. Only package names and versions are queried; repository files and
  provider data are not submitted.
- `zizmor` inspects local workflow definitions in explicit offline mode. It cannot send
  workflow contents, secrets, fixtures, source code, or provider data to an analysis
  service.

The workflow has read-only repository access, checkout credentials are discarded, and
no secrets are configured. Production provider files are never present in CI; test
fixtures are synthetic and contain privacy canaries that must not cross export or log
boundaries.

## Dependency and action policy

Python security tools belong to the `dev` dependency group and are resolved in
`uv.lock`. All third-party GitHub Actions references use a full 40-character commit SHA
with a nearby human-readable release comment. Updates must verify the upstream release,
change both the SHA and comment, and pass the offline workflow audit. Checkout discards
credentials except in the release tag job, whose sole purpose and `contents: write`
permission are the authenticated tag push. Other workflows declare read-only or empty
permissions; PyPI publishing receives only `id-token: write`.

## Alert triage

Treat any scheduled or pull-request security failure as a release blocker until it is
classified:

1. Reproduce the exact locked command locally without real provider files or secrets.
2. Determine whether the finding is exploitable, a dependency advisory, an upstream
   action change, or a documented false positive. Never paste private provider content
   into an issue, log, or external scanner.
3. For a plausible vulnerability, use the private reporting channel above, prepare a
   minimal synthetic reproducer, and prioritize containment before public disclosure.
4. For a vulnerable dependency or action, update to a reviewed fixed release and rerun
   every quality gate. If no fix exists, document the affected surface and mitigation;
   suppressions must cite the advisory and have a removal condition.
5. Record non-sensitive remediation work in Linear. Close the alert only after the fix
   is merged or a time-bounded, reviewed exception is documented.
