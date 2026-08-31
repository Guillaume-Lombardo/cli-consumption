from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from cli_consumption.dashboard import _dashboard_calculations_script

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node.js is unavailable",
)


def _payload() -> dict[str, Any]:
    conversations = [
        {
            "key": 1,
            "provider": "copilot",
            "tokenSemantics": "conversation-aggregate",
            "machine": "machine-a",
            "project": "alpha",
            "models": ["aggregate-model"],
            "startedAt": "2026-08-01T00:00:00Z",
            "endedAt": "2026-08-03T00:00:00Z",
        },
        {
            "key": 2,
            "provider": "codex",
            "tokenSemantics": "additive",
            "machine": "machine-a",
            "project": "alpha",
            "models": ["model-a"],
            "startedAt": "2026-08-02T00:00:00Z",
            "endedAt": "2026-08-02T05:00:00Z",
        },
        {
            "key": 3,
            "provider": "cursor",
            "tokenSemantics": "unavailable",
            "machine": "machine-b",
            "project": "beta",
            "models": ["model-b"],
            "startedAt": "2026-08-02T00:00:00Z",
            "endedAt": "2026-08-02T05:00:00Z",
        },
        {
            "key": 4,
            "provider": "codex",
            "tokenSemantics": "additive",
            "machine": "machine-b",
            "project": "beta",
            "models": ["model-b"],
            "startedAt": None,
            "endedAt": None,
        },
        {
            "key": 5,
            "provider": "copilot",
            "tokenSemantics": "conversation-aggregate",
            "machine": "machine-a",
            "project": "alpha",
            "models": ["aggregate-model"],
            "startedAt": "2026-08-04T00:00:00Z",
            "endedAt": "2026-08-04T05:00:00Z",
        },
    ]
    turns = [
        {
            "key": 20,
            "conversationKey": 2,
            "startedAt": "2026-08-02T01:00:00Z",
            "endedAt": "2026-08-02T02:00:00Z",
            "status": "completed",
            "durationMs": 3_600_000,
            "ttftMs": 100,
            "toolCalls": 2,
            "total_tokens": 100,
        },
        {
            "key": 21,
            "conversationKey": 2,
            "startedAt": "2026-08-02T03:00:00Z",
            "endedAt": "2026-08-02T04:00:00Z",
            "status": "aborted",
            "durationMs": 3_600_000,
            "ttftMs": None,
            "toolCalls": 0,
            "total_tokens": 300,
        },
        {
            "key": 22,
            "conversationKey": 2,
            "startedAt": "2026-08-02T04:00:00Z",
            "endedAt": None,
            "status": "in-progress",
            "durationMs": None,
            "ttftMs": 50,
            "toolCalls": 1,
            "total_tokens": 999,
        },
        {
            "key": 40,
            "conversationKey": 4,
            "startedAt": None,
            "endedAt": None,
            "status": "completed",
            "durationMs": None,
            "ttftMs": None,
            "toolCalls": 0,
            "total_tokens": 80,
        },
    ]

    def call(
        conversation: int,
        turn: int | None,
        timestamp: str | None,
        model: str,
        total: int,
        *,
        input_tokens: int = 0,
        cached: int = 0,
        output: int = 0,
        reasoning: int = 0,
    ) -> dict[str, Any]:
        return {
            "conversationKey": conversation,
            "turnKey": turn,
            "timestamp": timestamp,
            "model": model,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "total_tokens": total,
        }

    return {
        "contractVersion": 1,
        "meta": {
            "shareSafe": False,
            "exportWindow": {
                "since": "2026-08-01T00:00:00Z",
                "until": "2026-08-05T00:00:00Z",
            },
        },
        "conversations": conversations,
        "turns": turns,
        "modelCalls": [
            call(1, None, None, "aggregate-model", 200, input_tokens=100, cached=25),
            call(2, 20, "2026-08-02T01:30:00Z", "model-a", 100, input_tokens=80),
            call(
                2,
                21,
                "2026-08-02T03:30:00Z",
                "model-a",
                300,
                input_tokens=200,
                output=100,
                reasoning=20,
            ),
            call(2, None, "2026-08-02T04:30:00Z", "model-a", 50),
            call(2, 22, "2026-08-02T04:45:00Z", "model-a", 999),
            call(3, None, "2026-08-02T02:00:00Z", "model-b", 60),
            call(4, 40, None, "model-b", 80),
            call(5, None, None, "aggregate-model", 70),
        ],
        "toolCalls": [
            {
                "conversationKey": 2,
                "turnKey": 20,
                "timestamp": "2026-08-02T01:45:00Z",
                "sequence": 1,
                "tool": "exec_command",
            },
            {
                "conversationKey": 2,
                "turnKey": None,
                "timestamp": "2026-08-02T01:50:00Z",
                "sequence": 2,
                "tool": "apply_patch",
            },
        ],
        "workItems": [
            {
                "conversationKey": 2,
                "turnKey": 20,
                "startedAtMs": 1_785_635_100_000,
                "durationMs": 1_000,
                "kind": "tool",
                "status": "completed",
            },
            {
                "conversationKey": 2,
                "turnKey": None,
                "startedAtMs": 1_785_635_200_000,
                "durationMs": None,
                "kind": "tool",
                "status": "in-progress",
            },
        ],
        "contextSamples": [
            {
                "conversationKey": 2,
                "turnKey": 20,
                "timestamp": "2026-08-02T01:40:00Z",
                "inputTokens": 50,
                "contextWindowTokens": 100,
            },
            {
                "conversationKey": 2,
                "turnKey": 21,
                "timestamp": "2026-08-02T03:40:00Z",
                "inputTokens": 90,
                "contextWindowTokens": 100,
            },
        ],
        "turnSettings": [
            {
                "conversationKey": 2,
                "turnKey": 20,
                "model": "model-a",
                "effort": "medium",
                "mode": "default",
            }
        ],
        "compactions": [
            {
                "conversationKey": 2,
                "turnKey": 21,
                "timestamp": "2026-08-02T03:50:00Z",
            }
        ],
        "subagents": [
            {
                "conversationKey": 2,
                "childConversationKey": 4,
                "createdAtMs": 1_785_635_300_000,
            }
        ],
        "ingestionRuns": [],
    }


