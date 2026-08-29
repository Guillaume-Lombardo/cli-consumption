from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.qwen import QwenAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)

CANARY = "qwen privacy canary secret"


def _record(
    uuid: str,
    parent: str | None,
    record_type: str,
    timestamp: str,
    **values: Any,
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "qwen-session-1",
        "timestamp": timestamp,
        "type": record_type,
        "cwd": f"/srv/work/acme/{CANARY}",
        "version": "0.22.2",
        "gitBranch": CANARY,
        **values,
    }


def session(home: Path, *, extra: bool = False) -> Path:
    records = [
        _record(
            "prompt-1",
            None,
            "user",
            "2026-08-26T10:00:00Z",
            message={"role": "user", "parts": [{"text": CANARY}]},
            systemPayload={"displayText": CANARY, "hookContext": CANARY},
        ),
        _record(
            "response-1",
            "prompt-1",
            "assistant",
            "2026-08-26T10:00:01Z",
            model="qwen3-coder-plus",
            contextWindowSize=262_144,
            message={
                "role": "model",
                "parts": [{"thought": True, "text": CANARY}],
            },
        ),
        _record(
            "response-1",
            "prompt-1",
            "assistant",
            "2026-08-26T10:00:01.500Z",
            usageMetadata={
                "promptTokenCount": 100,
                "cachedContentTokenCount": 40,
                "candidatesTokenCount": 20,
                "thoughtsTokenCount": 5,
                "toolUsePromptTokenCount": 10,
                "totalTokenCount": 130,
            },
            message={
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "id": "call-1",
                            "name": "read_file",
                            "args": {"path": CANARY},
                        }
                    },
                ],
            },
        ),
        _record(
            "tool-result-1",
            "response-1",
            "tool_result",
            "2026-08-26T10:00:02Z",
            message={
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-1",
                            "name": "read_file",
                            "response": {"output": CANARY},
                        }
                    }
                ],
            },
            toolCallResult={"resultDisplay": CANARY, "error": CANARY},
        ),
        _record(
            "response-2",
            "tool-result-1",
            "assistant",
            "2026-08-26T10:00:03Z",
            model="qwen3-coder-plus",
            usageMetadata={
                "promptTokenCount": 20,
                "candidatesTokenCount": 4,
                "totalTokenCount": 24,
            },
            message={"role": "model", "parts": [{"text": CANARY}]},
        ),
        _record(
            "discarded-prompt",
            "response-2",
            "user",
            "2026-08-26T10:00:04Z",
            message={"role": "user", "parts": [{"text": CANARY}]},
        ),
        _record(
            "discarded-response",
            "discarded-prompt",
            "assistant",
            "2026-08-26T10:00:05Z",
            model="discarded-model",
            usageMetadata={"totalTokenCount": 999_999},
            message={"role": "model", "parts": [{"text": CANARY}]},
        ),
        _record(
            "rewind-1",
            "response-2",
            "system",
            "2026-08-26T10:00:06Z",
            subtype="rewind",
            systemPayload={"truncatedCount": 2, "secret": CANARY},
        ),
        _record(
            "prompt-2",
            "rewind-1",
            "user",
            "2026-08-26T10:00:07Z",
            message={"role": "user", "parts": [{"text": CANARY}]},
        ),
        _record(
            "response-3",
            "prompt-2",
            "assistant",
            "2026-08-26T10:00:08Z",
            model="qwen3-coder-flash",
            usageMetadata={
                "promptTokenCount": 3,
                "candidatesTokenCount": 1,
                "totalTokenCount": 4,
            },
            message={"role": "model", "parts": [{"text": CANARY}]},
        ),
        _record(
            "compression-1",
            "response-3",
            "system",
            "2026-08-26T10:00:09Z",
            subtype="chat_compression",
            systemPayload={"compressedHistory": [{"text": CANARY}]},
        ),
    ]
    if extra:
        records.append(
            _record(
                "metadata-1",
                "compression-1",
                "system",
                "2026-08-26T10:00:10Z",
                subtype="custom_title",
                systemPayload={"title": CANARY},
            )
        )
    path = home / "projects" / "-srv-work-acme" / "chats" / "qwen-session-1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def test_collects_active_branch_usage_tools_context_and_compactions(
    tmp_path: Path,
) -> None:
    home = tmp_path / "qwen"
    session(home)
    snapshot = QwenAdapter().collect([("laptop", home)], [("acme", "/srv/work/acme")])

    conversation = snapshot.conversations[0]
    assert conversation["id"] == "qwen:qwen-session-1"
    assert conversation["project"] == "acme"
    assert conversation["project_source"] == "mapping"
    assert conversation["models"] == ["qwen3-coder-flash", "qwen3-coder-plus"]
    assert conversation["iterations"] == 2
    assert conversation["model_calls"] == 3
    assert conversation["tool_calls"] == 1
    assert conversation["compactions"] == 1
    assert conversation["input_tokens"] == 123
    assert conversation["uncached_input_tokens"] == 83
    assert conversation["cached_input_tokens"] == 40
    assert conversation["output_tokens"] == 30
    assert conversation["reasoning_output_tokens"] == 5
    assert conversation["visible_output_tokens"] == 25
    assert conversation["total_tokens"] == 158
    assert conversation["unattributed_tokens"] == 5
    assert [turn["status"] for turn in snapshot.turns] == [
        "completed",
        "completed",
    ]
    assert snapshot.tool_calls[0]["tool_name"] == "read_file"
    assert snapshot.context_samples[0]["context_window_tokens"] == 262_144
    assert snapshot.compaction_events[0]["turn_id"] == ("qwen:qwen-session-1:prompt-2")
    assert CANARY not in json.dumps(snapshot.to_dict())
    assert "discarded-model" not in json.dumps(snapshot.to_dict())


def test_deduplicates_copies_and_rejects_malformed_records(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    session(first)
    path = session(second, extra=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")
        handle.write(
            json.dumps(
                {
                    "uuid": CANARY,
                    "parentUuid": "metadata-1",
                    "sessionId": "qwen-session-1",
                    "type": "assistant",
                    "timestamp": "invalid",
                    "model": CANARY,
                    "message": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": CANARY,
                                    "args": {"secret": CANARY},
                                }
                            }
                        ]
                    },
                }
            )
            + "\n"
        )

    snapshot = QwenAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 3
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert CANARY not in json.dumps(snapshot.to_dict())
    assert (
        snapshot.to_dict()
        == QwenAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Qwen Code projects directory"):
        QwenAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_and_ingestion_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "qwen"
    session(home)
    snapshot = QwenAdapter().collect([("machine", home)])
    assert CANARY not in json.dumps(snapshot.to_dict())

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
