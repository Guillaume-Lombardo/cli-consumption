from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cli_consumption.adapters.amazon_q import AmazonQAdapter

CANARY = "SECRET_CANARY_amazon_q_do_not_export"


def _fixture(home: Path) -> None:
    home.mkdir(parents=True)
    database = sqlite3.connect(home / "data.sqlite3")
    database.execute("CREATE TABLE conversations (key TEXT PRIMARY KEY, value TEXT)")
    state = {
        "conversation_id": "amazon-q-1",
        "transcript": [CANARY],
        "history": [
            {
                "user": {
                    "timestamp": "2026-08-27T10:00:00Z",
                    "content": {"Prompt": {"prompt": CANARY}},
                },
                "assistant": {"content": CANARY},
                "request_metadata": {
                    "message_id": "request-1",
                    "request_start_timestamp_ms": 1787824800000,
                    "stream_end_timestamp_ms": 1787824802000,
                    "model_id": "claude-sonnet-4",
                    "tool_use_ids_and_names": [["tool-1", "execute_bash"]],
                    "message_meta_tags": [{"secret": CANARY}],
                },
            }
        ],
    }
    database.execute(
        "INSERT INTO conversations VALUES (?, ?)", ("/srv/acme", json.dumps(state))
    )
    database.execute("INSERT INTO conversations VALUES (?, ?)", ("broken", "not-json"))
    database.commit()
    database.close()


def test_collects_amazon_q_metadata_and_discards_content(tmp_path: Path) -> None:
    home = tmp_path / "amazon-q"
    _fixture(home)
    snapshot = AmazonQAdapter().collect([("desktop", home)], [("acme", "/srv/acme")])

    assert snapshot.conversations[0]["project"] == "acme"
    assert snapshot.conversations[0]["models"] == ["claude-sonnet-4"]
    assert snapshot.tool_calls[0]["tool_name"] == "execute_bash"
    assert snapshot.malformed_records == 1
    assert snapshot.conversations[0]["total_tokens"] == 0
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_amazon_q_deduplicates_and_rejects_missing_source(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _fixture(first)
    _fixture(second)
    snapshot = AmazonQAdapter().collect([("a", first), ("b", second)])
    assert snapshot.duplicate_conversations == 1
    with pytest.raises(ValueError, match="Missing Amazon Q"):
        AmazonQAdapter().collect([("x", tmp_path / "missing")])
