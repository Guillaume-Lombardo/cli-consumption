from __future__ import annotations

from pathlib import Path
from typing import Protocol

from cli_consumption.models import Snapshot


class Adapter(Protocol):
    """Contract implemented by every supported AI CLI."""

    name: str

    def collect(
        self,
        sources: list[tuple[str, Path]],
        project_mappings: list[tuple[str, str]],
    ) -> Snapshot: ...
