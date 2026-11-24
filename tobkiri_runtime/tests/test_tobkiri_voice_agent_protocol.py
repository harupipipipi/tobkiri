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


def test_voice_pack_declares_microphone_approval_without_starting_capture() -> None:
    pack_dir = MODULE.parent.parent
    catalog = json.loads((pack_dir.parent.parent / "schemas/pack_v4_catalog.v1.json").read_text())
    records = [
        pack for pack in catalog["packs"] if pack["pack_id"] == "tobkiri_voice_agent_pack"
    ]
    extension = json.loads((pack_dir / "frontend_extensions/voice_agent.ui.json").read_text())

    if records:
        record = records[0]
        assert record["capabilities"] == ["host.microphone.capture"]
        assert record["approval_policy"] == "capability_gated"
        assert record["execution_boundary"] == "declarative_only"
        assert record["provided_contracts"] == []
    else:
        coordinator = json.loads(MODULE.with_name("coordinator.json").read_text())
        assert coordinator["status"] == "staged_unwired"
    assert "まだ開始できません" in extension["sidebar_items"][0]["panel"]["notes"][0]
    assert (
        extension["sidebar_items"][0]["panel"]["actions"][0]["id"] == "voice.microphone.permissions"
    )


def test_mixed_scheduled_and_spoken_segments_share_one_total_bound() -> None:
    """Separate directive types cannot each allocate a full playback budget."""
    source = " ".join(["SAY: spoken"] * 6 + ["SCHEDULE: 1: scheduled"] * 6)
    assert len(protocol.speech_plan(source)["segments"]) == 6


def test_invalid_and_negative_delays_do_not_become_positive_waits() -> None:
    """Only a complete nonnegative duration may delay playback."""
    plan = protocol.speech_plan("WAIT: -5 SAY: first SCHEDULE: garbage5: second")
    assert [item["delay"] for item in plan["segments"]] == [0.0, 0.0]


def test_voice_directive_input_has_a_fixed_size_limit() -> None:
    """Oversized provider output is rejected before parsing playback data."""
    import pytest

    with pytest.raises(ValueError, match="too large"):
        protocol.speech_plan("x" * 16_385)


def test_voice_directive_size_limit_applies_to_utf8_bytes() -> None:
    """Multibyte speech cannot exceed the same source payload budget."""
    import pytest

    with pytest.raises(ValueError, match="too large"):
        protocol.speech_plan("声" * 5500)
