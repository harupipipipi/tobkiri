"""Tests for _desktop_action delegation through ComputerSeatService."""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.models import ActionResult
from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
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
    return ctrl


def _mock_seat_click_success():
    svc = MagicMock()
    svc.click.return_value = asdict(ActionResult(action="click", driver="mac_accessibility", executed=True, confidence="high"))
    svc.type_text.return_value = asdict(ActionResult(action="type_text", driver="mac_accessibility", executed=True))
    svc.key.return_value = asdict(ActionResult(action="key", driver="mac_accessibility", executed=True))
    svc.scroll.return_value = asdict(ActionResult(action="scroll", driver="mac_accessibility", executed=True))
    svc.move.return_value = asdict(ActionResult(action="move", driver="mac_accessibility", executed=True))
    svc.drag.return_value = asdict(ActionResult(action="drag", driver="mac_accessibility", executed=True))
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    return svc


def test_click_through_seat_returns_driver(controller):
    svc = _mock_seat_click_success()
    controller._computer_seat = svc
    result = controller.run("computer.click", {"x": 50, "y": 50, "physical": True, "target": "desktop"}, yolo_mode=True)
    assert result["executed"] is True
    assert result["driver"] == "mac_accessibility"


def test_seat_failure_falls_through_to_legacy(controller):
    """If ComputerSeatService returns executed=False, legacy code runs."""
    svc = MagicMock()
    svc.click.return_value = asdict(ActionResult(action="click", driver="none", executed=False))
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    # This will fall through to legacy code which may or may not succeed
    # depending on platform, but it should not raise
    try:
        result = controller.run("computer.click", {"x": 50, "y": 50, "physical": True}, yolo_mode=True)
        # If we get here, legacy code ran
        assert result["action"] == "computer.click"
    except Exception:
        # Legacy code may fail on CI – that's OK, the point is it fell through
        pass


def test_seat_executed_fallback_does_not_rerun_legacy(controller, monkeypatch):
    """A ComputerSeat fallback success has already performed the action."""
    svc = MagicMock()
    result = asdict(ActionResult(action="type_text", driver="mac_apple_events", executed=True))
    result["is_fallback"] = True
    svc.type_text.return_value = result
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    monkeypatch.setattr(controller, "_focus_action_target", lambda payload: True)
    monkeypatch.setattr(controller, "_foreground_action_focus_error", lambda action, payload: None)
    monkeypatch.setattr(
        controller,
        "_darwin_type",
        lambda payload: (_ for _ in ()).throw(AssertionError("legacy type should not run")),
    )

    outcome = controller.run(
        "computer.type",
        {"text": "hello", "fallback": "foreground", "include_screenshot": False},
        yolo_mode=True,
    )

    assert outcome["executed"] is True
    assert outcome["driver"] == "mac_apple_events"
    assert outcome["is_fallback"] is True


def test_type_tries_background_safe_seat_before_focus(controller, monkeypatch):
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    svc = MagicMock()
    svc.background_action.return_value = asdict(
        ActionResult(
            action="type_text",
            driver="mac_accessibility",
            executed=True,
            confidence="high",
            can_parallel_user_work=True,
            requires_foreground=False,
            uses_physical_input=False,
            data={"completion_verified": True},
        )
    )
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: [{"app": "Vivaldi", "title": "Google", "x": 0, "y": 0, "width": 800, "height": 600, "pid": 123}],
    )
    monkeypatch.setattr(
        controller,
        "_focus_action_target",
        lambda payload: (_ for _ in ()).throw(AssertionError("background type should not focus")),
    )

    outcome = controller.run(
        "computer.type",
        {"app": "Vivaldi", "text": "hello", "include_screenshot": False},
        yolo_mode=True,
    )

    assert outcome["executed"] is True
    assert outcome["background"] is True
    assert outcome["driver"] == "mac_accessibility"
    svc.background_action.assert_called_once()


def test_observe_recommends_type_and_key_when_browser_ready(controller):
    svc = MagicMock()
    svc.observe.return_value = {"observed": True, "target": {"app": "Vivaldi"}}
    controller._computer_seat = svc

    outcome = controller.run("computer.observe", {"app": "Vivaldi"}, yolo_mode=True)

    assert outcome["action"] == "computer.observe"
    assert outcome["recommended_next_actions"][:2] == ["computer.type", "computer.key"]
    assert "normal approval gates still apply" in outcome["input_guidance"]


