from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ecosystem" / "defaultspack"))


def _controller(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._approval_path = tmp_path / "shared" / "browser_computer_approvals.json"
    return controller


def test_screenshot_requires_approval_before_capture_reuse_or_crop(tmp_path, monkeypatch):
    controller = _controller(tmp_path)

    monkeypatch.setattr(
        controller,
        "_capture_or_reuse_screenshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("capture/reuse must wait for approval")),
    )
    monkeypatch.setattr(
        controller,
        "_apply_screenshot_crop",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("crop must wait for approval")),
    )

    result = controller.run(
        "computer.screenshot",
        {"source": "latest", "crop": {"x": 1, "y": 2, "width": 3, "height": 4}},
    )

    assert result["requires_approval"] is True
    assert result["action"] == "computer.screenshot"
    assert result["payload"]["source"] == "latest"
    assert result["payload"]["crop"]["width"] == 3


def test_yolo_string_false_does_not_bypass_screenshot_approval(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    monkeypatch.setattr(
        controller,
        "_capture_or_reuse_screenshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("string false is not yolo")),
    )

    result = controller.run("computer.screenshot", {}, yolo_mode="false")

    assert result["requires_approval"] is True


def test_user_requested_computer_use_does_not_bypass_local_executor_approval(tmp_path, monkeypatch):
    from domain.tool import executor as executor_module

    ToolExecutor = executor_module.ToolExecutor
    monkeypatch.setattr(executor_module, "policy_from_context", lambda context: context.get("profile_policy", {}))
    monkeypatch.setenv("RUMI_COMPUTER_HOST_INTERNAL", "1")

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.BrowserComputerController._capture_action_result_screenshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("click must not execute before approval")),
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.BrowserComputerController._window_at_point",
        lambda self, x, y: None,
    )

    executor = ToolExecutor.__new__(ToolExecutor)
    result = executor._execute_local(
        "browser_computer",
        {"action": "computer.click", "payload": {"x": 10, "y": 20, "coordinate_space": "screen"}},
        {
            "user_requested_computer_use": True,
            "conversation_workspace_dir": str(tmp_path),
            "profile_policy": {"yolo_mode": "false"},
        },
    )

    assert result["is_error"] is False
    assert result["widget"]["error_type"] == "global_host_contract_unavailable"
    assert result["widget"]["status"] == "unavailable"


def test_open_url_approval_payload_includes_target_app(tmp_path):
    controller = _controller(tmp_path)

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "app": "Microsoft Edge", "persistent": False},
    )

    assert result["requires_approval"] is True
    assert result["payload"]["target_app"] == "Microsoft Edge"


def test_open_url_function_context_target_app_reaches_approval_payload(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.functions.browser_computer import main

    def fake_runner(action, payload, context=None, **kwargs):
        del context, kwargs
        return {
            "action": action,
            "requires_approval": True,
            "payload": payload,
        }

    monkeypatch.setattr(main, "_run_computer_action", lambda: fake_runner)
    result = main.run(
        {"conversation_workspace_dir": str(tmp_path), "computer_use_target_app": "Microsoft Edge"},
        {"action": "browser.open_url", "payload": {"url": "https://example.test", "persistent": False}},
    )

    assert result["widget"]["requires_approval"] is True
    assert result["widget"]["payload"]["app"] == "Microsoft Edge"


def test_open_url_target_app_dry_run_reports_targeted_launch_plan(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "target_app": "atlas", "dry_run": True},
    )

    assert result["dry_run"] is True
    assert result["target_app"] == "atlas"
    assert result["launch"]["mode"] == "target_app"
    assert result["launch"]["commands"][0] == ["open", "-b", "com.openai.atlas", "https://example.test"]


