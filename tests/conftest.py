from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def rollout_factory():
    def create(
        codex_home: Path,
        conversation_id: str = "conversation-1",
        *,
        extra_event: bool = False,
    ) -> Path:
        events: list[dict[str, Any]] = [
            {
                "timestamp": "2026-08-25T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": conversation_id,
                    "source": "cli",
                    "cwd": "/srv/work/acme/service",
                    "git": {"repository_url": "ssh://git.example/acme/service.git"},
                },
            },
            {
                "timestamp": "2026-08-25T10:00:00.250Z",
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {
                        "model": "gpt-5.6",
                        "reasoning_effort": "medium",
                        "collaboration_mode": {"mode": "default", "settings": {}},
                        "service_tier": "priority",
                    },
                },
            },
            {
                "timestamp": "2026-08-25T10:00:00.500Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "model": "gpt-5.6",
                    "effort": "high",
                    "collaboration_mode": {"mode": "default", "settings": {}},
                },
            },
            {
                "timestamp": "2026-08-25T10:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn-1",
                    "model_context_window": 1000,
                },
            },
            {
                "timestamp": "2026-08-25T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "model_context_window": 1000,
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 40,
                            "cache_write_input_tokens": 10,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                            "total_tokens": 120,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-08-25T10:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec",
                    "input": "await tools.exec_command({cmd: 'secret value'})",
                },
            },
            {
                "timestamp": "2026-08-25T10:00:03.500Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": conversation_id,
                    "turn_id": "turn-1",
                    "started_at_ms": 1000,
                    "completed_at_ms": 2000,
                    "item": {
                        "type": "CommandExecution",
                        "status": "completed",
                        "exit_code": 0,
                        "command": "secret value",
                        "stdout": "secret value",
                    },
                },
            },
            {
                "timestamp": "2026-08-25T10:00:04Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            },
        ]
        if extra_event:
            events.append(
                {
                    "timestamp": "2026-08-25T10:00:05Z",
                    "type": "compacted",
                    "payload": {},
                }
            )
        path = codex_home / "sessions" / f"{conversation_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        return path

    return create
