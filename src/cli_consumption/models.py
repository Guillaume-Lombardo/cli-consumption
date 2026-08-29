from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

CURRENT_SNAPSHOT_SCHEMA = 1
MIN_SUPPORTED_SNAPSHOT_SCHEMA = 1
MAX_SNAPSHOT_RECORDS = 250_000
MAX_SNAPSHOT_CONVERSATIONS = 10_000
MAX_MODELS_PER_CONVERSATION = 256
MAX_BIGINT = 9_223_372_036_854_775_807
MAX_INTEGER = 2_147_483_647

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

NonNegativeBigInt = Annotated[StrictInt, Field(ge=0, le=MAX_BIGINT)]
PositiveBigInt = Annotated[StrictInt, Field(gt=0, le=MAX_BIGINT)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0, le=MAX_INTEGER)]
Identifier64 = Annotated[StrictStr, Field(min_length=1, max_length=64)]
Identifier255 = Annotated[StrictStr, Field(min_length=1, max_length=255)]
Identifier512 = Annotated[StrictStr, Field(min_length=1, max_length=512)]
Identifier1024 = Annotated[StrictStr, Field(min_length=1, max_length=1024)]
Normalized64 = Annotated[
    StrictStr,
    Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]*$"),
]
Normalized255 = Annotated[
    StrictStr,
    Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]*$"),
]
Normalized512 = Annotated[
    StrictStr,
    Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]*$"),
]


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


Timestamp = Annotated[
    StrictStr, Field(min_length=1, max_length=64), AfterValidator(_timestamp)
]


class SnapshotValidationError(ValueError):
    """A validation failure whose text is safe to expose locally or over HTTP."""

    def __init__(self, code: str = "invalid_snapshot") -> None:
        self.code = code
        super().__init__(code)


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def reject_control_characters(cls, value: object) -> object:
        if isinstance(value, str) and any(ord(character) < 32 for character in value):
            raise ValueError("invalid text")
        return value


class TokenRecord(StrictRecord):
    input_tokens: NonNegativeBigInt
    cached_input_tokens: NonNegativeBigInt
    cache_write_input_tokens: NonNegativeBigInt
    uncached_input_tokens: NonNegativeBigInt
    output_tokens: NonNegativeBigInt
    reasoning_output_tokens: NonNegativeBigInt
    visible_output_tokens: NonNegativeBigInt
    unattributed_tokens: NonNegativeBigInt
    total_tokens: NonNegativeBigInt

    @model_validator(mode="after")
    def validate_token_composition(self) -> TokenRecord:
        if self.input_tokens != (
            self.cached_input_tokens
            + self.cache_write_input_tokens
            + self.uncached_input_tokens
        ):
            raise ValueError("invalid token composition")
        if self.output_tokens != (
            self.reasoning_output_tokens + self.visible_output_tokens
        ):
            raise ValueError("invalid token composition")
        if self.total_tokens != (
            self.input_tokens + self.output_tokens + self.unattributed_tokens
        ):
            raise ValueError("invalid token composition")
        return self


class ConversationRecord(TokenRecord):
    id: Identifier512
    provider: Normalized64
    external_id: Normalized512
    source_machine: Identifier255
    project: Identifier512
    project_source: Literal["git", "mapping", "none", "unmapped"]
    started_at: Timestamp | None
    ended_at: Timestamp | None
    duration_seconds: Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)] | None
    source: Normalized255
    models: Annotated[
        list[Normalized255], Field(max_length=MAX_MODELS_PER_CONVERSATION)
    ]
    iterations: NonNegativeInt
    model_calls: NonNegativeInt
    tool_calls: NonNegativeInt
    compactions: NonNegativeInt
    event_count: NonNegativeInt
    content_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class TurnRecord(TokenRecord):
    id: Identifier1024
    conversation_id: Identifier512
    external_id: Normalized512
    started_at: Timestamp | None
    ended_at: Timestamp | None
    status: Literal["aborted", "completed", "in-progress"]
    duration_ms: NonNegativeBigInt | None
    time_to_first_token_ms: NonNegativeBigInt | None
    model_calls: NonNegativeInt
    tool_calls: NonNegativeInt


class ModelCallRecord(TokenRecord):
    id: Identifier1024
    conversation_id: Identifier512
    turn_id: Identifier1024 | None
    sequence: NonNegativeInt
    timestamp: Timestamp | None
    model: Normalized255


class ToolCallRecord(StrictRecord):
    id: Identifier1024
    conversation_id: Identifier512
    turn_id: Identifier1024 | None
    sequence: NonNegativeInt
    timestamp: Timestamp | None
    tool_name: Normalized512
    outer_tool_name: Normalized512


