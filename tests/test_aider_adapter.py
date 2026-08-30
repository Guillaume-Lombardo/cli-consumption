from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from storage_helpers import read_table

from cli_consumption.adapters.aider import AiderAdapter
from cli_consumption.dashboard import generate_dashboard
from cli_consumption.exporting import export_csv
from cli_consumption.storage import (
    TABLES,
    create_database_engine,
    ingest_snapshot,
)
from cli_consumption.sync import send_snapshot

CANARY = "aider privacy canary secret"
USER_ID = "11111111-2222-4333-8444-555555555555"


def analytics(home: Path, *, complete: bool = True) -> Path:
    events: list[dict[str, Any]] = [
        {
            "event": "launched",
            "properties": {"system_prompt": CANARY},
            "user_id": USER_ID,
            "time": 1_777_300_000,
        },
        {
            "event": "repo",
            "properties": {"path": f"/private/{CANARY}", "num_files": 42},
            "user_id": USER_ID,
            "time": 1_777_300_001,
        },
        {
            "event": "message_send_starting",
            "properties": {"prompt": CANARY},
            "user_id": USER_ID,
            "time": 1_777_300_002,
        },
        {
            "event": "message_send",
            "properties": {
                "main_model": "anthropic/claude-sonnet-4-6",
                "weak_model": CANARY,
                "editor_model": CANARY,
                "edit_format": "diff",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 125,
                "cost": 1.25,
                "total_cost": 2.5,
                "secret": CANARY,
            },
            "user_id": USER_ID,
            "time": 1_777_300_004,
        },
        {
            "event": "command_run",
            "properties": {"command": CANARY, "output": CANARY},
            "user_id": USER_ID,
            "time": 1_777_300_005,
        },
        {
            "event": "message_send_starting",
            "properties": {},
            "user_id": USER_ID,
            "time": 1_777_300_006,
        },
        {
            "event": "message_send_exception",
            "properties": {"exception": CANARY, "traceback": CANARY},
            "user_id": USER_ID,
            "time": 1_777_300_007,
        },
    ]
    if complete:
        events.append(
            {
                "event": "exit",
                "properties": {"reason": CANARY},
                "user_id": USER_ID,
                "time": 1_777_300_010,
            }
        )
    path = home / "analytics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def test_collects_sessions_models_tokens_and_attempts(tmp_path: Path) -> None:
    home = tmp_path / "aider"
    analytics(home)

    snapshot = AiderAdapter().collect(
        [("laptop", home)], [("must-not-map", "/private")]
    )

    conversation = snapshot.conversations[0]
    assert conversation["id"].startswith("aider:")
    assert USER_ID not in conversation["id"]
    assert conversation["project"] == "outside-project"
    assert conversation["models"] == ["anthropic/claude-sonnet-4-6"]
    assert conversation["model_calls"] == 1
    assert conversation["tool_calls"] == 0
    assert conversation["input_tokens"] == 100
    assert conversation["uncached_input_tokens"] == 100
    assert conversation["output_tokens"] == 20
    assert conversation["visible_output_tokens"] == 20
    assert conversation["total_tokens"] == 125
    assert conversation["unattributed_tokens"] == 5
    assert [turn["status"] for turn in snapshot.turns] == ["completed", "aborted"]
    assert all(turn["duration_ms"] is None for turn in snapshot.turns)
    assert snapshot.model_calls[0]["turn_id"] == snapshot.turns[0]["id"]
    assert CANARY not in str(snapshot.to_dict())
    assert USER_ID not in str(snapshot.to_dict())


def test_deduplicates_copies_and_handles_malformed_events(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    analytics(first, complete=False)
    path = analytics(second)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n[]\n")
        handle.write('{"event":"bad","time":-1,"properties":{"secret":"')
        handle.write(CANARY + '"}}\n')

    snapshot = AiderAdapter().collect([("desktop", first), ("laptop", second)])

    assert snapshot.duplicate_conversations == 1
    assert snapshot.malformed_records == 3
    assert snapshot.conversations[0]["source_machine"] == "laptop"
    assert snapshot.conversations[0]["event_count"] == 8
    assert CANARY not in str(snapshot.to_dict())
    assert (
        snapshot.to_dict()
        == AiderAdapter().collect([("desktop", first), ("laptop", second)]).to_dict()
    )

    with pytest.raises(ValueError, match="Missing Aider analytics log"):
        AiderAdapter().collect([("machine", tmp_path / "missing")])


def test_privacy_canary_is_absent_from_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "aider"
    analytics(home)
    snapshot = AiderAdapter().collect([("machine", home)])
    observed: dict[str, Any] = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, int | str]:
            return {
                "run_id": "run-1",
                "received": 1,
                "written": 1,
                "skipped": 0,
            }

    class CapabilitiesResponse(Response):
        def json(self) -> dict[str, int | str]:
            return {"snapshot_schema_min": 1, "snapshot_schema_max": 1}

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get(self, _url: str) -> Response:
            return CapabilitiesResponse()

        def post(self, url: str, **kwargs: Any) -> Response:
            observed.update(url=url, **kwargs)
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr("cli_consumption.sync.httpx.Client", Client)
    send_snapshot(snapshot, "https://collector.test")
    assert CANARY not in json.dumps(observed["json"])
    assert USER_ID not in json.dumps(observed["json"])

    engine = create_database_engine(tmp_path / "usage.sqlite")
    try:
        ingest_snapshot(engine, snapshot)
        rows = {name: read_table(engine, name) for name in TABLES}
        assert CANARY not in json.dumps(rows)
        assert USER_ID not in json.dumps(rows)
        output = tmp_path / "reports"
        paths = export_csv(engine, output)
        dashboard = output / "dashboard.html"
        generate_dashboard(engine, dashboard)
        assert all(CANARY not in path.read_text() for path in paths)
        assert all(USER_ID not in path.read_text() for path in paths)
        assert CANARY not in dashboard.read_text()
        assert USER_ID not in dashboard.read_text()
    finally:
        engine.dispose()
