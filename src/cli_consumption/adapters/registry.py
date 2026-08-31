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
TokenSemantics = Literal[
    "additive", "conversation-aggregate", "context-snapshot", "unavailable"
]
CompatibilityStatus = Literal[
    "no-data",
    "detected",
    "compatible",
    "degraded",
    "unsupported-schema",
]


@dataclass(frozen=True, slots=True)
class AdapterQualification:
    """Auditable provenance for one synthetic provider-format contract."""

    version: str
    qualified_on: str
    format: str
    fixture: str
    provenance: str
    limitations: str


@dataclass(frozen=True, slots=True)
class AdapterSpec:
    """Static registration data for a provider adapter."""

    name: str
    adapter_type: type[Adapter]
    default_home: str
    markers: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    support: SupportStatus = "supported"
    token_semantics: TokenSemantics = "unavailable"
    # Exact presentation value for maintained docs; never emitted by diagnostics.
    documented_source: str = ""
    # Repository-only qualification metadata; never emitted by diagnostics.
    qualification: AdapterQualification | None = None


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """Privacy-minimized result of checking one local provider store."""

    name: str
    aliases: tuple[str, ...]
    support: SupportStatus
    status: CompatibilityStatus
    token_semantics: TokenSemantics

    def to_dict(self) -> dict[str, str | list[str]]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "support": self.support,
            "status": self.status,
            "token_semantics": self.token_semantics,
        }


_QUALIFIED_ON = "2026-08-30"


def _qualification(
    name: str,
    version: str,
    format: str,
    provenance: str,
    limitations: str,
    *,
    qualified_on: str = _QUALIFIED_ON,
) -> AdapterQualification:
    return AdapterQualification(
        version=version,
        qualified_on=qualified_on,
        format=format,
        fixture=f"tests/test_{name.replace('-', '_')}_adapter.py",
        provenance=provenance,
        limitations=limitations,
    )


