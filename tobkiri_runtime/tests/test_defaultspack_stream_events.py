from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.stream.events import run_event, to_legacy_chat_stream_event  # noqa: E402


def test_content_delta_maps_to_legacy_delta():
    event = run_event(
        "content_delta",
        run_id="run_1",
        conversation_id="conv_1",
        seq=1,
        data={"delta": "hello"},
    )

    assert to_legacy_chat_stream_event(event) == {
        "type": "delta",
        "delta": "hello",
        "run_id": "run_1",
        "conversation_id": "conv_1",
        "seq": 1,
    }


def test_browser_state_snapshot_maps_to_legacy_event():
    event = run_event(
        "browser_state_snapshot",
        run_id="run_1",
        conversation_id="conv_1",
        seq=7,
        data={
            "snapshot": {"active_window": {"title": "Example"}},
            "active_window": {"title": "Example"},
        },
        tool_call_id="call_1",
        state_revision=7,
    )

    legacy = to_legacy_chat_stream_event(event)
    assert legacy["type"] == "browser_state_snapshot"
    assert legacy["tool_call_id"] == "call_1"
    assert legacy["state_revision"] == 7
    assert legacy["snapshot"] == {"active_window": {"title": "Example"}}
    assert legacy["run_id"] == "run_1"
    assert legacy["conversation_id"] == "conv_1"
    assert legacy["seq"] == 7


def test_tool_display_fields_survive_legacy_mapping():
    event = run_event(
        "tool_call_started",
        run_id="run_1",
        conversation_id="conv_1",
        seq=2,
        data={
            "tool_name": "browser_computer",
            "tool_call_id": "call_1",
            "display_text": "画面を確認しています",
            "status": "running",
            "group": {"id": "browser", "label": "ブラウザ"},
        },
    )

    legacy = to_legacy_chat_stream_event(event)
    assert legacy["type"] == "tool_call_started"
    assert legacy["display_text"] == "画面を確認しています"
    assert legacy["status"] == "running"
    assert legacy["group"] == {"id": "browser", "label": "ブラウザ"}
