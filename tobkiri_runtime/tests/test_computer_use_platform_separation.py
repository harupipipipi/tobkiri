from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_platform_adapters_register_os_specific_drivers() -> None:
    from ecosystem.rumi_default_tools_pack.domain.computer.platform_adapters import (
        adapter_for_sys_platform,
    )
    from ecosystem.rumi_default_tools_pack.domain.computer.registry import DriverRegistry

    mac = DriverRegistry()
    adapter_for_sys_platform("darwin").register_drivers(mac)
    assert "mac_swift_host" in mac.all_drivers
    assert "windows_uia" not in mac.all_drivers
    assert "linux_visible" not in mac.all_drivers

    windows = DriverRegistry()
    adapter_for_sys_platform("win32").register_drivers(windows)
    assert "windows_uia" in windows.all_drivers
    assert "windows_postmessage" in windows.all_drivers
    assert "mac_swift_host" not in windows.all_drivers

    linux = DriverRegistry()
    adapter_for_sys_platform("linux").register_drivers(linux)
    assert "linux_x11_virtual" in linux.all_drivers
    assert "linux_visible" in linux.all_drivers
    assert "windows_uia" not in linux.all_drivers


def test_registry_has_linux_driver_order(monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.computer.drivers.linux_visible import (
        LinuxVisibleDesktopDriver,
    )
    from ecosystem.rumi_default_tools_pack.domain.computer.drivers.linux_x11_virtual import (
        LinuxX11VirtualDriver,
    )
    from ecosystem.rumi_default_tools_pack.domain.computer.drivers.local_visible import (
        LocalVisibleDesktopDriver,
    )
    from ecosystem.rumi_default_tools_pack.domain.computer.registry import DriverRegistry

    registry = DriverRegistry()
    registry.register(LinuxX11VirtualDriver(session=UnavailableSession()))
    registry.register(LinuxVisibleDesktopDriver())
    registry.register(LocalVisibleDesktopDriver())

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.drivers.linux_visible.sys.platform",
        "linux",
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.linux.xdotool.desktop_session_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.linux.xdotool.command_available",
        lambda name: name == "xdotool",
    )

    assert [driver.name for driver in registry.get_driver_chain("linux")] == [
        "linux_visible",
        "local_visible",
    ]


class UnavailableSession:
    def is_available(self) -> bool:
        return False


