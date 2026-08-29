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
