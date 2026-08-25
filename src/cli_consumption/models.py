from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def empty_tokens() -> dict[str, int]:
    return {
        **dict.fromkeys(TOKEN_FIELDS, 0),
        "uncached_input_tokens": 0,
        "visible_output_tokens": 0,
        "unattributed_tokens": 0,
    }


@dataclass(slots=True)
class Snapshot:
    """Provider-neutral usage records ready for persistence or transport."""

    provider: str
    conversations: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    subagents: list[dict[str, Any]] = field(default_factory=list)
    malformed_records: int = 0
    duplicate_conversations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Snapshot:
        return cls(
            provider=str(value["provider"]),
            conversations=list(value.get("conversations", [])),
            turns=list(value.get("turns", [])),
            model_calls=list(value.get("model_calls", [])),
            tool_calls=list(value.get("tool_calls", [])),
            subagents=list(value.get("subagents", [])),
            malformed_records=int(value.get("malformed_records", 0)),
            duplicate_conversations=int(value.get("duplicate_conversations", 0)),
        )
