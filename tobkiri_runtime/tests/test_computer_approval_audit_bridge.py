"""Tests for approval and audit bridge between BrowserComputerController and ComputerSeatService."""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.models import ActionResult
from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.permissions import (
    requires_approval,
    risk_level,
)
from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import (
    BrowserComputerController,
)


@pytest.fixture
def controller(tmp_path):
    ctrl = BrowserComputerController(artifact_root=tmp_path / "artifacts")
    shared = tmp_path / "user_data" / "shared"
    ctrl._session_path = shared / "browser_sessions.json"
    ctrl._approval_path = shared / "browser_computer_approvals.json"
    ctrl._browser_root = shared / "browser"
    ctrl._profile_root = ctrl._browser_root / "profiles"
    svc = MagicMock()
    svc.click.return_value = asdict(ActionResult(action="click", driver="mock", executed=True))
    svc.type_text.return_value = asdict(ActionResult(action="type_text", driver="mock", executed=True))
    svc.semantic_action.return_value = asdict(ActionResult(action="semantic_action", driver="mock", executed=True))
    svc.pid_event.return_value = asdict(ActionResult(action="pid_event", driver="mock", executed=True))
    svc.observe.return_value = {
        "platform": "darwin",
        "screenshot": {"data_url": "data:image/png;base64,AAAA"},
        "ax_tree": {"root": {"role": "AXWindow", "children": []}},
    }
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    ctrl._computer_seat = svc
    return ctrl


def test_click_requires_approval_without_yolo(controller):
    """click without yolo_mode or a trusted approval token should require approval."""
    result = controller.run("computer.click", {"x": 50, "y": 50})
    assert result.get("requires_approval") is True
    assert "approval_token" not in result


def test_click_executes_with_yolo(controller):
    """click with yolo_mode should execute without approval."""
    result = controller.run("computer.click", {"x": 50, "y": 50, "include_screenshot": False}, yolo_mode=True)
    assert result["executed"] is True


def test_observe_requires_approval_without_yolo(controller):
    """observe can return screenshots, so local execution requires external approval."""
    result = controller.run("computer.observe", {"app": "Notes"})
    assert result.get("requires_approval") is True
    assert "approval_token" not in result
    controller._computer_seat.observe.assert_not_called()


def test_ocr_requires_approval_without_yolo(controller):
    """ocr can disclose screen text, so it requires approval before fallback observation."""
    result = controller.run("computer.ocr", {"app": "Notes"})
    assert result.get("requires_approval") is True
    assert "approval_token" not in result
    controller._computer_seat.observe.assert_not_called()


def test_ax_tree_requires_approval_without_yolo(controller):
    """ax_tree can disclose visible UI content, so it requires approval."""
    result = controller.run("computer.ax_tree", {"app": "Notes"})
    assert result.get("requires_approval") is True
    assert "approval_token" not in result
    controller._computer_seat.observe.assert_not_called()


def test_click_text_requires_approval_without_yolo(controller):
    """click_text is a text-targeted click and must be gated like click/semantic_action."""
    result = controller.run("computer.click_text", {"text": "Save", "approved": True})
    assert result.get("requires_approval") is True
    assert "approval_token" not in result
    controller._computer_seat.semantic_action.assert_not_called()


def test_observe_yolo_bypasses(controller):
    """observe with yolo_mode executes after bypassing approval."""
    result = controller.run("computer.observe", {"app": "Notes"}, yolo_mode=True)
    assert result["action"] == "computer.observe"
    assert result["screenshot"]["data_url"].startswith("data:image/png;base64,")


def test_ax_tree_yolo_bypasses_and_filters_observe_result(controller):
    """ax_tree returns AX data from observe without leaking screenshot unless requested."""
    result = controller.run("computer.ax_tree", {"app": "Notes"}, yolo_mode=True)
    assert result["action"] == "computer.ax_tree"
    assert result["supported"] is True
    assert result["ax_tree"]["root"]["role"] == "AXWindow"
    assert "screenshot" not in result


