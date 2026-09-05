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

The `dashboard_layouts` table stores an internal fixed owner key and canonical JSON.
The key is never accepted from or returned to a client. It means “the authenticated
operator of this deployment”, not a person. There is exactly one application-managed
row; last committed write wins atomically on SQLite and PostgreSQL. Multi-user layouts
are out of scope until authentication supplies a stable subject.

Revision `0006` creates the table without seeding it. Upgrade therefore maps the old
hard-coded dashboard to the current default on first read. Downgrade to `0005` drops
only this presentation preference; normalized usage data is unchanged. The application
server must be upgraded before the BFF. Old servers reject the new route, old BFFs
ignore the table, and simultaneous old/new writers must be avoided during migration.
Snapshot extraction requires the exact new database revision but never transfers the
layout table. CSV, reporting datasets, normalized snapshots, retention, and provider
ingestion also exclude it.

Reading a layout requires `read`. Saving or resetting requires the separate `layout`
scope and `CLI_CONSUMPTION_LAYOUT_TOKEN`; neither `read` nor `export` gains mutation
authority. The BFF retains that credential server-side and requires its authenticated,
same-origin session for mutations. Capabilities advertise layout version 1 and the
fixed mutation scope.

## Privacy and dependencies

The allowed fields describe only component composition. Structural widget identifiers
cannot encode user input. Prompts, responses, tool
arguments, paths, credentials, provider payloads, operational labels, queries, and
dataset rows are forbidden by the closed schema. Validation errors use fixed codes and
do not echo input. The stored layout is private operational preference data, but does
not widen the conversation metadata boundary.

No grid or drag-and-drop dependency is added in this increment. The contract does not
require one, existing CSS grid is sufficient to render it, and avoiding a dependency
keeps the offline bundle smaller and network-free. Before adding one, a separate
decision must compare compressed bundle weight, keyboard and screen-reader behavior,
touch support, maintenance activity, deterministic offline builds, and compatibility
with both React runtimes.
