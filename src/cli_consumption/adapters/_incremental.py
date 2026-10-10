"""Generic bounded batching for adapters that opt into incremental collection.

An adapter opts in by implementing ``collect_incrementally``. It describes its store
as an ordered sequence of indivisible candidate groups (for example one transcript,
or one session plus its subagent transcripts) and supplies a function that collects
one batch of candidates with a fresh :class:`ProviderInputBudget`. Batches therefore
reset only aggregate candidate, read, and normalized-record budgets; per-file,
per-line, symlink, file-identity, and single-conversation limits stay unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TypeVar

from cli_consumption.adapters._shared import ProviderDataLimitError, ProviderInputBudget
from cli_consumption.adapters.base import CollectionBatch
from cli_consumption.models import Snapshot, SnapshotValidationError

INCREMENTAL_CANDIDATES_PER_BATCH = 1_000
MAX_INCREMENTAL_LISTING = 1_000_000
AGGREGATE_LIMIT_CODES = frozenset(
    {
        "provider_candidate_limit_exceeded",
        "provider_read_limit_exceeded",
        "provider_record_limit_exceeded",
    }
)

_Item = TypeVar("_Item")


def is_aggregate_limit(error: BaseException) -> bool:
    """Return whether a smaller batch of the same store could avoid this error."""
    if isinstance(error, ProviderDataLimitError):
        return str(error) in AGGREGATE_LIMIT_CODES
    if isinstance(error, SnapshotValidationError):
        return error.code == "snapshot_too_large"
    return False


def bounded_sorted_paths(paths: Iterable[Path]) -> list[Path]:
    """Sort one discovery listing without charging the per-batch candidate budget."""
    result: list[Path] = []
    for path in paths:
        if len(result) >= MAX_INCREMENTAL_LISTING:
            raise ProviderDataLimitError("provider_incremental_listing_limit_exceeded")
        result.append(path)
    return sorted(result)


def charge_candidates(
    items: Iterable[_Item], budget: ProviderInputBudget
) -> Iterator[_Item]:
    """Charge each batch candidate to the batch's own discovery budget."""
    for item in items:
        budget.item()
        yield item


def iter_collection_batches(
    groups: Iterable[Sequence[_Item]],
    collect: Callable[[list[_Item]], Snapshot],
    *,
    candidates_per_batch: int,
    subagent_merge: bool = False,
) -> Iterator[CollectionBatch]:
    """Pack ordered groups into bounded batches, splitting only on aggregate limits.

    A group is never divided between batches. A batch that exceeds an aggregate
    limit is split at a group boundary; a single group that still exceeds a limit
    fails with the original error.
    """
    pending: list[list[_Item]] = []
    size = 0
    for group in groups:
        items = list(group)
        if not items:
            continue
        if pending and size + len(items) > candidates_per_batch:
            yield from _collect_split(pending, collect, subagent_merge)
            pending, size = [], 0
        pending.append(items)
        size += len(items)
    if pending:
        yield from _collect_split(pending, collect, subagent_merge)


def iter_source_batches(
    provider: str,
    sources: list[tuple[str, Path]],
    discover: Callable[[Path], Iterable[Path]],
    collect: Callable[[list[tuple[str, Path]]], Snapshot],
    *,
    candidates_per_batch: int,
) -> Iterator[CollectionBatch]:
    """Batch single-file conversations source by source, in discovery order."""
    for machine, home in sources:
        yielded = False
        for batch in iter_collection_batches(
            ([(machine, path)] for path in discover(home)),
            collect,
            candidates_per_batch=candidates_per_batch,
        ):
            yielded = True
            yield batch
        if not yielded:
            yield CollectionBatch(Snapshot(provider=provider), frozenset())


def _collect_split(
    groups: list[list[_Item]],
    collect: Callable[[list[_Item]], Snapshot],
    subagent_merge: bool,
) -> Iterator[CollectionBatch]:
    snapshot: Snapshot | None = None
    try:
        snapshot = collect([item for group in groups for item in group])
    except (ProviderDataLimitError, SnapshotValidationError) as error:
        if len(groups) == 1 or not is_aggregate_limit(error):
            raise
    if snapshot is not None:
        yield CollectionBatch(snapshot, frozenset(), subagent_merge=subagent_merge)
        return
    midpoint = len(groups) // 2
    yield from _collect_split(groups[:midpoint], collect, subagent_merge)
    yield from _collect_split(groups[midpoint:], collect, subagent_merge)