ADAPTER_SPECS = (
    AdapterSpec(
        "aider",
        AiderAdapter,
        ".aider",
        ("analytics.jsonl",),
        documented_source="~/.aider/analytics.jsonl",
        token_semantics="additive",
        qualification=_qualification(
            "aider",
            "analytics schema (unversioned)",
            "analytics JSONL",
            "https://github.com/Aider-AI/aider/tree/5dc9490bb35f9729ef2c95d00a19ccd30c26339c",
            "Opt-in analytics; no projects, tools, cache, reasoning, or durations.",
        ),
    ),
    AdapterSpec(
        "amazon-q",
        AmazonQAdapter,
        ".local/share/amazon-q",
        ("data.sqlite3",),
        documented_source="~/.local/share/amazon-q/data.sqlite3",
        qualification=_qualification(
            "amazon-q",
            "conversation state (unversioned)",
            "SQLite conversations and serialized state",
            "https://github.com/aws/amazon-q-developer-cli/tree/15cc8f3cd18c4272925ce1c7053268eedff1ea0a",
            "Persistent conversations only; token counters unavailable.",
        ),
    ),
    AdapterSpec(
        "amp",
        AmpAdapter,
        ".local/share/amp",
        ("threads",),
        documented_source="~/.local/share/amp/threads/",
        token_semantics="additive",
        qualification=_qualification(
            "amp",
            "thread mirror (unversioned)",
            "thread JSON",
            "https://web.archive.org/web/20260825165815id_/https://ampcode.com/manual",
            "No subthreads, compactions, reasoning split, or latency.",
        ),
    ),
    AdapterSpec(
        "codex",
        CodexAdapter,
        ".codex",
        ("sessions",),
        documented_source="~/.codex/sessions/",
        token_semantics="additive",
        qualification=_qualification(
            "codex",
            "rollout schema (unversioned)",
            "session rollout JSONL",
            "https://github.com/openai/codex/tree/0a12b855a0b21068108a8a3b311d492712737e0f",
            "Local rollout metadata only; provider internals may evolve.",
        ),
    ),
    AdapterSpec(
        "copilot",
        CopilotAdapter,
        ".copilot",
        ("session-state",),
        documented_source="~/.copilot/session-state/",
        token_semantics="conversation-aggregate",
        qualification=_qualification(
            "copilot",
            "CLI 1.0.80 / event schema v1",
            "session event JSONL",
            "https://github.com/github/copilot-cli/tree/v1.0.80",
            "Shutdown aggregates only; no per-turn token attribution.",
        ),
    ),
    AdapterSpec(
        "continue",
        ContinueAdapter,
        ".continue",
        ("sessions",),
        documented_source="~/.continue/sessions/",
        token_semantics="additive",
        qualification=_qualification(
            "continue",
            "session schema (unversioned)",
            "session JSON",
            "https://github.com/continuedev/continue/tree/5522c6f44ca0ac3528b37244818fbfa39b5af470",
            "No reliable message timing, context windows, compactions, or latency.",
        ),
    ),
    AdapterSpec(
        "crush",
        CrushAdapter,
        ".local/share/crush",
        ("projects.json", "crush.db", ".crush/crush.db"),
        documented_source="~/.local/share/crush/",
        token_semantics="context-snapshot",
        qualification=_qualification(
            "crush",
            "CLI 0.91.2",
            "project registry and additive SQLite migrations",
            "https://github.com/charmbracelet/crush/tree/v0.91.2",
            "Latest context snapshot only; no additive per-call usage.",
        ),
    ),
    AdapterSpec(
        "cursor",
        CursorAdapter,
        ".cursor",
        ("chats", "projects/*/agent-transcripts"),
        documented_source="~/.cursor/",
        qualification=_qualification(
            "cursor",
            "Composer 2",
            "transcript JSONL and chat SQLite",
            "https://web.archive.org/web/20260815113223id_/https://cursor.com/docs/cli/overview",
            "No per-message timing or tokens; model attribution is incomplete.",
        ),
    ),
    AdapterSpec(
        "gemini",
        GeminiAdapter,
        ".gemini",
        ("tmp",),
        documented_source="~/.gemini/tmp/",
        token_semantics="additive",
        qualification=_qualification(
            "gemini",
            "session history (unversioned)",
            "active history JSON and JSONL",
            "https://github.com/google-gemini/gemini-cli/tree/0bd1d439751478771c45d3d0895a6a9760554bf4",
            "Nested agents excluded; hashed projects are not reversed.",
        ),
    ),
    AdapterSpec(
        "goose",
        GooseAdapter,
        ".local/share/goose/sessions",
        ("sessions.db",),
        documented_source="~/.local/share/goose/sessions/sessions.db",
        token_semantics="additive",
        qualification=_qualification(
            "goose",
            "CLI 1.47.0 / schema v16",
            "SQLite sessions and usage ledger",
            "https://github.com/aaif-goose/goose/tree/v1.47.0",
            "Schema v16 only; no legacy JSONL, subagents, reasoning, or latency.",
        ),
    ),
    AdapterSpec(
        "grok",
        GrokAdapter,
        ".grok",
        ("sessions/*/*/summary.json",),
        documented_source="~/.grok/sessions/",
        token_semantics="additive",
        qualification=_qualification(
            "grok",
            "session schema (unversioned)",
            "summary, updates, and events JSONL",
            "https://github.com/xai-org/grok-build/tree/bc7f02eddd3d84085849dc19ed216f11c23b0571",
            "No costs, subagent relationships, rewinds, or manual compactions.",
        ),
    ),
    AdapterSpec(
        "claude",
        ClaudeAdapter,
        ".claude",
        ("projects/*/*.jsonl",),
        documented_source="~/.claude/projects/",
        aliases=("claude-code",),
        token_semantics="additive",
        qualification=_qualification(
            "claude",
            "transcript schema (unversioned)",
            "project session JSONL",
            "https://github.com/anthropics/claude-code/tree/f1af9b1f4b1fd4c776135381606edada82ef638e",
            "Main sessions only; no subagents, context windows, effort, or latency.",
        ),
    ),
    AdapterSpec(
        "cline",
        ClineAdapter,
        ".cline/data",
        ("sessions/sessions.db",),
        documented_source="~/.cline/data/sessions/sessions.db",
        token_semantics="additive",
        qualification=_qualification(
            "cline",
            "SDK session schema (unversioned)",
            "SQLite session index and message JSON",
            "https://github.com/cline/cline/tree/48d63852745460ff0fa3dfcc0457bbe2493841de",
            "No costs or arbitrary task metadata; artifacts must remain present.",
        ),
    ),
    AdapterSpec(
        "kilo",
        KiloAdapter,
        ".local/share/kilo",
        ("kilo.db",),
        documented_source="~/.local/share/kilo/kilo.db",
        token_semantics="additive",
        qualification=_qualification(
            "kilo",
            "CLI 7.5.5",
            "SQLite session, message, and part tables",
            "https://github.com/Kilo-Org/kilocode/tree/v7.5.5",
            "CLI store only; no legacy IDE tasks, cloud sessions, or subagents.",
        ),
    ),
    AdapterSpec(
        "kimi",
        KimiAdapter,
        ".kimi",
        ("sessions/*/*/wire.jsonl",),
        documented_source="~/.kimi/sessions/",
        token_semantics="additive",
        qualification=_qualification(
            "kimi",
            "Wire v1",
            "wire event JSONL",
            "https://github.com/MoonshotAI/kimi-cli/tree/cbc15c076d17f70fec9f89c90c0502e68657f505",
            "Selected model unavailable; hashed work directories are not reversed.",
        ),
    ),
    AdapterSpec(
        "mistral-vibe",
        MistralVibeAdapter,
        ".vibe",
        ("logs/session/*/meta.json",),
        documented_source="~/.vibe/logs/session/",
        token_semantics="conversation-aggregate",
        qualification=_qualification(
            "mistral-vibe",
            "CLI 2.24.5",
            "session meta JSON and messages JSONL",
            "https://github.com/mistralai/mistral-vibe/tree/v2.24.5",
            "Session aggregates only; no timing or historical model attribution.",
        ),
    ),
    AdapterSpec(
        "opencode",
        OpenCodeAdapter,
        ".local/share/opencode",
        ("opencode.db",),
        documented_source="~/.local/share/opencode/opencode.db",
        token_semantics="additive",
        qualification=_qualification(
            "opencode",
            "CLI 1.18.23 / SQLite v2",
            "SQLite session plus current message/part or projection records",
            "https://github.com/anomalyco/opencode/tree/v1.18.23",
            "No pre-v2 JSON, child sessions, context windows, or costs.",
            qualified_on="2026-08-31",
        ),
    ),
    AdapterSpec(
        "openhands",
        OpenHandsAdapter,
        ".openhands",
        ("conversations/*/base_state.json",),
        documented_source="~/.openhands/conversations/",
        token_semantics="additive",
        qualification=_qualification(
            "openhands",
            "CLI 1.16.0",
            "SDK base state and event JSON",
            "https://github.com/OpenHands/OpenHands/tree/v1.16.0",
            "Local SDK persistence only; no cloud conversations or delegates.",
        ),
    ),
    AdapterSpec(
        "pi",
        PiAdapter,
        ".pi/agent",
        ("sessions",),
        documented_source="~/.pi/agent/sessions/",
        token_semantics="additive",
        qualification=_qualification(
            "pi",
            "session schema v3",
            "branched session JSONL",
            "https://github.com/earendil-works/pi/tree/853a80d26c90a14c1886f0ebb8ffaae133ca2185",
            "All branches counted; no branch graph, context windows, or durations.",
        ),
    ),
    AdapterSpec(
        "plandex",
        PlandexAdapter,
        "/plandex-server",
        ("orgs/*/plans/*/conversation",),
        documented_source="/plandex-server",
        token_semantics="additive",
        qualification=_qualification(
            "plandex",
            "conversation JSON (unversioned)",
            "self-hosted conversation JSON",
            "https://github.com/plandex-ai/plandex/tree/e2d772072efadbe41d2946d97d79be55532dbab5",
            "Offline self-hosted copy only; models and tools unavailable.",
        ),
    ),
    AdapterSpec(
        "qwen",
        QwenAdapter,
        ".qwen",
        ("projects/*/chats",),
        documented_source="~/.qwen/projects/",
        token_semantics="additive",
        qualification=_qualification(
            "qwen",
            "CLI 0.22.2",
            "active-branch chat JSONL",
            "https://github.com/QwenLM/qwen-code/tree/v0.22.2",
            "Archived and sidechain sessions excluded; cache writes unavailable.",
        ),
    ),
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
    return ProviderDiagnostic(
        spec.name, spec.aliases, spec.support, status, spec.token_semantics
    )