def test_mac_swift_host_driver_delegates_actions(monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.computer.drivers import mac_swift_host
    from ecosystem.rumi_default_tools_pack.domain.computer.drivers.mac_swift_host import (
        MacSwiftHostDriver,
    )
    from ecosystem.rumi_default_tools_pack.domain.computer.models import ComputerTarget

    calls: list[tuple[str, dict[str, object]]] = []

    class FakeHost:
        def available(self) -> bool:
            return True

        def run(self, action, args):
            calls.append((action, dict(args)))
            return {"action": action, "executed": True, "driver": "mac_swift_host"}

    monkeypatch.setattr(mac_swift_host.sys, "platform", "darwin")
    driver = MacSwiftHostDriver(host=FakeHost())

    result = driver.click(ComputerTarget(app="TextEdit", window_id=7), x=10, y=20)

    assert driver.is_available() is True
    assert result.executed is True
    assert result.driver == "mac_swift_host"
    assert calls == [
        (
            "computer.click",
            {"coordinate_space": "window", "app": "TextEdit", "window_id": 7, "x": 10, "y": 20, "button": "left"},
        )
    ]


def test_linux_visible_driver_delegates_to_linux_helpers(monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.computer.drivers import linux_visible
    from ecosystem.rumi_default_tools_pack.domain.computer.drivers.linux_visible import (
        LinuxVisibleDesktopDriver,
    )
    from ecosystem.rumi_default_tools_pack.domain.computer.models import ComputerTarget

    monkeypatch.setattr(linux_visible.sys, "platform", "linux")
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.linux.xdotool.desktop_session_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.linux.xdotool.command_available",
        lambda name: name == "xdotool",
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.linux.xdotool.click",
        lambda x, y, button="left": (x, y, button) == (11, 22, "right"),
    )

    driver = LinuxVisibleDesktopDriver()
    result = driver.click(ComputerTarget(app="gedit"), x=11, y=22, button="right")

    assert driver.is_available() is True
    assert result.executed is True
    assert result.driver == "linux_visible"
    assert result.uses_physical_input is True


def test_linux_window_ids_accept_hex_and_decimal(monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.computer.linux import xdotool
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    window = {"app": "gedit", "title": "Notes", "x": 1, "y": 2, "width": 800, "height": 600, "window_id": "0x2a"}
    normalized = BrowserComputerController._normalize_window_record(window)

    assert normalized is not None
    assert normalized["window_id"] == 42
    assert BrowserComputerController._window_records_match({"window_id": "0x2a"}, {"window_id": 42}) is True

    monkeypatch.setattr(xdotool, "list_windows", lambda: [window])
    assert xdotool.find_window(window_id=42) == window


def test_selected_window_id_mismatch_does_not_match_same_app() -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    active_dialog = {"app": "Google Chrome", "title": "Blocking Dialog", "window_id": 10474}
    selected_page = {"app": "Google Chrome", "title": "Search Page", "window_id": 9480}

    assert BrowserComputerController._window_records_match(active_dialog, selected_page) is False


def test_physical_click_inside_active_same_app_dialog_is_allowed(tmp_path, monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    active_dialog = {
        "app": "Google Chrome",
        "title": "Blocking Dialog",
        "window_id": 10474,
        "x": 940,
        "y": 118,
        "width": 320,
        "height": 204,
    }
    selected_page = {
        "app": "Google Chrome",
        "title": "Google",
        "window_id": 9480,
        "x": 66,
        "y": 38,
        "width": 1200,
        "height": 797,
    }

    monkeypatch.setattr(controller, "_capture_target", lambda payload: selected_page)
    monkeypatch.setattr(controller, "_active_window", lambda: active_dialog)
    monkeypatch.setattr(controller, "_focus_window", lambda target: None)
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    assert controller._foreground_action_focus_error(
        "computer.click",
        {"app": "Google Chrome", "window_id": 9480, "physical": True, "x": 1205, "y": 157},
    ) is None


def test_type_into_inactive_selected_window_still_refuses_same_app_dialog(tmp_path, monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    active_dialog = {
        "app": "Google Chrome",
        "title": "Blocking Dialog",
        "window_id": 10474,
        "x": 940,
        "y": 118,
        "width": 320,
        "height": 204,
    }
    selected_page = {
        "app": "Google Chrome",
        "title": "Google",
        "window_id": 9480,
        "x": 66,
        "y": 38,
        "width": 1200,
        "height": 797,
    }

    monkeypatch.setattr(controller, "_capture_target", lambda payload: selected_page)
    monkeypatch.setattr(controller, "_active_window", lambda: active_dialog)
    monkeypatch.setattr(controller, "_focus_window", lambda target: None)
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    result = controller._foreground_action_focus_error(
        "computer.type",
        {"app": "Google Chrome", "window_id": 9480, "text": "hello"},
    )

    assert result is not None
    assert result["is_error"] is True
    assert result["recovery"]["kind"] == "focus_required"


def test_app_only_key_allows_same_app_untitled_browser_chrome_surface(tmp_path, monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    active_chrome_surface = {
        "app": "Vivaldi",
        "pid": 4242,
        "title": "",
        "window_id": 139,
        "x": 0,
        "y": 37,
        "width": 1470,
        "height": 44,
    }
    selected_page = {
        "app": "Vivaldi",
        "pid": 4242,
        "title": "Me at the zoo - YouTube - Vivaldi",
        "window_id": 134,
        "x": 0,
        "y": 37,
        "width": 1470,
        "height": 919,
    }

    monkeypatch.setattr(controller, "_capture_target", lambda payload: selected_page)
    monkeypatch.setattr(controller, "_active_window", lambda: active_chrome_surface)
    monkeypatch.setattr(controller, "_focus_window", lambda target: None)
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    assert controller._foreground_action_focus_error(
        "computer.key",
        {"app": "Vivaldi", "key": "k"},
    ) is None

    result = controller._foreground_action_focus_error(
        "computer.key",
        {"app": "Vivaldi", "title": "Me at the zoo", "key": "k"},
    )
    assert result is not None
    assert result["recovery"]["kind"] == "focus_required"


def test_app_only_physical_click_allows_same_app_untitled_browser_chrome_surface(tmp_path, monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    active_chrome_surface = {
        "app": "Vivaldi",
        "pid": 4242,
        "title": "",
        "window_id": 139,
        "x": 0,
        "y": 37,
        "width": 1470,
        "height": 44,
    }
    selected_page = {
        "app": "Vivaldi",
        "pid": 4242,
        "title": "Me at the zoo - YouTube - Vivaldi",
        "window_id": 134,
        "x": 0,
        "y": 37,
        "width": 1470,
        "height": 919,
    }

    monkeypatch.setattr(controller, "_capture_target", lambda payload: selected_page)
    monkeypatch.setattr(controller, "_active_window", lambda: active_chrome_surface)
    monkeypatch.setattr(controller, "_focus_window", lambda target: None)
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    assert controller._foreground_action_focus_error(
        "computer.click",
        {"app": "Vivaldi", "physical": True, "x": 724, "y": 661},
    ) is None

    result = controller._foreground_action_focus_error(
        "computer.click",
        {"app": "Vivaldi", "title": "Me at the zoo", "physical": True, "x": 724, "y": 661},
    )
    assert result is not None
    assert result["recovery"]["kind"] == "focus_required"


def test_app_only_type_accepts_same_pid_atlas_translation_chrome_without_refocus(tmp_path, monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    active_popover = {
        "app": "ChatGPT Atlas",
        "pid": 3300,
        "title": "",
        "window_id": 4216,
        "x": 1124,
        "y": 118,
        "width": 346,
        "height": 113,
    }
    selected_page = {
        "app": "ChatGPT Atlas",
        "pid": 3300,
        "title": "Example Domain",
        "window_id": 76,
        "x": 0,
        "y": 38,
        "width": 1470,
        "height": 845,
    }
    focused = []

    monkeypatch.setattr(controller, "_capture_target", lambda payload: selected_page)
    monkeypatch.setattr(controller, "_active_window", lambda: active_popover)
    monkeypatch.setattr(controller, "_focus_window", lambda target: focused.append(target))
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    payload = {"app": "ChatGPT Atlas", "fallback": "foreground", "text": "youtube"}
    assert controller._focus_action_target(payload) is True
    assert focused == []
    assert controller._foreground_action_focus_error("computer.type", payload) is None


def test_atlas_translation_chrome_requires_same_pid(tmp_path, monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    selected_page = {
        "app": "ChatGPT Atlas", "pid": 3300, "title": "Example Domain", "window_id": 76,
        "x": 0, "y": 38, "width": 1470, "height": 845,
    }
    active_popover = {
        "app": "ChatGPT Atlas", "pid": 3399, "title": "", "window_id": 4216,
        "x": 1124, "y": 118, "width": 346, "height": 113,
    }
    monkeypatch.setattr(controller, "_capture_target", lambda payload: selected_page)
    monkeypatch.setattr(controller, "_active_window", lambda: active_popover)
    monkeypatch.setattr(controller, "_focus_window", lambda target: None)
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    result = controller._foreground_action_focus_error(
        "computer.type", {"app": "ChatGPT Atlas", "fallback": "foreground", "text": "youtube"},
    )

    assert result is not None
    assert result["diagnostics"]["failure_stage"] == "foreground_target_verification"
    assert result["diagnostics"]["input_dispatched"] is False


def test_app_only_type_rejects_substantial_same_pid_atlas_content_window(tmp_path, monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    selected_page = {
        "app": "ChatGPT Atlas", "pid": 3300, "title": "Example Domain", "window_id": 76,
        "x": 0, "y": 38, "width": 1470, "height": 845,
    }
    other_content = {
        "app": "ChatGPT Atlas", "pid": 3300, "title": "Other tab", "window_id": 88,
        "x": 20, "y": 58, "width": 1200, "height": 700,
    }
    monkeypatch.setattr(controller, "_capture_target", lambda payload: selected_page)
    monkeypatch.setattr(controller, "_active_window", lambda: other_content)
    monkeypatch.setattr(controller, "_focus_window", lambda target: None)
    monkeypatch.setattr(browser_computer.time, "sleep", lambda seconds: None)

    result = controller._foreground_action_focus_error(
        "computer.type", {"app": "ChatGPT Atlas", "fallback": "foreground", "text": "youtube"},
    )

    assert result is not None
    assert result["recovery"]["kind"] == "focus_required"


def test_controller_capabilities_include_linux_and_mac_swift(monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Linux")
    linux_caps = BrowserComputerController._capabilities()
    assert linux_caps["desktop_actions"] is True
    assert linux_caps["linux_visible_driver"] is True

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    mac_caps = BrowserComputerController._capabilities()
    assert mac_caps["mac_swift_host"] is True
    assert mac_caps["platform_separated_drivers"] is True