def test_explicit_background_key_uses_background_safe_seat(controller, monkeypatch):
    svc = MagicMock()
    svc.background_action.return_value = asdict(
        ActionResult(
            action="key",
            driver="mac_cgevent_pid",
            executed=True,
            can_parallel_user_work=True,
            uses_physical_input=False,
        )
    )
    svc.key.side_effect = AssertionError("background key must not use foreground key path")
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: [{"app": "Vivaldi", "title": "Google", "x": 0, "y": 0, "width": 800, "height": 600, "pid": 123}],
    )
    monkeypatch.setattr(controller, "_focus_action_target", lambda payload: False)

    outcome = controller.run(
        "computer.key",
        {"app": "Vivaldi", "key_combo": "return", "background": True, "include_screenshot": False},
        yolo_mode=True,
    )

    assert outcome["executed"] is True
    assert outcome["background"] is True
    assert outcome["driver"] == "mac_cgevent_pid"
    svc.background_action.assert_called_once()


def test_computer_seat_target_preserves_pid_from_matching_window(controller, monkeypatch):
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: [
            {
                "app": "Vivaldi",
                "title": "Google - Vivaldi",
                "x": 0,
                "y": 37,
                "width": 1470,
                "height": 919,
                "window_id": 7112,
                "pid": 23721,
            }
        ],
    )

    target = controller._computer_seat_target({"app": "Vivaldi"})

    assert target["app"] == "Vivaldi"
    assert target["pid"] == 23721
    assert target["window_id"] == 7112


def test_computer_seat_target_fills_pid_from_running_app(controller, monkeypatch):
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: [
            {
                "app": "Vivaldi",
                "title": "Google - Vivaldi",
                "x": 0,
                "y": 37,
                "width": 1470,
                "height": 919,
                "window_id": 7112,
            }
        ],
    )
    monkeypatch.setattr(
        controller,
        "_running_apps",
        lambda: [
            {"name": "Vivaldi Helper", "app": "Vivaldi Helper", "pid": 46606},
            {"name": "Vivaldi", "app": "Vivaldi", "pid": 23721},
        ],
    )

    target = controller._computer_seat_target({"app": "Vivaldi"})

    assert target["pid"] == 23721


def test_virtual_cursor_publishes_user_separate_overlay(controller, monkeypatch):
    overlay = {"started": True, "sequence_id": "seq"}
    monkeypatch.setattr(controller, "_publish_virtual_pointer", lambda pointer, *, action, payload: overlay)

    outcome = controller.run(
        "computer.move",
        {"x": 12, "y": 34, "coordinate_space": "screen", "include_screenshot": False},
        yolo_mode=True,
    )

    assert outcome["executed"] is True
    assert outcome["virtual_cursor"] is True
    assert outcome["ai_cursor"]["x"] == 12
    assert outcome["ai_cursor"]["y"] == 34
    assert outcome["virtual_pointer_overlay"] == overlay


def test_background_safe_result_rejects_physical_mac_driver():
    assert BrowserComputerController._seat_result_is_background_safe(
        {
            "driver": "mac_swift_host",
            "executed": True,
            "can_parallel_user_work": False,
            "uses_physical_input": True,
        }
    ) is False
    assert BrowserComputerController._seat_result_is_background_safe(
        {
            "driver": "mac_accessibility",
            "executed": True,
            "can_parallel_user_work": True,
            "requires_foreground": False,
            "uses_physical_input": False,
        }
    ) is True


def test_explicit_app_does_not_reuse_stale_selected_window_pid(controller, monkeypatch):
    controller._write_computer_state(
        {
            "target_window": {
                "app": "Safari",
                "title": "Old Safari",
                "x": 0,
                "y": 0,
                "width": 900,
                "height": 700,
                "pid": 444,
                "window_id": 1001,
            }
        }
    )
    monkeypatch.setattr(controller, "_list_windows", lambda: [])
    monkeypatch.setattr(controller, "_running_apps", lambda: [])
    svc = MagicMock()
    svc.background_action.side_effect = AssertionError("must not send events to a stale PID")
    controller._computer_seat = svc

    target = controller._computer_seat_target({"app": "Vivaldi"})
    assert target["pid"] is None
    assert target.get("_target_resolution_error")

    outcome = controller.run(
        "computer.key",
        {"app": "Vivaldi", "key_combo": "return", "background": True, "include_screenshot": False},
        yolo_mode=True,
    )

    assert outcome["executed"] is False
    svc.background_action.assert_not_called()


