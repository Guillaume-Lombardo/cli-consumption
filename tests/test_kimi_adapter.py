from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_consumption.adapters.kimi import KimiAdapter

CANARY = "SECRET_CANARY_kimi_do_not_export"


def _fixture(home: Path) -> None:
    wire = home / "sessions" / "work-key" / "kimi-1" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    records = [
        {"type": "metadata", "protocol_version": "1.10"},
        {
            "timestamp": 1787824800.0,
            "message": {"type": "TurnBegin", "payload": {"user_input": CANARY}},
        },
        {
            "timestamp": 1787824800.5,
            "message": {
                "type": "StatusUpdate",
                "payload": {
                    "token_usage": {
                        "input_other": 10,
                        "input_cache_read": 3,
                        "input_cache_creation": 2,
                        "output": 4,
                    },
                    "context_tokens": 15,
                    "max_context_tokens": 128000,
                    "message_id": "model-1",
                },
            },
        },
        {
            "timestamp": 1787824801.0,
            "message": {
                "type": "ToolCall",
                "payload": {
                    "id": "tool-1",
                    "function": {"name": "Shell", "arguments": CANARY},
                },
            },
        },
        {
            "timestamp": 1787824801.5,
            "message": {"type": "CompactionEnd", "payload": {}},
        },
        {"timestamp": 1787824802.0, "message": {"type": "TurnEnd", "payload": {}}},
    ]
    wire.write_text(
        "\n".join(json.dumps(record) for record in records) + "\nnot-json\n",
        encoding="utf-8",
    )


def test_collects_kimi_wire_metadata_and_discards_content(tmp_path: Path) -> None:
    home = tmp_path / "kimi"
    _fixture(home)
    snapshot = KimiAdapter().collect([("desktop", home)])

    assert snapshot.conversations[0]["input_tokens"] == 15
    assert snapshot.conversations[0]["output_tokens"] == 4
    assert snapshot.tool_calls[0]["tool_name"] == "Shell"
    assert len(snapshot.context_samples) == 1
    assert len(snapshot.compaction_events) == 1
    assert snapshot.malformed_records == 1
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_kimi_deduplicates_and_rejects_missing_source(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _fixture(first)
    _fixture(second)
    snapshot = KimiAdapter().collect([("a", first), ("b", second)])
    assert snapshot.duplicate_conversations == 1
    with pytest.raises(ValueError, match="Missing Kimi"):
        KimiAdapter().collect([("x", tmp_path / "missing")])