def test_edge_haze_stop_failure_does_not_fail_browser_open_url(tmp_path, monkeypatch):
    controller = _controller(tmp_path)

    class FailingHaze:
        _lease_path = tmp_path / "edge_haze.lease.json"
        _sequence_id = "seq-test"

        @classmethod
        def from_pack_root(cls, pack_root):
            return cls()

        def start(self, *, action, payload):
            return True

        def stop(self):
            raise OSError("No space left on device")

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze.ComputerUseEdgeHazeManager",
        FailingHaze,
    )
    monkeypatch.setattr(controller, "_consume_approval", lambda *args, **kwargs: True)
    monkeypatch.setattr(controller, "_open_url_result", lambda *args, **kwargs: {"opened": True})

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "target_app": "atlas"},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert "No space left on device" in result["edge_haze"]["stop_error"]


def test_edge_haze_can_be_disabled_for_debug_smoke(tmp_path, monkeypatch):
    controller = _controller(tmp_path)

    monkeypatch.setenv("RUMI_EDGE_HAZE_DISABLED", "1")
    monkeypatch.setattr(controller, "_consume_approval", lambda *args, **kwargs: True)
    monkeypatch.setattr(controller, "_open_url_result", lambda *args, **kwargs: {"opened": True})

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "target_app": "atlas"},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["edge_haze"]["disabled"] is True


def test_darwin_targeted_open_url_reports_open_command_failure(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    commands = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Unable to find application named ChatGPT Atlas",
        )

    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(
        controller,
        "_activate_app_name",
        lambda app_name: (_ for _ in ()).throw(AssertionError("open failure must not activate")),
    )

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "app": "ChatGPT Atlas", "persistent": False},
        yolo_mode=True,
    )

    assert result["opened"] is False
    assert result["is_error"] is True
    assert result["target_app"] == "ChatGPT Atlas"
    assert "Unable to find application named ChatGPT Atlas" in result["reason"]
    assert commands[0][0] == ["open", "-b", "com.openai.atlas", "https://example.test"]
    assert commands[1][0] == ["open", "-a", "ChatGPT Atlas", "https://example.test"]
    assert commands[0][1]["timeout"] == browser_computer._DARWIN_AUTOMATION_TIMEOUT_SECONDS


def test_darwin_targeted_open_url_reports_unavailable_after_open_success(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    commands = []
    monotonic_values = iter([0.0, 3.0, 4.0, 7.0])

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.time, "monotonic", lambda: next(monotonic_values, 7.0))
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(controller, "_active_window_for_app", lambda app_name: None)
    monkeypatch.setattr(controller, "_running_apps", lambda: [])
    monkeypatch.setattr(controller, "_activate_app_name", lambda app_name: False)

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "app": "ChatGPT Atlas", "persistent": False},
        yolo_mode=True,
    )

    assert result["opened"] is False
    assert result["is_error"] is True
    assert "did not become available" in result["reason"]
    assert commands[0] == ["open", "-b", "com.openai.atlas", "https://example.test"]
    assert commands[1] == ["open", "-a", "ChatGPT Atlas", "https://example.test"]


def test_darwin_targeted_open_url_debug_foreground_accepts_atlas_command_success(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    commands = []

    monkeypatch.setenv("RUMI_COMPUTER_USE_DEBUG_FOREGROUND", "1")
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(controller, "_active_window_for_app", lambda app_name: None)
    monkeypatch.setattr(controller, "_running_apps", lambda: [])
    monkeypatch.setattr(
        controller,
        "_activate_app_name",
        lambda app_name: (_ for _ in ()).throw(AssertionError("debug foreground open should not block on activation")),
    )

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "app": "ChatGPT Atlas", "persistent": False},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["target_app"] == "ChatGPT Atlas"
    assert result["command_accepted"] is True
    assert result["window_verified"] is False
    assert result["launch_command"] == ["open", "-b", "com.openai.atlas", "https://example.test"]
    assert commands == [["open", "-b", "com.openai.atlas", "https://example.test"]]


