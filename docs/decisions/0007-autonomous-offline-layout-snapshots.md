# ADR 0007: Autonomous offline layout snapshots

- Status: Accepted
- Scope: authenticated web export, FastAPI export, and standalone dashboard runtime

## Context

The persistent dashboard and standalone renderer already share the versioned widget
registry, calculations, and grid. Web export nevertheless reloaded whichever layout
was stored when FastAPI handled the request and let the opening machine choose the
offline theme. A concurrent save could therefore make a download differ from the
composition the operator reviewed.

## Decision

The authenticated web path posts a strict version 1 envelope containing the last
successfully rendered `DashboardQuery v1`, a detached copy of the currently visible
validated `DashboardLayout v1`, and `light` or `dark`. The layout may be the in-memory
edit draft when edit mode is visible; export never persists it. The envelope contains
no revision, ETag, owner key, credential, endpoint, cookie, or widget state.

Before any request, an accessible dialog summarizes only profile, window, theme,
widget count, and selected-filter count. It omits filter labels and layout JSON. The
snapshot is frozen when the dialog opens. Cancel or Escape performs no request and
restores focus. The dialog states that offline filters only refine the copied subset
and that retired widgets are removed deterministically; an otherwise incompatible
stored document has already resolved to the safe default.

FastAPI strictly validates the envelope and the HTML sink revalidates a detached
layout dump before encoding. It uses the supplied query, composition, and theme rather
than reading layout persistence. The completed HTML embeds those values and all
minimized reporting rows, so later database or preference changes cannot affect it.
The shared offline runtime applies the same registry, logical order, CSS-grid geometry,
and calculations, starts with the embedded theme, and retains local filter and theme
interactions without storage or network access.

For compatibility, the collector continues to accept the previous exact
`DashboardQuery v1` body. That unambiguous legacy shape loads the current resolved
layout and uses the opening system theme. The CLI keeps the same `generate_dashboard`
defaults and adds no server, credential, or theme requirement. The web BFF always
sends the envelope and still uses only its export credential.

The envelope has a dedicated 128 KiB request ceiling because it combines independently
bounded contracts; other reporting bodies remain limited to 64 KiB. Existing selected
row, scalar-memory, 128 MiB HTML, 60-second, and one-concurrent-export limits remain.
The slot is acquired before allocating a mode-0600 temporary. Generation completes
before any response, and the file is removed after failure, streaming, or cancellation.

Standalone HTML declares a deny-by-default CSP: network connections, objects, base
URLs, and form submissions are forbidden; only inline script/style and embedded data
fonts/images are allowed. It includes no network primitive or remote URL.

## Consequences

The file is a reproducible snapshot of the reviewed UI state and opens through
`file://` at desktop, tablet, and mobile widths in either theme. Detailed and
share-safe disclosure semantics do not change. No normalized field, storage table,
migration, provider payload, log field, or billing claim is introduced.
