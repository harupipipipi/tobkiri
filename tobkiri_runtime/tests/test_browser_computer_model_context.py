from __future__ import annotations

import json
import importlib
import sys

import pytest


_MISSING = object()
_ISOLATED_MODULE_NAMES = (
    "ecosystem.defaultspack.blocks.tool.browser_computer",
    "domain.host_bridge.computer_router",
    "ecosystem.defaultspack.domain.host_bridge.computer_router",
)
_ROUTER_PARENT_NAMES = (
    "domain.host_bridge",
    "ecosystem.defaultspack.domain.host_bridge",
)


@pytest.fixture(scope="module")
def browser_computer_block():
    """Keep the block's dual import aliases from leaking into later tests."""
    original_path = list(sys.path)
    original_modules = {
        name: sys.modules.get(name, _MISSING) for name in _ISOLATED_MODULE_NAMES
    }
    original_parent_attrs = {}
    for parent_name in _ROUTER_PARENT_NAMES:
        parent = sys.modules.get(parent_name)
        original_parent_attrs[parent_name] = (
            getattr(parent, "computer_router", _MISSING) if parent is not None else _MISSING
        )

    module = importlib.import_module("ecosystem.defaultspack.blocks.tool.browser_computer")
    yield module

    sys.path[:] = original_path
    for name, original in original_modules.items():
        if original is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    for parent_name, original in original_parent_attrs.items():
        parent = sys.modules.get(parent_name)
        if parent is None:
            continue
        if original is _MISSING:
            if hasattr(parent, "computer_router"):
                delattr(parent, "computer_router")
        else:
            setattr(parent, "computer_router", original)