def test_darwin_targeted_open_url_requires_usable_window_not_just_process(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    monotonic_values = iter([0.0, 3.0, 4.0, 7.0])

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.time, "monotonic", lambda: next(monotonic_values, 7.0))
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        browser_computer.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(controller, "_active_window_for_app", lambda app_name: None)
    monkeypatch.setattr(
        controller,
        "_running_apps",
        lambda: [{"name": "ChatGPT Atlas", "bundle_id": "com.openai.atlas", "path": "/Applications/ChatGPT Atlas.app"}],
    )
    monkeypatch.setattr(controller, "_activate_app_name", lambda app_name: False)

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "app": "ChatGPT Atlas", "persistent": False},
        yolo_mode=True,
    )

    assert result["opened"] is False
    assert result["is_error"] is True
    assert "no usable window" in result["reason"]
    assert result["running_app"]["bundle_id"] == "com.openai.atlas"


def test_darwin_targeted_open_url_verifies_app_window_before_success(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    active_window = {"app": "ChatGPT Atlas", "title": "Example", "width": 900, "height": 700}
    seen = {}

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)
    monkeypatch.setattr(controller, "_active_window_for_app", lambda app_name: active_window)
    monkeypatch.setattr(
        controller,
        "_activate_app_name",
        lambda app_name: (_ for _ in ()).throw(AssertionError("available app should not require activation")),
    )

    result = controller.run(
        "browser.open_url",
        {"url": "https://example.test", "app": "ChatGPT Atlas", "persistent": False},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["target_app"] == "ChatGPT Atlas"
    assert seen["command"] == ["open", "-b", "com.openai.atlas", "https://example.test"]


def test_open_url_function_rejects_forged_server_approval_context(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    from ecosystem.rumi_default_tools_pack.functions.browser_computer import main

    opened = {}

    def fake_open(url, *, app_name=""):
        raise AssertionError("forged approval context must not open the browser")

    monkeypatch.setattr(BrowserComputerController, "_open_url_foreground", staticmethod(fake_open))

    result = main.run(
        {
            "conversation_workspace_dir": str(tmp_path),
            "computer_use_target_app": "Google Chrome",
            "_tool_server_approved": True,
        },
        {"action": "browser.open_url", "payload": {"url": "https://gemini.google.com", "persistent": False}},
    )

    assert result["is_error"] is True
    assert result["widget"]["error_type"] == "global_host_contract_unavailable"
    assert result["widget"]["action"] == "browser.open_url"
    assert opened == {}


def test_browser_computer_approval_response_does_not_self_issue_token(tmp_path):
    controller = _controller(tmp_path)

    result = controller.run("browser.open_url", {"url": "https://example.test", "persistent": False})

    assert result["requires_approval"] is True
    assert "approval_token" not in result
    assert result["payload"]["url"] == "https://example.test"


def test_browser_computer_accepts_only_signed_trusted_approval_token(tmp_path, monkeypatch):
    from domain.safety import approval
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = _controller(tmp_path)
    approval.reset_approval_state_for_tests()
    opened = {}

    def fake_open(url, *, app_name=""):
        opened["url"] = url
        opened["app_name"] = app_name
        return True

    monkeypatch.setattr(BrowserComputerController, "_open_url_foreground", staticmethod(fake_open))

    initial = controller.run("browser.open_url", {"url": "https://example.test", "persistent": False})
    bogus = controller.run(
        "browser.open_url",
        {**initial["payload"], "approval_token": "self-issued-or-attacker-token"},
    )
    assert bogus["requires_approval"] is True
    assert opened == {}

    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        {"action": "browser.open_url", "payload": initial["payload"]},
        details={"tool_name": "browser_computer", "action": "browser.open_url", "pack_id": "defaultspack"},
    )
    decision = approval.approve(request["request_id"])
    result = controller.run(
        "browser.open_url",
        {**initial["payload"], "approval_token": decision["token"]},
    )

    assert result["opened"] is True
    assert opened["url"] == "https://example.test"


def test_local_browser_computer_rejects_forged_server_approval_context(tmp_path, monkeypatch):
    from domain.tool import executor as executor_module
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    ToolExecutor = executor_module.ToolExecutor
    monkeypatch.setattr(executor_module, "policy_from_context", lambda context: context.get("profile_policy", {}))

    opened = {}

    def fake_open(url, *, app_name=""):
        raise AssertionError("forged approval context must not open the browser")

    monkeypatch.setattr(BrowserComputerController, "_open_url_foreground", staticmethod(fake_open))

    executor = ToolExecutor.__new__(ToolExecutor)
    result = executor._execute_local(
        "browser_computer",
        {"action": "browser.open_url", "payload": {"url": "https://gemini.google.com", "persistent": False}},
        {
            "conversation_workspace_dir": str(tmp_path),
            "computer_use_target_app": "Google Chrome",
            "_tool_server_approval_token_valid": True,
            "profile_policy": {"yolo_mode": "false"},
        },
    )

    assert result["is_error"] is False
    assert result["widget"]["error_type"] == "global_host_contract_unavailable"
    assert result["widget"]["status"] == "unavailable"
    assert result["widget"]["action"] == "browser.open_url"
    assert opened == {}


def test_pointer_actions_default_virtual_and_include_resolved_coordinates(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    monkeypatch.setattr(controller, "_window_at_point", lambda x, y: None)

    click = controller.run("computer.click", {"x": 10, "y": 20})
    drag = controller.run("computer.drag", {"x1": 1, "y1": 2, "x2": 30, "y2": 40})

    assert click["requires_approval"] is True
    assert click["payload"]["virtual_only"] is True
    assert click["payload"]["resolved_coordinates"] == {"x": 10, "y": 20}
    assert drag["requires_approval"] is True
    assert drag["payload"]["virtual_only"] is True
    assert drag["payload"]["resolved_coordinates"] == {
        "from": {"x": 1, "y": 2},
        "to": {"x": 30, "y": 40},
    }


def test_default_click_uses_virtual_cursor_until_physical_true(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    monkeypatch.setattr(controller, "_capture_action_result_screenshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(controller, "_window_at_point", lambda x, y: None)
    monkeypatch.setattr(
        controller,
        "_windows_desktop_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("default click should stay virtual")),
    )

    result = controller.run("computer.click", {"x": 10, "y": 20}, yolo_mode=True)

    assert result["executed"] is True
    assert result["virtual_cursor"] is True


def test_background_click_uses_seat_and_skips_virtual_cursor(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    seat_calls = []

    def fake_seat(action, payload, **kwargs):
        seat_calls.append((action, dict(payload), dict(kwargs)))
        return {
            "action": "click",
            "driver": "windows_postmessage",
            "executed": True,
            "confidence": "best_effort",
            "can_parallel_user_work": True,
            "requires_foreground": False,
            "uses_physical_input": False,
            "data": {
                "hwnd": 123,
                "input_space": "screen",
                "screen": {"x": 10, "y": 20},
                "client": {"x": 5, "y": 8},
            },
            "notes": ["posted"],
        }

    monkeypatch.setattr(controller, "_try_computer_seat_action", fake_seat)
    monkeypatch.setattr(controller, "_set_ai_cursor", lambda payload: (_ for _ in ()).throw(AssertionError("background must not use virtual cursor")))

    result = controller.run("computer.click", {"x": 10, "y": 20, "background": True}, yolo_mode=True)

    assert result["executed"] is True
    assert result["background"] is True
    assert result["driver"] == "windows_postmessage"
    assert result["target"]["client"] == {"x": 5, "y": 8}
    assert seat_calls and seat_calls[0][1]["background"] is True


def test_background_click_passes_hwnd_and_coordinate_space_to_seat(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    captured = {}

    class FakeSeat:
        def background_action(self, action, target, payload, **kwargs):
            captured["target"] = dict(target)
            captured["point"] = dict(payload)
            return {
                "action": action,
                "driver": "windows_postmessage",
                "executed": True,
                "confidence": "best_effort",
                "can_parallel_user_work": True,
                "requires_foreground": False,
                "uses_physical_input": False,
                "data": {
                    "hwnd": target["hwnd"],
                    "input_space": target["coordinate_space"],
                    "screen": {"x": payload["x"], "y": payload["y"]},
                    "client": {"x": 50, "y": 40},
                },
            }

    monkeypatch.setattr(controller, "_get_computer_seat", lambda: FakeSeat())
    monkeypatch.setattr(controller, "_window_at_point", lambda x, y: None)

    result = controller.run(
        "computer.click",
        {
            "x": 150,
            "y": 90,
            "hwnd": 9001,
            "window": {"hwnd": 9001, "x": 100, "y": 50, "width": 400, "height": 300},
            "coordinate_space": "screen",
            "background": True,
        },
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert captured["target"]["hwnd"] == 9001
    assert captured["target"]["coordinate_space"] == "screen"
    assert captured["point"] == {"x": 150, "y": 90, "button": "left"}
    assert result["target"]["input_space"] == "screen"


def test_background_type_does_not_fall_back_to_foreground(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    monkeypatch.setattr(controller, "_try_computer_seat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        controller,
        "_windows_desktop_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("background must not use foreground fallback")),
    )

    result = controller.run("computer.type", {"text": "hello", "background": True}, yolo_mode=True)

    assert result["executed"] is False
    assert result["is_error"] is True
    assert result["background"] is True
    assert result["recovery"]["kind"] == "foreground_fallback_available"


def test_type_without_text_is_rejected_as_invalid_payload(tmp_path):
    controller = _controller(tmp_path)

    result = controller.run(
        "computer.type",
        {"key": "l", "modifiers": ["command"]},
        yolo_mode=True,
    )

    assert result["executed"] is False
    assert result["is_error"] is True
    assert result["recovery"]["kind"] == "invalid_type_payload"
    assert "computer.key" in result["reason"]


def test_windows_open_url_can_target_specific_browser(monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    calls = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(browser_computer.shutil, "which", lambda name: r"C:\Browsers\msedge.exe" if name == "Microsoft Edge" else None)
    monkeypatch.setattr(
        browser_computer.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )

    assert BrowserComputerController._open_url_foreground("https://example.test", app_name="Microsoft Edge") is True
    assert calls[0][0].lower().endswith("msedge.exe")
    assert calls[0][1] == "https://example.test"


def test_windows_virtual_screen_coordinates_are_reported_for_desktop_capture(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        controller,
        "_windows_screenshot",
        lambda path, target=None: {
            "x": -1920,
            "y": 0,
            "width": 3840,
            "height": 1080,
            "screen": "virtual_screen",
            "unit": "display_coordinate",
        },
    )

    result = controller._capture_screenshot(tmp_path / "shot.png", {"target": "desktop"})

    assert result["action_coordinate_system"]["screen"] == "virtual_screen"
    assert result["action_coordinate_system"]["x_range"] == [-1920, 1919]


def test_windows_sendkeys_escapes_literals_and_supports_modifiers():
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    assert BrowserComputerController._windows_sendkeys_escape_text("a+b{c}\n") == "a{+}b{{}c{}}{ENTER}"
    assert BrowserComputerController._windows_send_key("p", ["ctrl", "shift"]) == "^+p"
    assert BrowserComputerController._windows_send_key("ctrl+escape") == "^{ESC}"
    assert BrowserComputerController._windows_send_key("pagedown") == "{PGDN}"
    assert BrowserComputerController._windows_send_key("pageup") == "{PGUP}"
    assert BrowserComputerController._windows_send_key("back") == "{BACKSPACE}"
    assert BrowserComputerController._windows_send_key("back", ["alt"]) == "%{LEFT}"
    assert BrowserComputerController._windows_send_key("alt+back") == "%{LEFT}"


def test_windows_key_action_escapes_powershell_string_literals(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    scripts = []
    monkeypatch.setattr(controller, "_run_powershell", scripts.append)

    controller._windows_desktop_action("computer.key", {"key": "'); Start-Process calc; #"})

    assert len(scripts) == 1
    assert "$key = '{''); START-PROCESS CALC; #}'" in scripts[0]
    assert "$key = '{'); START-PROCESS CALC; #}'" not in scripts[0]
    assert "SendWait($key)" in scripts[0]


def test_windows_drag_steps_and_scrolls_at_point(tmp_path, monkeypatch):
    controller = _controller(tmp_path)
    scripts = []
    monkeypatch.setattr(controller, "_run_powershell", scripts.append)
    monkeypatch.setattr(controller, "_resolve_action_point", lambda payload, **kwargs: ({"x": 45, "y": 55}, None))

    controller._windows_desktop_action("computer.drag", {"x1": 1, "y1": 2, "x2": 30, "y2": 40})
    controller._windows_desktop_action("computer.scroll", {"x": 10, "y": 20, "amount": -2})

    assert "$steps = 12" in scripts[0]
    assert "Start-Sleep -Milliseconds 15" in scripts[0]
    assert "New-Object System.Drawing.Point(45, 55)" in scripts[1]
    assert "mouse_event(0x0800, 0, 0, -240" in scripts[1]


def test_windows_focus_window_uses_foreground_api(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    scripts = []
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(controller, "_run_powershell", scripts.append)

    controller._focus_window({"app": "chrome", "title": "LINE Chat - Google Chrome", "window_id": 1234})

    assert scripts
    assert "ShowWindowAsync($hwnd, 9)" in scripts[0]
    assert "SetForegroundWindow($hwnd)" in scripts[0]
    assert "$hwnd = [IntPtr]1234" in scripts[0]


def test_foreground_type_refuses_when_selected_window_is_not_active(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    target_window = {
        "app": "chrome",
        "title": "LINE Chat - Google Chrome",
        "x": 10,
        "y": 20,
        "width": 900,
        "height": 700,
        "window_id": 200,
    }
    active_window = {
        "app": "Codex",
        "title": "Codex",
        "x": 0,
        "y": 0,
        "width": 900,
        "height": 700,
        "window_id": 100,
    }
    focus_calls = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(controller, "_matching_window", lambda payload: target_window)
    monkeypatch.setattr(controller, "_active_window", lambda: active_window)
    monkeypatch.setattr(controller, "_focus_window", lambda window: focus_calls.append(window))
    monkeypatch.setattr(controller, "_try_computer_seat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        controller,
        "_windows_desktop_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("type must not hit the active Codex window")),
    )

    result = controller.run(
        "computer.type",
        {"text": "hello", "app": "Google Chrome", "title": "LINE Chat"},
        yolo_mode=True,
    )

    assert result["is_error"] is True
    assert result["executed"] is False
    assert result["recovery"]["kind"] == "focus_required"
    assert result["active_window"]["app"] == "Codex"
    assert result["selected_window"]["title"] == "LINE Chat - Google Chrome"
    assert focus_calls


def test_foreground_type_executes_when_selected_window_is_active(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    target_window = {
        "app": "chrome",
        "title": "LINE Chat - Google Chrome",
        "x": 10,
        "y": 20,
        "width": 900,
        "height": 700,
        "window_id": 200,
    }
    desktop_actions = []

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(controller, "_matching_window", lambda payload: target_window)
    monkeypatch.setattr(controller, "_active_window", lambda: target_window)
    monkeypatch.setattr(controller, "_focus_window", lambda window: None)
    monkeypatch.setattr(controller, "_try_computer_seat_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_windows_desktop_action", lambda action, payload: desktop_actions.append((action, payload)))
    monkeypatch.setattr(controller, "_capture_action_result_screenshot", lambda *args, **kwargs: {})

    result = controller.run(
        "computer.type",
        {"text": "hello", "app": "Google Chrome", "title": "LINE Chat"},
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert result.get("is_error") is not True
    assert desktop_actions[0][0] == "computer.type"


def test_windows_window_listing_is_dpi_aware(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path)
    scripts = []
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(controller, "_run_powershell_capture", lambda script: scripts.append(script) or "[]")

    assert controller._windows_windows() == []
    assert scripts
    assert "SetProcessDPIAware" in scripts[0]