class WorkItemRecord(StrictRecord):
    id: Identifier1024
    conversation_id: Identifier512
    turn_id: Identifier1024 | None
    sequence: NonNegativeInt
    kind: Literal[
        "agent-coordination",
        "command",
        "compaction",
        "dynamic-tool",
        "extension",
        "file-change",
        "mcp-tool",
        "media",
        "message",
        "other",
        "reasoning",
        "subagent-activity",
        "user-message",
    ]
    tool_name: Normalized512 | None
    started_at_ms: NonNegativeBigInt | None
    completed_at_ms: NonNegativeBigInt | None
    duration_ms: NonNegativeBigInt | None
    status: Literal["completed", "failed", "in-progress", "unknown"]


class ContextSampleRecord(StrictRecord):
    id: Identifier1024
    conversation_id: Identifier512
    turn_id: Identifier1024 | None
    sequence: NonNegativeInt
    timestamp: Timestamp | None
    input_tokens: NonNegativeBigInt
    context_window_tokens: PositiveBigInt


class TurnSettingRecord(StrictRecord):
    id: Identifier1024
    conversation_id: Identifier512
    turn_id: Identifier1024
    model: Normalized255 | None
    effort: Normalized64 | None
    collaboration_mode: Normalized64 | None
    service_tier: Normalized64 | None
    context_window_tokens: PositiveBigInt | None


class CompactionEventRecord(StrictRecord):
    id: Identifier1024
    conversation_id: Identifier512
    turn_id: Identifier1024 | None
    sequence: NonNegativeInt
    timestamp: Timestamp | None


class SubagentRecord(StrictRecord):
    id: Identifier1024
    provider: Normalized64
    source_machine: Identifier255
    parent_thread_id: Normalized512
    child_thread_id: Normalized512
    status: Literal["aborted", "completed", "failed", "in-progress", "unknown"]
    created_at_ms: NonNegativeBigInt | None
    updated_at_ms: NonNegativeBigInt | None
    agent_role: Literal[
        "other", "planning", "research", "review", "test", "unspecified", "worker"
    ]
    tokens_used: NonNegativeBigInt | None


class SnapshotPayload(StrictRecord):
    schema_version: Literal[1] = CURRENT_SNAPSHOT_SCHEMA
    provider: Normalized64
    conversations: Annotated[
        list[ConversationRecord], Field(max_length=MAX_SNAPSHOT_CONVERSATIONS)
    ] = Field(default_factory=list)
    turns: list[TurnRecord] = Field(default_factory=list)
    model_calls: list[ModelCallRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    work_items: list[WorkItemRecord] = Field(default_factory=list)
    context_samples: list[ContextSampleRecord] = Field(default_factory=list)
    turn_settings: list[TurnSettingRecord] = Field(default_factory=list)
    compaction_events: list[CompactionEventRecord] = Field(default_factory=list)
    subagents: list[SubagentRecord] = Field(default_factory=list)
    malformed_records: NonNegativeInt = 0
    duplicate_conversations: NonNegativeInt = 0

    @field_validator(
        "conversations",
        "turns",
        "model_calls",
        "tool_calls",
        "work_items",
        "context_samples",
        "turn_settings",
        "compaction_events",
        "subagents",
    )
    @classmethod
    def bound_collection(cls, value: list[object]) -> list[object]:
        if len(value) > MAX_SNAPSHOT_RECORDS:
            raise ValueError("too many records")
        return value

    @model_validator(mode="after")
    def bound_total_records(self) -> SnapshotPayload:
        total = sum(
            len(records)
            for records in (
                self.conversations,
                self.turns,
                self.model_calls,
                self.tool_calls,
                self.work_items,
                self.context_samples,
                self.turn_settings,
                self.compaction_events,
                self.subagents,
            )
        )
        if total > MAX_SNAPSHOT_RECORDS:
            raise ValueError("too many records")
        return self


def empty_tokens() -> dict[str, int]:
    return {
        **dict.fromkeys(TOKEN_FIELDS, 0),
        "uncached_input_tokens": 0,
        "visible_output_tokens": 0,
        "unattributed_tokens": 0,
    }


@dataclass(slots=True)
class Snapshot:
    """Mutable provider-neutral records built by adapters."""

    provider: str
    conversations: list[dict[str, Any]] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    work_items: list[dict[str, Any]] = field(default_factory=list)
    context_samples: list[dict[str, Any]] = field(default_factory=list)
    turn_settings: list[dict[str, Any]] = field(default_factory=list)
    compaction_events: list[dict[str, Any]] = field(default_factory=list)
    subagents: list[dict[str, Any]] = field(default_factory=list)
    malformed_records: int = 0
    duplicate_conversations: int = 0
    schema_version: int = CURRENT_SNAPSHOT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Snapshot:
        try:
            payload = SnapshotPayload.model_validate(value)
        except Exception as error:
            raise SnapshotValidationError() from error
        values = payload.model_dump()
        return cls(**values)