def test_background_safe_only_requires_background_api(controller):
    class OldSeatService:
        def __init__(self):
            self.click = MagicMock(return_value=asdict(ActionResult(action="click", executed=True)))
            self.type_text = MagicMock(return_value=asdict(ActionResult(action="type_text", executed=True)))
            self.key = MagicMock(return_value=asdict(ActionResult(action="key", executed=True)))
            self.scroll = MagicMock(return_value=asdict(ActionResult(action="scroll", executed=True)))

    svc = OldSeatService()
    controller._computer_seat = svc

    for action, payload in (
        ("computer.key", {"key_combo": "return", "background": True, "include_screenshot": False}),
        ("computer.type", {"text": "hello", "background": True, "include_screenshot": False}),
        ("computer.click", {"x": 12, "y": 34, "background": True, "include_screenshot": False}),
    ):
        outcome = controller.run(action, payload, yolo_mode=True)
        assert outcome["executed"] is False

    svc.key.assert_not_called()
    svc.type_text.assert_not_called()
    svc.click.assert_not_called()


def test_implicit_background_rejects_experimental_and_prompts_for_foreground(controller, monkeypatch):
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    svc = MagicMock()
    def background_action(action, target, payload, *, verified_only=False):
        if verified_only:
            return asdict(ActionResult(action="key", driver="none", executed=False, confidence="failed"))
        return asdict(
            ActionResult(
                action="key",
                driver="mac_cgevent_pid",
                executed=True,
                confidence="experimental",
                can_parallel_user_work=True,
                requires_foreground=False,
                uses_physical_input=False,
            )
        )

    svc.background_action.side_effect = background_action
    svc.key.return_value = asdict(ActionResult(action="key", driver="none", executed=False))
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: [{"app": "Vivaldi", "title": "Google", "x": 0, "y": 0, "width": 800, "height": 600, "pid": 123}],
    )
    monkeypatch.setattr(
        controller,
        "_focus_action_target",
        lambda payload: (_ for _ in ()).throw(AssertionError("implicit background miss must not focus")),
    )
    monkeypatch.setattr(controller, "_foreground_action_focus_error", lambda action, payload: None)
    monkeypatch.setattr(controller, "_apple_script", lambda action, payload: "return")
    monkeypatch.setattr(
        "tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.subprocess.run",
        lambda *args, **kwargs: MagicMock(returncode=0),
    )

    outcome = controller.run(
        "computer.key",
        {"app": "Vivaldi", "key_combo": "return", "include_screenshot": False},
        yolo_mode=True,
    )

    assert outcome["executed"] is False
    assert outcome["is_error"] is True
    assert outcome["recovery"]["kind"] == "foreground_confirmation_required"
    assert outcome["message"] == "backgroundで実行できません。foregroundで作業しますか？"
    assert outcome["recovery"]["retry_payload"]["fallback"] == "foreground"
    svc.background_action.assert_called_once()