def _run(payload: dict[str, Any], program: str) -> Any:
    harness = f"""
{_dashboard_calculations_script()}
const data={json.dumps(payload, separators=(",", ":"))};
const calculations=createDashboardCalculations(data);
const result=(()=>{{{program}}})();
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node"],
        input=harness,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_percentile_and_metric_comparison_contracts() -> None:
    result = _run(
        _payload(),
        """
return {
  empty:calculations.percentile([],0.5),
  median:calculations.percentile([20,10],0.5),
  filtered:calculations.percentile([10,"invalid",30],0.75),
  higher:calculations.compareMetric(120,100,"higher"),
  lower:calculations.compareMetric(80,100,"lower"),
  neutral:calculations.compareMetric(80,100),
  missing:calculations.compareMetric(80,0,"lower"),
};
""",
    )

    assert result == {
        "empty": None,
        "median": 15,
        "filtered": 25,
        "higher": {"change": 20, "style": "better"},
        "lower": {"change": -20, "style": "better"},
        "neutral": {"change": -20, "style": "neutral"},
        "missing": None,
    }


def test_period_contract_clips_to_export_window_and_avoids_wall_clock() -> None:
    result = _run(
        _payload(),
        """
const serialize=(range)=>range&&({
  start:range.start?.toISOString()||null,
  end:range.end.toISOString(),
  previous:range.previous&&({
    start:range.previous.start.toISOString(),
    end:range.previous.end.toISOString(),
  }),
});
return {
  latest:serialize(calculations.rangeFor("2")),
  custom:serialize(calculations.rangeFor("custom",{
    from:"2026-07-01",to:"2026-09-01",
  })),
};
""",
    )

    assert result == {
        "latest": {
            "start": "2026-08-03T00:00:00.000Z",
            "end": "2026-08-04T23:59:59.999Z",
            "previous": {
                "start": "2026-08-01T00:00:00.000Z",
                "end": "2026-08-02T23:59:59.999Z",
            },
        },
        "custom": {
            "start": "2026-08-01T00:00:00.000Z",
            "end": "2026-08-04T23:59:59.999Z",
            "previous": None,
        },
    }

    empty = _payload()
    empty["meta"] = {"shareSafe": False}
    empty["conversations"] = []
    empty["turns"] = []
    empty["modelCalls"] = []
    empty["toolCalls"] = []
    empty["workItems"] = []
    empty["contextSamples"] = []
    empty["turnSettings"] = []
    empty["compactions"] = []
    empty["subagents"] = []
    empty["ingestionRuns"] = []
    assert _run(empty, 'return calculations.rangeFor("30");') is None


def test_period_contract_does_not_invert_for_timestamp_less_aggregate() -> None:
    payload = _payload()
    payload["meta"]["exportWindow"] = {
        "since": "2026-08-10T00:00:00Z",
        "until": "2026-08-15T00:00:00Z",
    }
    payload["conversations"] = [
        {
            **payload["conversations"][0],
            "startedAt": "2026-08-01T00:00:00Z",
            "endedAt": "2026-08-12T12:00:00Z",
        }
    ]
    payload["turns"] = []
    payload["modelCalls"] = [
        {
            **payload["modelCalls"][0],
            "timestamp": None,
        }
    ]
    payload["toolCalls"] = []
    payload["workItems"] = []
    payload["contextSamples"] = []
    payload["turnSettings"] = []
    payload["compactions"] = []
    payload["subagents"] = []

    result = _run(
        payload,
        """
