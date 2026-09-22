from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.contract


def _posted_key_result() -> dict[str, object]:
    return {
        "action": "computer.key",
        "executed": True,
        "delivered": True,
        "input_dispatched": True,
        "completion_verified": False,
        "effect_observed": False,
        "postcondition_verified": False,
        "outcome": "posted_unverified",
        "verification_required": "focus_state",
        "is_error": True,
        "error_code": "KEY_EFFECT_NOT_VERIFIED",
        "key_combo": "CANARY_PRIVATE_SHORTCUT",
        "title": "CANARY_PRIVATE_TITLE",
        "pid": 99123,
    }


def test_helper_returns_safe_error_for_posted_unverified_key():
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    envelope = _computer_result_envelope("computer.key", _posted_key_result())

    assert envelope["ok"] is False
    assert envelope["error_code"] == "KEY_EFFECT_NOT_VERIFIED"
    assert envelope["result"]["executed"] is True
    assert envelope["result"]["completion_verified"] is False
    assert envelope["result"]["verification_required"] == "focus_state"
    assert "CANARY" not in json.dumps(envelope)
    assert "key_combo" not in envelope["result"]


def test_viewer_client_preserves_safe_key_effect_contract(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret")
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {
            "ok": False,
            "error": {"code": "KEY_EFFECT_NOT_VERIFIED", "message": "CANARY_RAW_ERROR"},
            "result": _posted_key_result(),
        },
    )

    result = client.run_computer("computer.key", {})

    assert result["error_code"] == "KEY_EFFECT_NOT_VERIFIED"
    assert result["executed"] is True
    assert result["delivered"] is True
    assert result["completion_verified"] is False
    assert result["verification_required"] == "focus_state"
    assert "focus or effect verification" in result["reason"]
    assert "CANARY" not in json.dumps(result)
    assert "key_combo" not in result
