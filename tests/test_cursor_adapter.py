from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.cursor import CursorAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "cursor privacy canary secret"
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _line(role: str, content: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "role": role,
            "message": {
                "content": content,
                "model": CANARY,
                "usage": {"input_tokens": 999, "secret": CANARY},
            },
            "timestamp": CANARY,
            "cwd": CANARY,
            "arbitrary": {"secret": CANARY},
        }
    )


def _meta_db(home: Path, *, session_id: str = SESSION_ID) -> Path:
    workspace = hashlib.md5(b"/srv/work/project", usedforsecurity=False).hexdigest()
    path = home / "chats" / workspace / session_id / "store.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE blobs (key TEXT PRIMARY KEY, value BLOB)")
    metadata = {
        "agentId": session_id,
        "createdAt": 1787824800000,
        "lastUsedModel": "claude-4-sonnet",
        "mode": CANARY,
        "name": CANARY,
        "credentials": CANARY,
    }
    connection.execute(
        "INSERT INTO meta VALUES (?, ?)",
        ("0", json.dumps(metadata).encode().hex()),
    )
    connection.execute("INSERT INTO blobs VALUES (?, ?)", (CANARY, CANARY))
    connection.commit()
    connection.close()
    return path


def _transcript(home: Path, *, extra: bool = False) -> Path:
    path = (
        home
        / "projects"
        / "srv-work-project"
        / "agent-transcripts"
        / SESSION_ID
        / f"{SESSION_ID}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        _line(
            "user",
            [{"type": "text", "text": f"<user_info>{CANARY}</user_info>"}],
        ),
        _line(
            "user",
            [
                {
                    "type": "text",
                    "text": f"<user_query>{CANARY}</user_query>",
                }
            ],
        ),
        _line(
            "assistant",
            [
                {"type": "thinking", "thinking": CANARY},
                {"type": "text", "text": CANARY},
                {
                    "type": "tool_use",
                    "name": "Shell",
                    "input": {"command": CANARY, "secret": CANARY},
                },
                {
                    "type": "tool_result",
                    "name": CANARY,
                    "content": CANARY,
                },
            ],
        ),
        _line("assistant", [{"type": "text", "text": CANARY}]),
        _line("user", [{"type": "text", "text": CANARY}]),
        _line(
            "assistant",
            [
                {
                    "type": "tool-call",
                    "tool": "Read",
                    "arguments": {"path": CANARY},
                }
            ],
        ),
    ]
    if extra:
        lines.append(_line("assistant", [{"type": "text", "text": CANARY}]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_collects_composer_transcript_and_session_meta(tmp_path: Path) -> None:
    home = tmp_path / "cursor"
    _transcript(home)
    _meta_db(home)

    snapshot = CursorAdapter().collect(
        [("laptop", home)], [("project", "/srv/work/project")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"] == f"cursor:{SESSION_ID}"
    assert conversation["source_machine"] == "laptop"
    assert conversation["project"] == "project"
    assert conversation["project_source"] == "mapping"
    assert conversation["models"] == ["claude-4-sonnet"]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 3
    assert conversation["tool_calls"] == 2
    assert conversation["input_tokens"] == 0
    assert conversation["total_tokens"] == 0
    assert [turn["status"] for turn in snapshot.turns] == [
        "completed",
        "completed",
    ]
    assert [turn["model_calls"] for turn in snapshot.turns] == [2, 1]
    assert [call["model"] for call in snapshot.model_calls] == [
        "unknown",
        "unknown",
        "unknown",
    ]
    assert [call["tool_name"] for call in snapshot.tool_calls] == ["Shell", "Read"]
    assert all(call["timestamp"] is None for call in snapshot.model_calls)
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_deduplicates_and_tolerates_malformed_or_partial_sources(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _transcript(first)
    path = _transcript(second, extra=True)
    _meta_db(second)
    with path.open("ab") as handle:
        handle.write(b"not-json\n[]\n")
        handle.write(
            json.dumps({"role": "assistant", "message": CANARY}).encode() + b"\n"
        )

    snapshot = CursorAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records >= 3
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["model_calls"] == 4
    assert CANARY not in json.dumps(snapshot.to_dict())
    assert (
        snapshot.to_dict()
        == CursorAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    db_only = tmp_path / "db-only"
    _meta_db(db_only, session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    partial = CursorAdapter().collect([("machine", db_only)])
    assert partial.conversations[0]["source"] == "local-sqlite-meta"
    assert partial.conversations[0]["iterations"] == 0
    assert partial.conversations[0]["models"] == ["claude-4-sonnet"]

    malformed_db = tmp_path / "malformed-db"
    bad_path = malformed_db / "chats" / "workspace" / "session" / "store.db"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_bytes(b"not sqlite")
    malformed_snapshot = CursorAdapter().collect([("machine", malformed_db)])
    assert malformed_snapshot.malformed_records == 1
    assert malformed_snapshot.conversations == []

    with pytest.raises(ValueError, match="Missing Cursor CLI projects or chats"):
        CursorAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_and_ingestion_is_idempotent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    home = tmp_path / "cursor"
    _transcript(home)
    _meta_db(home)
    snapshot = CursorAdapter().collect([("machine", home)])
    api_body = json.dumps(snapshot.to_dict())
    assert CANARY not in api_body
    assert CANARY not in caplog.text

    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        first = ingest_snapshot(engine, snapshot)
        second = ingest_snapshot(engine, snapshot)
        assert (first.written, first.skipped) == (1, 0)
        assert (second.written, second.skipped) == (0, 1)
        rows = {name: read_table(engine, name) for name in TABLES}
        assert CANARY not in json.dumps(rows)
        output = tmp_path / "reports"
        paths = export_csv(engine, output)
        dashboard = output / "dashboard.html"
        generate_dashboard(engine, dashboard)
        assert all(CANARY not in path.read_text() for path in paths)
        assert CANARY not in dashboard.read_text()
    finally:
        engine.dispose()
