from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cli_consumption.adapters.cline import ClineAdapter

CANARY = "SECRET_CANARY_cline_do_not_export"


def _fixture(home: Path, *, suffix: str = "") -> None:
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    database = sqlite3.connect(sessions / "sessions.db")
    database.execute(
        "CREATE TABLE sessions (session_id TEXT, started_at TEXT, ended_at TEXT, "
        "updated_at TEXT, status TEXT, provider TEXT, model TEXT, cwd TEXT, "
        "workspace_root TEXT, metadata_json TEXT, messages_path TEXT)"
    )
    artifact = sessions / "cline-1" / "cline-1.messages.json"
    artifact.parent.mkdir()
    artifact.write_text(
        json.dumps(
            {
                "version": 1,
                "messages": [
                    {
                        "id": "u1",
                        "role": "user",
                        "content": [{"type": "text", "text": CANARY}],
                    },
                    {
                        "id": "a1",
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": CANARY},
                            {
                                "type": "tool_use",
                                "name": "editor",
                                "input": {"secret": CANARY},
                            },
                        ],
                        "metrics": {
                            "inputTokens": 12,
                            "cacheReadTokens": 2,
                            "cacheWriteTokens": 3,
                            "outputTokens": 4,
                        },
                    },
                    "malformed",
                ],
            }
        ),
        encoding="utf-8",
    )
    database.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cline-1",
            "2026-08-27T10:00:00Z",
            "2026-08-27T10:00:02Z",
            f"2026-08-27T10:00:02{suffix}Z",
            "completed",
            "anthropic",
            "claude-sonnet",
            "/srv/acme",
            "/srv/acme",
            json.dumps({"secret": CANARY}),
            str(artifact),
        ),
    )
    database.commit()
    database.close()


def test_collects_cline_metadata_and_discards_content(tmp_path: Path) -> None:
    home = tmp_path / "cline"
    _fixture(home)
    snapshot = ClineAdapter().collect([("laptop", home)], [("acme", "/srv/acme")])

    assert len(snapshot.conversations) == 1
    assert snapshot.conversations[0]["provider"] == "cline"
    assert snapshot.conversations[0]["project"] == "acme"
    assert snapshot.conversations[0]["input_tokens"] == 12
    assert snapshot.conversations[0]["output_tokens"] == 4
    assert snapshot.tool_calls[0]["tool_name"] == "editor"
    assert snapshot.malformed_records == 1
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_cline_deduplicates_and_rejects_missing_source(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _fixture(first)
    _fixture(second)
    snapshot = ClineAdapter().collect([("a", first), ("b", second)])
    assert snapshot.duplicate_conversations == 1
    assert len(snapshot.conversations) == 1
    with pytest.raises(ValueError, match="Missing Cline"):
        ClineAdapter().collect([("x", tmp_path / "missing")])