const range=calculations.rangeFor("30");
const selected=calculations.selectSlice({
  provider:"",machine:"",project:"",model:"",range,
});
return {
  start:range.start.toISOString(),
  end:range.end.toISOString(),
  inverted:range.end<range.start,
  calls:selected.calls.map((call)=>call.total_tokens),
};
""",
    )

    assert result == {
        "start": "2026-08-10T00:00:00.000Z",
        "end": "2026-08-12T23:59:59.999Z",
        "inverted": False,
        "calls": [200],
    }


def test_period_contract_uses_child_activity_for_open_aggregate() -> None:
    payload = _payload()
    payload["meta"]["exportWindow"] = {
        "since": "2026-08-10T00:00:00Z",
        "until": "2026-08-15T00:00:00Z",
    }
    payload["conversations"] = [
        {
            **payload["conversations"][0],
            "startedAt": "2026-08-01T00:00:00Z",
            "endedAt": None,
        }
    ]
    payload["turns"] = [
        {
            **payload["turns"][0],
            "conversationKey": 1,
            "startedAt": "2026-08-12T01:00:00Z",
            "endedAt": "2026-08-12T02:00:00Z",
        }
    ]
    payload["modelCalls"] = [
        {
            **payload["modelCalls"][0],
            "timestamp": None,
        }
    ]
    payload["toolCalls"] = []
    payload["workItems"] = []
    payload["contextSamples"] = []
    payload["turnSettings"] = []
    payload["compactions"] = []
    payload["subagents"] = []

    result = _run(
        payload,
        """
const range=calculations.rangeFor("30");
const selected=calculations.selectSlice({
  provider:"",machine:"",project:"",model:"",range,
});
return {
  start:range.start.toISOString(),
  end:range.end.toISOString(),
  turns:selected.turns.map((turn)=>turn.key),
  calls:selected.calls.map((call)=>call.total_tokens),
};
""",
    )

    assert result == {
        "start": "2026-08-10T00:00:00.000Z",
        "end": "2026-08-12T23:59:59.999Z",
        "turns": [20],
        "calls": [200],
    }


def test_period_contract_handles_150k_timestamped_rows_without_argument_spread() -> (
    None
):
    result = _run(
        _payload(),
        """
data.workItems=Array.from({length:150000},(_,index)=>({
  startedAtMs:Date.parse("2026-08-01T00:00:00Z")+index,
}));
const range=calculations.rangeFor("30");
return {
  start:range.start.toISOString(),
  end:range.end.toISOString(),
};
""",
    )

    assert result == {
        "start": "2026-08-01T00:00:00.000Z",
        "end": "2026-08-04T23:59:59.999Z",
    }


def test_selection_and_aggregation_use_exact_semantics_without_mutation() -> None:
    result = _run(
        _payload(),
        """
