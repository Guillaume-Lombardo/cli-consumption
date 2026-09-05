# ADR 0005: Versioned dashboard layouts and widget registry

- Status: Accepted
- Scope: shared TypeScript contracts, offline renderer, Next.js BFF, reporting API,
  SQLite, and PostgreSQL

## Context

The online and self-contained dashboards need one provider-neutral description of
which approved widgets form a dashboard. Layout preferences must not become another
report query, copy reporting data, or accept arbitrary component names and props.
The existing dashboard authentication is deliberately mono-operator: its signed
session has no stable subject. A client-provided user identifier or token hash would
therefore invent an unsafe identity boundary.

## Decision

`DashboardLayout v1` is a presentation-only document with exactly `version`,
`columns`, and `widgets`. Each widget has a bounded stable identifier, a type from the
explicit registry, integer grid position and size, and a type-specific configuration.
Version 1 widget configurations are empty objects. Unknown keys, duplicate identifiers,
unknown types, non-structural identifiers, non-integers, positions outside the
twelve-column grid, and sizes outside the registry's minimum/maximum bounds are rejected.
An identifier is exactly its registered type or `type-N`, where canonical decimal `N`
is between 1 and 32. This permits bounded multiple instances without creating a
free-form string channel. Documents contain at most 32 widgets, use at most 64 grid
rows, reject overlapping rectangles, and remain under the reporting API's 64 KiB
bound. Version 1 does not normalize collisions because silently moving a widget would
make the persisted intent ambiguous.

Python model instances are working values rather than security capabilities. The SQL
and offline-HTML sinks reconstruct a detached strict model from its serialized fields
immediately before encoding. Post-validation mutation therefore produces the same fixed
rejection as malformed input and cannot alter persistence or an export in progress.

The layout is separate from `DashboardQuery v1` (time window and operational filters)
and `DashboardDataset v1` (minimized reporting rows). The shared contracts package is
the canonical TypeScript type, registry, validator, default, and resolver used by both
browser runtimes. A shared grid component renders every registered widget according to
the resolved document: omitted widgets are hidden, coordinate order is deterministic,
and positions and sizes use the same relative twelve-column CSS grid online and
offline. The default lists the legacy dashboard sections in their established logical
order, so an absent row or a reset remains useful and deterministic.

A widget removed by a later application release is dropped while reading an otherwise
valid stored document. Remaining widgets retain their document order and coordinates.
If nothing valid remains, or any other corruption is found, resolution returns the
complete current default. New writes are strict and never accept unknown widgets.
Future layout versions require an explicit migrator before they can be written.
Layouts written by an earlier prerelease implementation with free-form identifiers
resolve to the complete safe default instead of carrying those values into an export.

The `dashboard_layouts` table stores an internal fixed owner key, canonical JSON, and
a non-null signed-bigint revision in the closed range 1 through `2^63 - 1`.
The key is never accepted from or returned to a client. It means “the authenticated
operator of this deployment”, not a person. There is exactly one application-managed
row. An absent row has logical revision zero; an upgraded legacy row starts at one.
Every successful save or reset increments the revision, and reset persists the
canonical default rather than deleting the row. This prevents ABA ambiguity. The
maximum revision cannot be incremented and fails closed as a conflict. Multi-user
layouts are out of scope until authentication supplies a stable subject.

Revision `0006` creates the table without seeding it. Revision `0007` adds the bounded
revision, assigning one to an existing row; downgrade to `0006` removes only the
concurrency field and preserves the JSON. A further downgrade to `0005` drops only this
presentation preference. Normalized usage data is unchanged. The application server
must be upgraded before the BFF. Writers without `If-Match` fail with `428
layout_revision_required`, so they cannot silently overwrite a newer draft. Mixed
application versions remain unsupported while the schema migration runs.
Snapshot extraction requires the exact new database revision but never transfers the
layout table. CSV, reporting datasets, normalized snapshots, retention, and provider
ingestion also exclude it.

Reading a layout requires `read`. Saving or resetting requires the separate `layout`
scope and `CLI_CONSUMPTION_LAYOUT_TOKEN`; neither `read` nor `export` gains mutation
authority. The BFF retains that credential server-side and requires its authenticated,
same-origin session for mutations. Capabilities advertise layout version 1 and the
fixed mutation scope.

GET returns the layout body and a quoted opaque `ETag` derived only from its internal
revision. It contains no layout JSON, owner key, label, credential, or stable user
identity, and clients must not interpret it. PUT and DELETE require that exact value in
`If-Match`. The collector performs one conditional INSERT or UPDATE, never a
read-then-write sequence. A stale create, update, or reset returns only `412
layout_conflict`; malformed validators use a fixed error. The BFF forwards and
re-emits only syntactically bounded entity tags.

The browser starts in view mode with no per-widget controls. Explicit edit mode owns a
detached draft, baseline, and at most twenty undo states in React memory outside the
metric renderer, so reporting-filter refreshes cannot erase an edit. Nothing is stored
in local storage or a URL. Save is explicit. Transport failure or conflict preserves
the draft and offers explicit reload/discard or reload-the-latest-revision-and-retry.
Reset changes only the draft and is undoable until save.

The palette is the existing closed registry and describes each widget's purpose,
metrics, and size range. Addition uses deterministic row-major first-fit placement.
Movement and resizing reuse strict v1 collision and grid validation. Pointer capture is
available on non-mobile layouts; arrow keys move, Shift+arrow resizes, and visible
buttons provide the same operations. Mobile disables drag while retaining the complete
button controls. Status changes use a polite live region and invalid moves leave the
prior valid draft unchanged.

## Privacy and dependencies

The allowed fields describe only component composition. Structural widget identifiers
cannot encode user input. Prompts, responses, tool
arguments, paths, credentials, provider payloads, operational labels, queries, and
dataset rows are forbidden by the closed schema. Validation errors use fixed codes and
do not echo input. The stored layout is private operational preference data, but does
not widen the conversation metadata boundary.

No grid or drag-and-drop dependency is added. The contract does not
require one, existing CSS grid is sufficient to render it, and avoiding a dependency
keeps the offline bundle smaller and network-free. Before adding one, a separate
decision must compare compressed bundle weight, keyboard and screen-reader behavior,
touch support, maintenance activity, deterministic offline builds, and compatibility
with both React runtimes.
