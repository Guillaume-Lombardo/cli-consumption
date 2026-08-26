from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_consumption.adapters.plandex import PlandexAdapter

CANARY = "SECRET_CANARY_plandex_do_not_export"


def _fixture(home: Path) -> None:
    conversation = home / "orgs" / "org-1" / "plans" / "plan-1" / "conversation"
    conversation.mkdir(parents=True)
    values = [
        {
            "id": "u1",
            "orgId": "org-1",
            "planId": "plan-1",
            "userId": CANARY,
            "role": "user",
            "tokens": 7,
            "num": 1,
            "message": CANARY,
            "createdAt": "2026-08-27T10:00:00Z",
        },
        {
            "id": "a1",
            "orgId": "org-1",
            "planId": "plan-1",
            "userId": CANARY,
            "role": "assistant",
            "tokens": 21,
            "num": 2,
            "message": CANARY,
            "stopped": False,
            "flags": {"secret": CANARY},
            "createdAt": "2026-08-27T10:00:02Z",
        },
    ]
    for value in values:
        (conversation / f"{value['id']}.json").write_text(
            json.dumps(value), encoding="utf-8"
        )
    (conversation / "broken.json").write_text("not-json", encoding="utf-8")


def test_collects_plandex_server_metadata_and_discards_content(tmp_path: Path) -> None:
    home = tmp_path / "plandex-server"
    _fixture(home)
    snapshot = PlandexAdapter().collect([("server", home)])

    assert snapshot.conversations[0]["external_id"] == "plan-1"
    assert snapshot.conversations[0]["total_tokens"] == 21
    assert snapshot.conversations[0]["unattributed_tokens"] == 21
    assert snapshot.malformed_records == 1
    assert CANARY not in json.dumps(snapshot.to_dict())


def test_plandex_deduplicates_and_rejects_missing_source(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _fixture(first)
    _fixture(second)
    snapshot = PlandexAdapter().collect([("a", first), ("b", second)])
    assert snapshot.duplicate_conversations == 1
    with pytest.raises(ValueError, match="Missing Plandex"):
        PlandexAdapter().collect([("x", tmp_path / "missing")])
