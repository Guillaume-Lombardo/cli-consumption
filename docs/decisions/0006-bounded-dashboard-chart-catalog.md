# ADR 0006: Bounded dashboard chart catalog

## Status

Accepted.

## Decision

Online and offline dashboards derive charts through the same pure `chartCatalog`
calculation and render activity through the same React component. No charting or
drag-and-drop dependency is added. The catalog is bounded to 364 cells: 52 complete UTC
Sunday-to-Saturday weeks ending in the week containing the selected end date.

Days inside the selected/export window are observed and may be zero. Days outside it are
missing and use a distinct dashed cell. Tooltips and the disclosure table give exact UTC
dates, values, and units. Arrow keys move by day vertically and week horizontally. The
selector contains only attributable measurements.

Daily token charts accept additive timestamped model calls only. Conversation aggregate
and context snapshot counters remain valid for total KPIs, but are not assigned to an
invented day. Conversations, turns, and duration are attributed to their UTC start day.
Streaks are independent of the selected metric: they count observed days containing a
conversation or turn and end at the last observed day rather than trailing calendar
alignment.

Token composition uses mutually exclusive normalized counters: uncached plus cache-write
input, cached input, visible output, and reasoning output. Counts are provider-reported
usage, never price or billing data. Rankings are deterministically sorted and bounded to
ten rows. Provider and model time-series categories are ranked globally across the
visible series, limited to five labelled buckets, and folded into one typed remainder
bucket. Each day carries the same stable bucket IDs; display labels never serve as IDs,
so legitimate labels such as `Other` or `Overall` cannot collide with UI sentinels.

## Consequences

No API or persistence field is added. Detailed operational labels and share-safe
pseudonyms retain their existing privacy semantics. A daily series may be unavailable
when a global total exists; the UI states this rather than displaying a false zero.
