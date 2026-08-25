"""Analyze AI coding CLI usage without exporting conversation content."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cli-consumption")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

__all__ = ["__version__"]
