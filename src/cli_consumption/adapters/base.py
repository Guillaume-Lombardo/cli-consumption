from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from cli_consumption.models import Snapshot


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    """One snapshot plus an optional subagent-scope authority override."""

    snapshot: Snapshot
    authoritative_subagent_scopes: frozenset[tuple[str, str]] | None = None


class UnsupportedProviderFormat(ValueError):
    """Raised when a detected provider store has an incompatible schema."""


class Adapter(Protocol):
    """Contract implemented by every supported AI CLI."""

    name: str

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Snapshot: ...


@runtime_checkable
class IncrementalAdapter(Adapter, Protocol):
    """Optional adapter contract for bounded, independently ingestible batches."""

    def collect_incrementally(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Iterator[CollectionBatch]: ...
