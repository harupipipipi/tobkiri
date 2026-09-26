"""Focused contract checks for the staged voice speech protocol."""

import importlib.util
import json
from pathlib import Path

MODULE = (
    Path(__file__).resolve().parents[1] / "ecosystem/tobkiri_voice_agent_pack/voice/protocol.py"
)
spec = importlib.util.spec_from_file_location("tobkiri_voice_protocol", MODULE)
assert spec is not None and spec.loader is not None
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


def test_packed_directives_respect_wait_and_turn_boundary() -> None:
    plan = protocol.speech_plan("WAIT: 3 SAY: 少し待ちました ASK: どうしましたか？")
    assert plan == {
        "segments": [
            {"delay": 3.0, "text": "少し待ちました"},
            {"delay": 0.0, "text": "どうしましたか？"},
        ],
        "continue": False,
        "finish": False,
    }


def test_unbounded_delays_and_segments_are_capped() -> None:
    raw = "WAIT: 900 " + " ".join(f"SAY: {i}" for i in range(20)) + " FINISH"
    plan = protocol.speech_plan(raw)
    assert len(plan["segments"]) == 6
    assert plan["segments"][0] == {"delay": 120.0, "text": "0"}
    assert plan["finish"] is True


def test_duplicate_ask_is_not_spoken_twice() -> None:
    plan = protocol.speech_plan("SAY: どうしましたか？ ASK: どうしましたか？")
    assert plan["segments"] == [{"delay": 0.0, "text": "どうしましたか？"}]


def test_voice_coordinator_reuses_active_model_and_existing_subagent_tool() -> None:
    coordinator = json.loads(MODULE.with_name("coordinator.json").read_text())
    assert coordinator["model_source"] == "active_conversation"
    assert coordinator["decision_model_source"] == "optional_independent_setting"
    assert coordinator["tools"] == [
        {
            "id": "subagent",
            "mode": "primary_for_substantial_tasks",
            "authority": "existing_tobkiri_tool_policy",
        }
    ]
