from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cli_consumption.adapters.aider import AiderAdapter
from cli_consumption.adapters.amazon_q import AmazonQAdapter
from cli_consumption.adapters.amp import AmpAdapter
from cli_consumption.adapters.base import Adapter, UnsupportedProviderFormat
from cli_consumption.adapters.claude import ClaudeAdapter
from cli_consumption.adapters.cline import ClineAdapter
from cli_consumption.adapters.codex import CodexAdapter
from cli_consumption.adapters.continue_cli import ContinueAdapter
from cli_consumption.adapters.copilot import CopilotAdapter
from cli_consumption.adapters.crush import CrushAdapter
from cli_consumption.adapters.cursor import CursorAdapter
from cli_consumption.adapters.gemini import GeminiAdapter
from cli_consumption.adapters.goose import GooseAdapter
from cli_consumption.adapters.grok import GrokAdapter
from cli_consumption.adapters.kilo import KiloAdapter
from cli_consumption.adapters.kimi import KimiAdapter
from cli_consumption.adapters.mistral_vibe import MistralVibeAdapter
from cli_consumption.adapters.opencode import OpenCodeAdapter
from cli_consumption.adapters.openhands import OpenHandsAdapter
from cli_consumption.adapters.pi import PiAdapter
from cli_consumption.adapters.plandex import PlandexAdapter
from cli_consumption.adapters.qwen import QwenAdapter

SupportStatus = Literal["supported"]
CompatibilityStatus = Literal[
    "no-data",
    "detected",
    "compatible",
    "degraded",
    "unsupported-schema",
]


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """Static registration data for a provider adapter."""

    name: str
    adapter_type: type[Adapter]
    default_home: str
    markers: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    support: SupportStatus = "supported"


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """Privacy-minimized result of checking one local provider store."""

    name: str
    aliases: tuple[str, ...]
    support: SupportStatus
    status: CompatibilityStatus

    def to_dict(self) -> dict[str, str | list[str]]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "support": self.support,
            "status": self.status,
        }


ADAPTER_SPECS = (
    AdapterSpec("aider", AiderAdapter, ".aider", ("analytics.jsonl",)),
    AdapterSpec("amazon-q", AmazonQAdapter, ".local/share/amazon-q", ("data.sqlite3",)),
    AdapterSpec("amp", AmpAdapter, ".local/share/amp", ("threads",)),
    AdapterSpec("codex", CodexAdapter, ".codex", ("sessions",)),
    AdapterSpec("copilot", CopilotAdapter, ".copilot", ("session-state",)),
    AdapterSpec("continue", ContinueAdapter, ".continue", ("sessions",)),
    AdapterSpec(
        "crush",
        CrushAdapter,
        ".local/share/crush",
        ("projects.json", "crush.db", ".crush/crush.db"),
    ),
    AdapterSpec(
        "cursor",
        CursorAdapter,
        ".cursor",
        ("chats", "projects/*/agent-transcripts"),
    ),
    AdapterSpec("gemini", GeminiAdapter, ".gemini", ("tmp",)),
    AdapterSpec("goose", GooseAdapter, ".local/share/goose/sessions", ("sessions.db",)),
    AdapterSpec("grok", GrokAdapter, ".grok", ("sessions/*/*/summary.json",)),
    AdapterSpec(
        "claude",
        ClaudeAdapter,
        ".claude",
        ("projects/*/*.jsonl",),
        aliases=("claude-code",),
    ),
    AdapterSpec("cline", ClineAdapter, ".cline/data", ("sessions/sessions.db",)),
    AdapterSpec("kilo", KiloAdapter, ".local/share/kilo", ("kilo.db",)),
    AdapterSpec("kimi", KimiAdapter, ".kimi", ("sessions/*/*/wire.jsonl",)),
    AdapterSpec(
        "mistral-vibe",
        MistralVibeAdapter,
        ".vibe",
        ("logs/session/*/meta.json",),
    ),
    AdapterSpec("opencode", OpenCodeAdapter, ".local/share/opencode", ("opencode.db",)),
    AdapterSpec(
        "openhands",
        OpenHandsAdapter,
        ".openhands",
        ("conversations/*/base_state.json",),
    ),
    AdapterSpec("pi", PiAdapter, ".pi/agent", ("sessions",)),
    AdapterSpec(
        "plandex",
        PlandexAdapter,
        "/plandex-server",
        ("orgs/*/plans/*/conversation",),
    ),
    AdapterSpec("qwen", QwenAdapter, ".qwen", ("projects/*/chats",)),
)


def resolve_adapter_spec(name: str) -> AdapterSpec | None:
    """Resolve a canonical provider name or documented alias."""
    return next(
        (spec for spec in ADAPTER_SPECS if name == spec.name or name in spec.aliases),
        None,
    )


def default_source_path(spec: AdapterSpec, home: Path | None = None) -> Path:
    """Return the provider's local source path without requiring it to exist."""
    path = Path(spec.default_home).expanduser()
    if not path.is_absolute():
        path = (home or Path.home()) / path
    return path.resolve()


def has_provider_data(spec: AdapterSpec, path: Path) -> bool:
    """Return whether a source contains any of the provider's detection markers."""
    return any(
        any(path.glob(marker)) if "*" in marker else (path / marker).exists()
        for marker in spec.markers
    )


def diagnose_provider(spec: AdapterSpec, path: Path) -> ProviderDiagnostic:
    """Check a detected store without persisting or exposing provider data."""
    status: CompatibilityStatus
    if not has_provider_data(spec, path):
        status = "no-data"
    else:
        try:
            snapshot = spec.adapter_type().collect([("local", path)], [])
        except UnsupportedProviderFormat:
            status = "unsupported-schema"
        except Exception:  # Provider stores are untrusted; diagnostics stay bounded.
            status = "degraded"
        else:
            if snapshot.malformed_records:
                status = "degraded"
            elif snapshot.conversations:
                status = "compatible"
            else:
                status = "detected"
    return ProviderDiagnostic(spec.name, spec.aliases, spec.support, status)