const before=JSON.stringify(data);
const range={
  start:new Date("2026-08-02T00:00:00Z"),
  end:new Date("2026-08-02T23:59:59.999Z"),
};
const selected=calculations.selectSlice({
  provider:"",machine:"",project:"",model:"",range,
});
const selectedBefore=JSON.stringify(selected);
const tokenCalls=calculations.semanticTokenCalls(selected);
const metrics=calculations.metrics(selected);
const modelSlice=calculations.selectSlice({
  provider:"",machine:"",project:"",model:"model-a",range,
});
const unbounded=calculations.selectSlice({
  provider:"codex",machine:"machine-b",project:"",model:"",range:null,
});
return {
  conversations:selected.conversations.map((row)=>row.key),
  turns:selected.turns.map((row)=>row.key),
  selectedCalls:selected.calls.map((row)=>row.total_tokens),
  semanticCalls:tokenCalls.map((row)=>row.total_tokens),
  modelTools:modelSlice.tools.map((row)=>row.turnKey),
  modelWork:modelSlice.work.map((row)=>row.turnKey),
  unboundedTurns:unbounded.turns.map((row)=>row.key),
  unboundedCalls:unbounded.calls.map((row)=>row.total_tokens),
  metrics,
  dataUnchanged:before===JSON.stringify(data),
  sliceUnchanged:selectedBefore===JSON.stringify(selected),
};
""",
    )

    assert result["conversations"] == [1, 2, 3]
    assert result["turns"] == [20, 21, 22]
    assert result["selectedCalls"] == [200, 100, 300, 50, 999, 60]
    assert result["semanticCalls"] == [200, 100, 300, 50]
    assert result["modelTools"] == [20]
    assert result["modelWork"] == [20]
    assert result["unboundedTurns"] == [40]
    assert result["unboundedCalls"] == []
    assert result["dataUnchanged"] is True
    assert result["sliceUnchanged"] is True
    assert result["metrics"] == {
        "turns": 3,
        "completed": 1,
        "aborted": 1,
        "tokens": 650,
        "tokensPerTurn": 200,
        "toolsPerTurn": 1,
        "cacheRate": 25 / 380 * 100,
        "durationP50": 3_600_000,
        "durationP75": 3_600_000,
        "durationP95": 3_600_000,
        "ttftP50": 100,
        "ttftP75": 100,
        "ttftP95": 100,
        "tokenP75": 250,
        "tokenP95": 290,
        "toolP75": 1.5,
        "toolP95": 1.9,
        "abortRate": 50,
        "reasoningShare": 20,
        "activeMs": 7_200_000,
        "throughput": 1,
        "pressureP50": 70,
        "pressureP95": 88,
        "activeDays": 1,
    }


def test_cohort_comparison_and_missing_measurements_are_explicit() -> None:
    result = _run(
        _payload(),
        """
const range={
  start:new Date("2026-08-02T00:00:00Z"),
  end:new Date("2026-08-02T23:59:59.999Z"),
};
const selected=calculations.selectSlice({
  provider:"codex",machine:"",project:"",model:"",range,
});
const empty={
  conversations:[],turns:[],calls:[],tools:[],work:[],contexts:[],
  settings:[],compactions:[],subagents:[],
};
return {
  cohorts:calculations.cohortComparison(selected,"project"),
  empty:calculations.metrics(empty),
};
""",
    )

    assert result["cohorts"] == [
        {
            "label": "alpha",
            "turns": 2,
            "durationP50": 3_600_000,
            "tokensP50": 200,
            "toolsPerTurn": 1,
            "pressureP95": 88,
            "abortRate": 50,
        }
    ]
    assert result["empty"] == {
        "turns": 0,
        "completed": 0,
        "aborted": 0,
        "tokens": 0,
        "tokensPerTurn": None,
        "toolsPerTurn": None,
        "cacheRate": 0,
        "durationP50": None,
        "durationP75": None,
        "durationP95": None,
        "ttftP50": None,
        "ttftP75": None,
        "ttftP95": None,
        "tokenP75": None,
        "tokenP95": None,
        "toolP75": None,
        "toolP95": None,
        "abortRate": 0,
        "reasoningShare": 0,
        "activeMs": 0,
        "throughput": 0,
        "pressureP50": None,
        "pressureP95": None,
        "activeDays": 0,
    }
