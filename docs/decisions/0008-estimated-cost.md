# ADR 0008: Estimated cost from versioned pricing

## Status

Accepted on 2026-10-09 by the maintainer. Implementation is tracked by G1L-655 and
G1L-664.

## Context

Cost estimates were deferred until token semantics were comparable across providers.
Every comparable tool now shows an estimate, and the registry already classifies each
provider's token semantics as `additive`, `conversation-aggregate`,
`context-snapshot`, or `unavailable`. That classification is enough to decide where an
estimate is meaningful.

## Decision

CLI Consumption shows an **estimated cost at public API list prices**. It is never
presented as an invoice, a bill, or the amount actually paid.

- Only model calls from providers with `additive` token semantics receive an estimate.
  Other semantics, unknown models, and missing counters display as unavailable, never
  as zero.
- Prices come from a versioned pricing file bundled in the Python package, with its
  effective date, upstream provenance, and license. Estimation makes no network
  request. A reproducible development script refreshes the file, and CI fails when it
  becomes stale, as it does for provider qualification.
- Users may supply a local override file. It is untrusted input: strictly validated,
  size-bounded, and never echoed in errors.
- Each model call is priced per counter: uncached input, cache read, cache write
  (including distinct cache durations when the provider reports them), visible output,
  and reasoning output. Model aliases resolve through the pricing file; long-context
  tiers apply only when the file defines them.
- Estimates are computed at read time and are not persisted. Reports and exports carry
  the currency and the pricing version that produced them.
- Subscription plans (for example Claude Max, ChatGPT Pro, or Copilot) are reported as
  API-equivalent value, with an explicit label.
- Share-safe output may include estimates because they derive only from already
  shared aggregate counters; pseudonymization rules for labels are unchanged.

## Consequences

The invariant that local token events are not billing data still holds; every human and
JSON output states that the value is an estimate. No schema migration is needed. The
pricing file becomes a maintained input with its own freshness check.