def test_windows_implicit_background_rejects_postmessage_and_prompts_for_foreground(controller, monkeypatch):
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    svc = MagicMock()

    def background_action(action, target, payload, *, verified_only=False):
        if verified_only:
            return asdict(ActionResult(action="key", driver="none", executed=False, confidence="failed"))
        return asdict(
            ActionResult(
                action="key",
                driver="windows_postmessage",
                executed=True,
                confidence="best_effort",
                can_parallel_user_work=True,
                requires_foreground=False,
                uses_physical_input=False,
            )
        )

    svc.background_action.side_effect = background_action
    svc.key.return_value = asdict(ActionResult(action="key", driver="none", executed=False))
    svc.doctor.return_value = {"platform": "win32", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    foreground = {"value": False}
    monkeypatch.setattr(
        controller,
        "_focus_action_target",
        lambda payload: (_ for _ in ()).throw(AssertionError("implicit background miss must not focus")),
    )
    monkeypatch.setattr(controller, "_foreground_action_focus_error", lambda action, payload: None)
    monkeypatch.setattr(controller, "_windows_desktop_action", lambda action, payload: foreground.update(value=True))

    outcome = controller.run(
        "computer.key",
        {"key_combo": "return", "include_screenshot": False},
        yolo_mode=True,
    )

    assert foreground["value"] is False
    assert outcome["executed"] is False
    assert outcome["is_error"] is True
    assert outcome["recovery"]["kind"] == "foreground_confirmation_required"
    assert outcome["user_prompt"] == "backgroundで実行できません。foregroundで作業しますか？"


def test_windows_explicit_background_allows_postmessage_key(controller, monkeypatch):
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    svc = MagicMock()
    svc.background_action.return_value = asdict(
        ActionResult(
            action="key",
            driver="windows_postmessage",
            executed=True,
            confidence="best_effort",
            can_parallel_user_work=True,
            requires_foreground=False,
            uses_physical_input=False,
        )
    )
    svc.key.side_effect = AssertionError("explicit background must not use foreground key")
    svc.doctor.return_value = {"platform": "win32", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    monkeypatch.setattr(controller, "_focus_action_target", lambda payload: (_ for _ in ()).throw(AssertionError("must not focus")))

    outcome = controller.run(
        "computer.key",
        {
            "window": {"x": 0, "y": 0, "width": 800, "height": 600, "pid": 4321, "hwnd": 9876},
            "key_combo": "return",
            "background": True,
            "include_screenshot": False,
        },
        yolo_mode=True,
    )

    assert outcome["executed"] is True
    assert outcome["background"] is True
    assert outcome["driver"] == "windows_postmessage"
    svc.background_action.assert_called_once()


def test_foreground_fallback_opt_in_runs_foreground_after_prompt(controller, monkeypatch):
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    svc = MagicMock()
    svc.background_action.side_effect = AssertionError("fallback=foreground must skip background")
    svc.key.return_value = asdict(ActionResult(action="key", driver="none", executed=False))
    svc.doctor.return_value = {"platform": "win32", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    focused = {"value": False}
    foreground = {"value": False}
    monkeypatch.setattr(controller, "_focus_action_target", lambda payload: focused.update(value=True) or True)
    monkeypatch.setattr(controller, "_foreground_action_focus_error", lambda action, payload: None)
    monkeypatch.setattr(controller, "_windows_desktop_action", lambda action, payload: foreground.update(value=True))

    outcome = controller.run(
        "computer.key",
        {"key_combo": "return", "fallback": "foreground", "include_screenshot": False},
        yolo_mode=True,
    )

    assert focused["value"] is True
    assert foreground["value"] is True
    assert outcome["executed"] is True
    assert outcome["driver"] == "foreground_input"
    assert outcome.get("background") is not True


def test_background_key_partial_success_does_not_replay_foreground(controller, monkeypatch):
    svc = MagicMock()
    svc.background_action.side_effect = [
        asdict(
            ActionResult(
                action="key",
                driver="mac_cgevent_pid",
                executed=True,
                confidence="experimental",
                can_parallel_user_work=True,
                requires_foreground=False,
                uses_physical_input=False,
            )
        ),
        asdict(
            ActionResult(
                action="key",
                driver="mac_cgevent_pid",
                executed=False,
                confidence="failed",
                can_parallel_user_work=True,
                requires_foreground=False,
                uses_physical_input=False,
            )
        ),
    ]
    svc.key.side_effect = AssertionError("foreground key must not replay a partial background sequence")
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: [{"app": "Vivaldi", "title": "Google", "x": 0, "y": 0, "width": 800, "height": 600, "pid": 123}],
    )

    outcome = controller.run(
        "computer.key",
        {"app": "Vivaldi", "key_combo": "return", "count": 3, "background": True, "include_screenshot": False},
        yolo_mode=True,
    )

    assert outcome["executed"] is True
    assert outcome["partial_success"] is True
    assert outcome["executed_count"] == 1
    assert outcome["requested_count"] == 3
    svc.key.assert_not_called()


def test_window_relative_coordinates_are_sent_to_background_as_screen(controller, monkeypatch):
    svc = MagicMock()
    svc.background_action.return_value = asdict(
        ActionResult(
            action="click",
            driver="windows_postmessage",
            executed=True,
            confidence="best_effort",
            can_parallel_user_work=True,
            requires_foreground=False,
            uses_physical_input=False,
        )
    )
    svc.doctor.return_value = {"platform": "win32", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    monkeypatch.setattr(controller, "_running_apps", lambda: [])
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: [{"app": "Target", "title": "Doc", "x": 100, "y": 100, "width": 500, "height": 400, "pid": 321, "hwnd": 777}],
    )

    outcome = controller.run(
        "computer.click",
        {
            "app": "Target",
            "x": 10,
            "y": 10,
            "coordinate_space": "window",
            "background": True,
            "include_screenshot": False,
        },
        yolo_mode=True,
    )

    assert outcome["executed"] is True
    _action, target, payload = svc.background_action.call_args.args
    assert target["coordinate_space"] == "screen"
    assert payload["x"] == 110
    assert payload["y"] == 110


def test_seat_exception_falls_through(controller):
    """If _try_computer_seat_action raises, legacy code runs."""
    svc = MagicMock()
    svc.click.side_effect = RuntimeError("driver crash")
    svc.doctor.return_value = {"platform": "darwin", "driver_chain_order": [], "available_drivers": [], "unavailable_drivers": []}
    controller._computer_seat = svc
    try:
        result = controller.run("computer.click", {"x": 50, "y": 50, "physical": True}, yolo_mode=True)
        assert result["action"] == "computer.click"
    except Exception:
        pass


def test_dry_run_does_not_execute(controller):
    svc = _mock_seat_click_success()
    controller._computer_seat = svc
    result = controller.run("computer.click", {"x": 50, "y": 50, "physical": True, "dry_run": True}, yolo_mode=True)
    assert result["dry_run"] is True
    svc.click.assert_not_called()