def test_open_url_model_result_marks_navigation_complete_without_trimming_widget(monkeypatch, browser_computer_block):
    full_result = {
        "action": "browser.open_url",
        "url": "https://example.test/path",
        "opened": True,
        "target_app": "Example Browser",
        "launch": {"large": "ui-and-audit-detail"},
        "edge_haze": {"attempted": True, "started": True},
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run(
        {"action": "browser.open_url", "payload": {"url": "https://example.test/path"}},
        {},
    )

    response = response["data"]
    model_context = json.loads(response["result"])["model_context"]
    assert response["widget"] is full_result
    assert response["widget"]["launch"] == {"large": "ui-and-audit-detail"}
    assert model_context["navigation"] == {
        "open_succeeded": True,
        "last_requested_url": "https://example.test/path",
        "same_url_reopen_needed": False,
    }
    assert model_context["task_transition"] == {
        "completed_phase": "navigate_to_requested_page",
        "next_phase": "observe_opened_page",
        "recommended_actions": ["computer.screenshot"],
        "avoid_actions": ["browser.open_url:same_url"],
    }
    assert "launch" not in json.loads(response["result"])
    assert "edge_haze" not in json.loads(response["result"])


def test_screenshot_model_result_advances_to_interaction_and_reports_binding(monkeypatch, browser_computer_block):
    full_result = {
        "action": "computer.screenshot",
        "model_image": "data:image/png;base64,large",
        "coordinate_contract": {"large": "coordinate-detail"},
        "target_window": {"app": "Example Browser", "pid": 42, "window_id": 7},
        "active_window": {"app": "Example Browser", "pid": 42},
        "selected_window": {"app": "Example Browser", "pid": 42},
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run({"action": "computer.screenshot", "payload": {}}, {})

    response = response["data"]
    model_context = json.loads(response["result"])["model_context"]
    assert response["widget"]["model_image"].startswith("data:image/png")
    assert model_context["target"] == {
        "app": "Example Browser",
        "pid": 42,
        "window_id": 7,
        "window_selected": True,
        "binding_valid": True,
        "window_foreground": True,
    }
    transition = model_context["task_transition"]
    assert transition["completed_phase"] == "observe_current_target"
    assert transition["next_phase"] == "interact_with_visible_target"
    assert transition["recommended_actions"] == ["computer.click", "computer.type", "computer.key"]
    assert "repeat_observation_without_state_change" in transition["avoid_actions"]
    assert "model_image" not in json.loads(response["result"])


def test_screenshot_with_mismatched_selected_window_recommends_rebinding(monkeypatch, browser_computer_block):
    full_result = {
        "action": "computer.screenshot",
        "target_window": {"app": "Example Browser", "pid": 42, "window_id": 7},
        "selected_window": {"app": "Other App", "pid": 99, "window_id": 8},
        "active_window": {"app": "Other App", "pid": 99, "window_id": 8},
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run({"action": "computer.screenshot", "payload": {}}, {})["data"]

    model_context = json.loads(response["result"])["model_context"]
    assert model_context["target"]["binding_valid"] is False
    assert model_context["target"]["window_foreground"] is False
    transition = model_context["task_transition"]
    assert transition["next_phase"] == "bind_foreground_target"
    assert transition["recommended_actions"] == ["computer.select_window", "computer.show_app"]
    assert transition["avoid_actions"] == ["send_input_before_target_binding"]


def test_approval_result_does_not_claim_action_success(monkeypatch, browser_computer_block):
    full_result = {
        "action": "computer.type",
        "approval_required": True,
        "approval_request_id": "request-id",
        "payload": {"text": "literal text"},
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run({"action": "computer.type", "payload": {"text": "literal text"}}, {})

    response = response["data"]
    model_context = json.loads(response["result"])["model_context"]
    assert model_context["outcome"] == {"succeeded": False, "approval_pending": True}
    assert model_context["task_transition"]["next_phase"] == "await_approval"
    assert response["widget"]["approval_request_id"] == "request-id"


def test_failed_action_keeps_safe_diagnostics_in_compact_result(monkeypatch, browser_computer_block):
    full_result = {
        "action": "computer.type",
        "is_error": True,
        "reason": "completion was not verified",
        "diagnostics": {"error_code": "TYPE_COMPLETION_NOT_VERIFIED"},
        "driver_trace": {"large": "ui-and-audit-detail"},
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run({"action": "computer.type", "payload": {"text": "x"}}, {})

    response = response["data"]
    compact = json.loads(response["result"])
    assert response["is_error"] is True
    assert compact["reason"] == "completion was not verified"
    assert compact["diagnostics"] == {"error_code": "TYPE_COMPLETION_NOT_VERIFIED"}
    assert compact["model_context"]["task_transition"]["next_phase"] == "recover_from_failure"
    assert "driver_trace" not in compact
    assert response["widget"]["driver_trace"] == {"large": "ui-and-audit-detail"}


def test_posted_unverified_key_remains_a_model_facing_failure(monkeypatch, browser_computer_block):
    full_result = {
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
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run(
        {"action": "computer.key", "payload": {"key": "return"}}, {}
    )["data"]
    compact = json.loads(response["result"])

    assert compact["model_context"]["outcome"]["succeeded"] is False
    assert compact["model_context"]["task_transition"]["completed_phase"] == "action_failed"
    assert compact["error_code"] == "KEY_EFFECT_NOT_VERIFIED"
    assert compact["executed"] is True
    assert compact["completion_verified"] is False
    assert compact["verification_required"] == "focus_state"


def test_not_ready_probe_tells_model_to_stop_before_write_approval(monkeypatch, browser_computer_block):
    full_result = {
        "action": "computer.probe_text_control",
        "executed": True,
        "probe_completed": True,
        "semantic_control_ready": False,
        "input_dispatched": False,
        "mutation_attempted": False,
        "error_code": "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
        "diagnostics": {"semantic_discovery_stage": "scan_incomplete"},
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run(
        {"action": "computer.probe_text_control", "payload": {"target_control": "browser_address"}},
        {},
    )["data"]
    compact = json.loads(response["result"])

    assert response["is_error"] is False
    assert compact["probe_completed"] is True
    assert compact["semantic_control_ready"] is False
    transition = compact["model_context"]["task_transition"]
    assert transition["next_phase"] == "stop_before_write_approval"
    assert "computer.type" in transition["avoid_actions"]
    assert "computer.screenshot" in transition["avoid_actions"]


def test_ready_probe_is_the_only_probe_state_that_recommends_type(monkeypatch, browser_computer_block):
    full_result = {
        "action": "computer.probe_text_control",
        "executed": True,
        "probe_completed": True,
        "semantic_control_ready": True,
        "input_dispatched": False,
        "mutation_attempted": False,
        "diagnostics": {"semantic_discovery_stage": "ready"},
    }
    monkeypatch.setattr(browser_computer_block, "run_computer_action", lambda *args, **kwargs: full_result)

    response = browser_computer_block.run(
        {"action": "computer.probe_text_control", "payload": {"target_control": "browser_address"}},
        {},
    )["data"]
    transition = json.loads(response["result"])["model_context"]["task_transition"]

    assert transition["next_phase"] == "request_write_approval_and_type"
    assert transition["recommended_actions"] == ["computer.type"]