def test_ocr_yolo_returns_explicit_unsupported_without_ocr_driver(controller):
    """ocr should clearly report unsupported when neither host nor fallback exposes OCR."""
    result = controller.run("computer.ocr", {"app": "Notes", "include_ax_tree": True}, yolo_mode=True)
    assert result["action"] == "computer.ocr"
    assert result["supported"] is False
    assert "No OCR-capable" in result["reason"]
    assert result["ax_tree"]["root"]["role"] == "AXWindow"


def test_semantic_action_requires_approval(controller):
    """semantic_action is high-risk and requires approval."""
    result = controller.run("computer.semantic_action", {"intent": "press Save"})
    assert result.get("requires_approval") is True


def test_semantic_action_yolo_bypasses(controller):
    """semantic_action with yolo_mode bypasses approval."""
    result = controller.run("computer.semantic_action", {"intent": "press Save"}, yolo_mode=True)
    assert result["action"] == "computer.semantic_action"
    assert result.get("requires_approval") is not True


def test_click_text_yolo_uses_semantic_action_fallback(controller):
    """click_text passes text target aliases through to semantic fallback."""
    result = controller.run(
        "computer.click_text",
        {
            "query": "Save",
            "text_query": "Save",
            "match_text": "Save",
            "role": "button",
            "element_id": "AX-1",
            "confidence_threshold": 0.8,
        },
        yolo_mode=True,
    )

    assert result["action"] == "computer.click_text"
    assert result["underlying_action"] == "computer.semantic_action"
    call = controller._computer_seat.semantic_action.call_args
    assert call.kwargs["intent"] == "click the button matching text: Save"
    assert call.kwargs["element_or_point"] == {
        "id": "AX-1",
        "role": "button",
        "confidence_threshold": 0.8,
        "text": "Save",
        "query": "Save",
        "text_query": "Save",
        "match_text": "Save",
    }


@pytest.mark.parametrize("alias_key", ["text_query", "match_text"])
def test_click_text_yolo_normalizes_aliases_before_swift_host(controller, monkeypatch, alias_key):
    """Swift host only understands text-like canonical fields, so click_text aliases are normalized first."""
    captured = {}

    def fake_swift_action(action, payload):
        captured["action"] = action
        captured["payload"] = payload
        return {"driver": "mac_swift_host", "executed": True}

    monkeypatch.setattr(controller, "_darwin_swift_optional_action_result", fake_swift_action)

    result = controller.run("computer.click_text", {alias_key: "Save"}, yolo_mode=True)

    assert result["action"] == "computer.click_text"
    assert result["executed"] is True
    assert captured["action"] == "computer.click_text"
    assert captured["payload"][alias_key] == "Save"
    assert captured["payload"]["text"] == "Save"
    controller._computer_seat.semantic_action.assert_not_called()


def test_pid_event_requires_approval(controller):
    """pid_event is high-risk and requires approval."""
    result = controller.run("computer.pid_event", {"pid": 123, "sub_action": "click", "x": 10, "y": 10})
    assert result.get("requires_approval") is True


def test_pid_event_yolo_bypasses(controller):
    """pid_event with yolo_mode bypasses approval."""
    result = controller.run("computer.pid_event", {"pid": 123, "sub_action": "click", "x": 10, "y": 10}, yolo_mode=True)
    assert result["action"] == "computer.pid_event"


def test_dry_run_does_not_execute_driver(controller):
    """dry_run should not call any driver method."""
    result = controller.run("computer.click", {"x": 50, "y": 50, "dry_run": True}, yolo_mode=True)
    assert result["dry_run"] is True
    controller._computer_seat.click.assert_not_called()


def test_pid_event_function_routes_through_captured_host_contract(monkeypatch):
    """The standalone function must not verify tokens or call a native service."""
    from ecosystem.rumi_default_tools_pack.functions.computer_pid_event import main

    captured = {}

    def fake_run_host_contract_action(action, payload, **kwargs):
        captured["action"] = action
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {"status": "denied", "requires_approval": True}

    monkeypatch.setattr(main, "run_host_contract_action", fake_run_host_contract_action)
    arguments = {
        "pid": 123,
        "action": "type_text",
        "text": "blocked",
        "approved": True,
        "approval_token": "forged",
    }

    result = main.run(
        {"_tool_server_approved": True, "yolo_mode": True},
        arguments,
    )

    assert result == {"status": "denied", "requires_approval": True}
    assert captured == {
        "action": "computer.pid_event",
        "payload": arguments,
        "kwargs": {"source_function_id": "computer_pid_event"},
    }


def test_pid_event_function_propagates_missing_host_session(monkeypatch):
    """No captured dispatch session means no PID-scoped native execution."""
    from ecosystem.rumi_default_tools_pack.domain.tool import host_contract_adapter
    from ecosystem.rumi_default_tools_pack.functions.computer_pid_event import main

    class EmptyContainer:
        @staticmethod
        def get_or_none(name):
            assert name == "v4_dispatch_session"
            return None

    monkeypatch.setattr(host_contract_adapter, "get_container", EmptyContainer)

    result = main.run({}, {"pid": 123, "action": "click", "approved": True})

    assert result == {
        "status": "unavailable",
        "success": False,
        "error_type": "global_host_contract_unavailable",
        "action": "computer.pid_event",
    }


# --- Permission model tests ---

def test_click_is_high_risk():
    assert risk_level("click") == "high"
    assert requires_approval("click") is True


def test_observe_is_high_risk():
    assert risk_level("observe") == "high"
    assert requires_approval("observe") is True


def test_scroll_is_medium_risk():
    assert risk_level("scroll") == "medium"
    assert requires_approval("scroll") is False


def test_move_is_medium_risk():
    assert risk_level("move") == "medium"
    assert requires_approval("move") is False


def test_drag_is_high_risk():
    assert risk_level("drag") == "high"
    assert requires_approval("drag") is True


def test_semantic_action_is_high_risk():
    assert risk_level("semantic_action") == "high"
    assert requires_approval("semantic_action") is True


def test_semantic_action_function_routes_through_host_contract(monkeypatch):
    """Standalone semantic function must not call ComputerSeatService directly."""
    from tobkiri_runtime.ecosystem.rumi_default_tools_pack.functions.computer_semantic_action import main

    captured = {}

    def fake_run_host_contract_action(action, payload, **kwargs):
        captured["action"] = action
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {"action": action, "requires_approval": True}

    monkeypatch.setattr(main, "run_host_contract_action", fake_run_host_contract_action)

    result = main.run(
        {"conversation_id": "conv-1", "yolo_mode": True, "_tool_server_approved": True},
        {"app": "VictimApp", "intent": "press Delete", "element_id": "AX-DESTRUCTIVE-BUTTON", "approval_token": "forged", "approved": True},
    )

    assert result["requires_approval"] is True
    assert captured["action"] == "computer.semantic_action"
    assert captured["payload"] == {
        "app": "VictimApp",
        "pid": None,
        "window_id": None,
        "intent": "press Delete",
        "element_id": "AX-DESTRUCTIVE-BUTTON",
    }
    assert captured["kwargs"] == {"source_function_id": "computer_semantic_action"}


def test_semantic_function_propagates_host_denial(monkeypatch):
    """A captured Host denial must not fall back to direct computer control."""
    from ecosystem.rumi_default_tools_pack.domain.tool import host_contract_adapter
    from ecosystem.rumi_default_tools_pack.functions.computer_semantic_action import main

    calls = []

    class Session:
        def provider_metadata(self, contract_id):
            return ({"contract_id": contract_id},)

        def invoke(self, contract_id, operation_id, payload, **kwargs):
            calls.append((contract_id, operation_id, payload))
            raise PermissionError("Host approval required")

    class Container:
        def get_or_none(self, name):
            assert name == "v4_dispatch_session"
            return Session()

    monkeypatch.setattr(host_contract_adapter, "get_container", lambda: Container())
    with pytest.raises(PermissionError, match="Host approval required"):
        main.run(
            {"yolo_mode": True, "_tool_server_approved": True},
            {"intent": "press Save", "point": [0.2, 0.3],
             "approval_token": "untrusted"},
        )
    assert calls == [(
        "rumi.action.desktop.host.v1", "desktop.accessibility.action",
        {"app": None, "pid": None, "window_id": None,
         "intent": "press Save", "point": [0.2, 0.3]},
    )]
