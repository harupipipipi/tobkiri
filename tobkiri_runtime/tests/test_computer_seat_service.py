"""Tests for ComputerSeatService – observe/click/type_text with mock drivers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer import (
    ComputerSeatService,
    DriverRegistry,
)
from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.models import (
    ActionResult,
    ComputerCapabilities,
    ComputerTarget,
    ObserveResult,
)
from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.drivers.base import (
    ComputerDriver,
)


class MockDriver(ComputerDriver):
    def __init__(self, name_: str = "mock", succeed: bool = True):
        self._name = name_
        self._succeed = succeed

    @property
    def name(self) -> str:
        return self._name

    @property
    def platform(self) -> str:
        return "darwin"

    def capabilities(self) -> ComputerCapabilities:
        return ComputerCapabilities(can_semantic_action=True)

    def observe(self, target):
        return ObserveResult(platform="darwin", ax_tree={"mock": True})

    def click(self, target, x=0, y=0, button="left"):
        return ActionResult(action="click", driver=self._name, executed=self._succeed)

    def type_text(self, target, text=""):
        return ActionResult(action="type_text", driver=self._name, executed=self._succeed)

    def key(self, target, key_combo=""):
        return ActionResult(action="key", driver=self._name, executed=self._succeed)

    def scroll(self, target, x=0, y=0, direction="down", clicks=3):
        return ActionResult(action="scroll", driver=self._name, executed=self._succeed)

    def semantic_action(self, target, intent="", element_or_point=None):
        return ActionResult(action="semantic_action", driver=self._name, executed=self._succeed)

    def is_available(self) -> bool:
        return True


class BackgroundTypeDriver(MockDriver):
    def __init__(self, name_: str = "background", *, physical: bool = False):
        super().__init__(name_, succeed=True)
        self._physical = physical
        self.called = False

    def capabilities(self) -> ComputerCapabilities:
        return ComputerCapabilities(
            can_background_type=not self._physical,
            can_parallel_user_work=not self._physical,
            can_foreground_action=self._physical,
        )

    def type_text(self, target, text=""):
        self.called = True
        return ActionResult(
            action="type_text",
            driver=self._name,
            executed=True,
            can_parallel_user_work=not self._physical,
            uses_physical_input=self._physical,
        )


class AxCandidateDriver(BackgroundTypeDriver):
    def __init__(self):
        super().__init__("mac_accessibility", physical=False)

    def safe_type_candidate_diagnostics(self, target):
        return {
            "pyobjc_ax_import_available": True,
            "ax_process_trusted": True,
            "ax_set_value_unsafe_app": False,
            "target_app_present": bool(target.app),
            "target_bundle_present": bool(target.bundle_id),
            "target_pid_present": target.pid is not None,
            "target_window_present": target.window_id is not None or bool(target.window_title),
            "attempted": False,
            "result_code": "AX_ELIGIBLE",
        }

class BackgroundKeyDriver(MockDriver):
    def __init__(self, name_: str):
        super().__init__(name_, succeed=True)
        self.called = False

    def capabilities(self) -> ComputerCapabilities:
        return ComputerCapabilities(
            can_background_key=True,
            can_parallel_user_work=True,
            can_foreground_action=False,
        )

    def key(self, target, key_combo=""):
        self.called = True
        return ActionResult(
            action="key",
            driver=self._name,
            executed=True,
            confidence="experimental" if self._name == "mac_cgevent_pid" else "best_effort",
            can_parallel_user_work=True,
            requires_foreground=False,
            uses_physical_input=False,
        )


class PidEventDriver(MockDriver):
    def __init__(self, name_: str, *, unsafe_foreground: bool = False):
        super().__init__(name_, succeed=True)
        self.unsafe_foreground = unsafe_foreground
        self.calls: list[tuple[str, dict[str, object]]] = []

    def capabilities(self) -> ComputerCapabilities:
        return ComputerCapabilities(
            can_background_click=True,
            can_background_type=True,
            can_background_key=True,
            can_background_scroll=True,
            can_pid_event=True,
            can_foreground_action=self.unsafe_foreground,
            can_parallel_user_work=not self.unsafe_foreground,
        )

    def _result(self, action: str, **data: object) -> ActionResult:
        self.calls.append((action, data))
        return ActionResult(
            action=action,
            driver=self._name,
            executed=True,
            can_parallel_user_work=not self.unsafe_foreground,
            requires_foreground=self.unsafe_foreground,
            uses_physical_input=self.unsafe_foreground,
            data=data,
        )

    def click(self, target, x=0, y=0, button="left"):
        return self._result("click", x=x, y=y, button=button, pid=target.pid)

    def type_text(self, target, text=""):
        return self._result("type_text", text=text, pid=target.pid)

    def key(self, target, key_combo=""):
        return self._result("key", key_combo=key_combo, pid=target.pid)

    def scroll(self, target, x=0, y=0, direction="down", clicks=3):
        return self._result("scroll", x=x, y=y, direction=direction, clicks=clicks, pid=target.pid)


class SemanticProbeDriver(MockDriver):
    def __init__(self, name_: str, *, ready: bool = False):
        super().__init__(name_, succeed=True)
        self.ready = ready
        self.calls: list[tuple[ComputerTarget, dict[str, object]]] = []

    def probe_text_control(self, target, selector=None):
        self.calls.append((target, dict(selector or {})))
        return ActionResult(
            action="probe_text_control",
            driver=self._name,
            executed=True,
            confidence="verified",
            can_parallel_user_work=True,
            requires_foreground=False,
            uses_physical_input=False,
            data={
                "probe_completed": True,
                "semantic_control_ready": self.ready,
                "input_dispatched": False,
                "mutation_attempted": False,
            },
        )


def _make_service(drivers):
    reg = DriverRegistry()
    for d in drivers:
        reg.register(d)
    svc = ComputerSeatService(reg)
    svc._platform = "test"  # Use generic platform so all registered drivers are in chain
    return svc


def test_observe_success():
    svc = _make_service([MockDriver("mock1")])
    result = svc.observe({"app": "Test"})
    assert result["ax_tree"] == {"mock": True}


def test_click_success_not_fallback():
    svc = _make_service([MockDriver("mock1")])
    result = svc.click({"app": "Test"}, x=10, y=20)
    assert result["executed"] is True
    assert result["is_fallback"] is False


def test_click_fallback_to_next_driver():
    d1 = MockDriver("fail_driver", succeed=False)
    d2 = MockDriver("ok_driver", succeed=True)
    svc = _make_service([d1, d2])
    result = svc.click({"app": "Test"})
    # d1 returns executed=False, so chain tries d2
    assert result["executed"] is True
    assert result["driver"] == "ok_driver"


def test_type_text_success():
    svc = _make_service([MockDriver("mock1")])
    result = svc.type_text({"app": "Test"}, text="hello")
    assert result["executed"] is True


def test_semantic_probe_uses_only_named_swift_driver_and_ready_false_is_valid():
    native = SemanticProbeDriver("mac_swift_host", ready=False)
    fallback = SemanticProbeDriver("fallback_probe", ready=True)
    svc = _make_service([fallback, native])
    selector = {"roles": ["AXTextField", "AXComboBox"]}

    result = svc.probe_text_control(
        {"app": "ChatGPT Atlas", "pid": 42, "window_id": 99},
        selector=selector,
    )

    assert result["executed"] is True
    assert result["driver"] == "mac_swift_host"
    assert result["data"]["probe_completed"] is True
    assert result["data"]["semantic_control_ready"] is False
    assert result["data"]["input_dispatched"] is False
    assert result["data"]["mutation_attempted"] is False
    assert len(native.calls) == 1
    assert native.calls[0][1] == selector
    assert fallback.calls == []


def test_semantic_probe_never_falls_back_when_swift_driver_is_missing():
    fallback = SemanticProbeDriver("fallback_probe", ready=True)
    svc = _make_service([fallback])

    result = svc.probe_text_control(
        {"app": "ChatGPT Atlas", "pid": 42, "window_id": 99},
        selector={"roles": ["AXTextField", "AXComboBox"]},
    )

    assert result["executed"] is False
    assert result["driver"] == "none"
    assert result["data"]["error_code"] == "TYPE_SEMANTIC_PROBE_UNAVAILABLE"
    assert result["data"]["input_dispatched"] is False
    assert result["data"]["mutation_attempted"] is False
    assert fallback.calls == []


def test_background_action_skips_foreground_only_driver():
    foreground = BackgroundTypeDriver("mac_swift_host", physical=True)
    background = BackgroundTypeDriver("mac_cgevent_pid", physical=False)
    svc = _make_service([foreground, background])

    result = svc.background_action("type_text", {"app": "Test"}, {"text": "hello"})

    assert result["executed"] is True
    assert result["driver"] == "mac_cgevent_pid"
    assert foreground.called is False
    assert background.called is True


def test_ax_candidate_posted_unverified_stops_replay_and_reports_safe_fixed_diagnostics():
    driver = AxCandidateDriver()
    fallback = BackgroundTypeDriver("later_background", physical=False)
    svc = _make_service([driver, fallback])

    result = svc.background_action(
        "type_text",
        {
            "app": "Private App",
            "bundle_id": "private.bundle",
            "pid": 123,
            "window_id": 456,
            "window_title": "Private title",
        },
        {"text": "private typed text"},
        verified_only=True,
    )

    assert result["executed"] is True
    assert result["data"]["completion_verified"] is False
    assert result["data"]["outcome"] == "posted_unverified"
    assert result["data"]["verification_required"] == "screenshot"
    assert driver.called is True
    assert fallback.called is False
    candidate = result["data"]["ax_candidate"]
    assert candidate == {
        "driver_registered": True,
        "driver_available": True,
        "background_type_capable": True,
        "attempted": True,
        "result_code": "AX_TYPE_POSTED_UNVERIFIED",
        "pyobjc_ax_import_available": True,
        "ax_process_trusted": True,
        "ax_set_value_unsafe_app": False,
        "target_app_present": True,
        "target_bundle_present": True,
        "target_pid_present": True,
        "target_window_present": True,
    }
    serialized = str(candidate)
    for private in ("Private App", "private.bundle", "123", "456", "Private title", "private typed text"):
        assert private not in serialized


def test_posted_key_is_terminal_and_does_not_fallback_to_second_driver():
    first = BackgroundKeyDriver("first_post_only")
    second = BackgroundKeyDriver("second_post_only")
    svc = _make_service([first, second])

    result = svc.background_action(
        "key", {"app": "Test"}, {"key_combo": "return"}, verified_only=True
    )

    assert first.called is True
    assert second.called is False
    assert result["executed"] is True
    assert result["data"]["input_dispatched"] is True
    assert result["data"]["completion_verified"] is False
    assert result["data"]["outcome"] == "posted_unverified"
    assert result["data"]["verification_required"] == "focus_state"


def test_registry_ax_candidate_diagnostics_reports_absence_without_import_guessing():
    diagnostics = DriverRegistry().safe_ax_candidate_diagnostics(set())

    assert diagnostics == {
        "driver_registered": False,
        "driver_available": False,
        "background_type_capable": False,
        "attempted": False,
        "result_code": "AX_DRIVER_NOT_REGISTERED",
    }


@pytest.mark.parametrize("driver_name", ["mac_cgevent_pid", "windows_postmessage"])
def test_post_only_transports_are_explicit_background_only(driver_name):
    driver = BackgroundKeyDriver(driver_name)
    svc = _make_service([driver])

    normal = svc.key({"app": "Unknown", "pid": 123}, key_combo="return")
    assert normal["executed"] is False
    assert driver.called is False

    background = svc.background_action("key", {"app": "Unknown", "pid": 123}, {"key_combo": "return"})

    assert background["executed"] is True
    assert background["driver"] == driver_name
    assert driver.called is True

    driver.called = False
    verified = svc.background_action(
        "key",
        {"app": "Unknown", "pid": 123},
        {"key_combo": "return"},
        verified_only=True,
    )

    assert verified["executed"] is False
    assert driver.called is False


@pytest.mark.parametrize("driver_name", ["mac_cgevent_pid", "windows_postmessage"])
@pytest.mark.parametrize(
    ("action", "payload", "expected"),
    [
        ("click", {"x": 10, "y": 20, "button": "left"}, {"x": 10, "y": 20, "button": "left", "pid": 123}),
        ("type_text", {"text": "hello"}, {"text": "hello", "pid": 123}),
        ("key", {"key_combo": "return"}, {"key_combo": "return", "pid": 123}),
        ("scroll", {"x": 7, "y": 8, "direction": "down", "clicks": 2}, {"x": 7, "y": 8, "direction": "down", "clicks": 2, "pid": 123}),
    ],
)
def test_pid_event_uses_only_pid_safe_transports(driver_name, action, payload, expected):
    unsafe = PidEventDriver("mac_swift_host", unsafe_foreground=True)
    safe = PidEventDriver(driver_name)
    svc = _make_service([unsafe, safe])

    result = svc.pid_event(action, {"app": "Vivaldi", "pid": 123}, payload)

    assert result["executed"] is True
    assert result["driver"] == driver_name
    assert result["uses_physical_input"] is False
    assert result["requires_foreground"] is False
    assert result["can_parallel_user_work"] is True
    assert unsafe.calls == []
    assert safe.calls == [(action, expected)]


def test_pid_event_rejects_driver_without_can_pid_event():
    driver = BackgroundKeyDriver("mac_cgevent_pid")
    svc = _make_service([driver])

    result = svc.pid_event("key", {"app": "Vivaldi", "pid": 123}, {"key_combo": "return"})

    assert result["executed"] is False
    assert driver.called is False
    assert "No PID/PostMessage driver accepted" in result["notes"][-1]


def test_mac_accessibility_skips_ax_set_value_for_vivaldi():
    from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.drivers.mac_accessibility import (
        MacAccessibilityDriver,
    )

    driver = MacAccessibilityDriver()
    result = driver.type_text(
        ComputerTarget(app="Vivaldi", pid=1234, bundle_id="com.vivaldi.Vivaldi"),
        text="hello",
    )

    assert result.executed is False
    assert result.uses_physical_input is False
    assert "Skipping AXSetValue" in result.notes[0]


def test_mac_accessibility_chromium_detection_uses_exact_names_and_bundles():
    from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.drivers.mac_accessibility import (
        MacAccessibilityDriver,
    )

    positives = [
        ComputerTarget(app="Vivaldi", bundle_id="com.vivaldi.Vivaldi"),
        ComputerTarget(app="Google Chrome", bundle_id="com.google.Chrome"),
        ComputerTarget(app="Microsoft Edge", bundle_id="com.microsoft.edgemac"),
        ComputerTarget(app="Arc", bundle_id="company.thebrowser.Browser"),
    ]
    negatives = [
        ComputerTarget(app="Archive Utility", bundle_id="com.apple.archiveutility"),
        ComputerTarget(app="Ledger Live", bundle_id="com.ledger.live"),
    ]

    assert all(MacAccessibilityDriver._target_avoids_ax_set_value(target) for target in positives)
    assert not any(MacAccessibilityDriver._target_avoids_ax_set_value(target) for target in negatives)


def test_mac_accessibility_candidate_diagnostics_use_real_ax_readiness_and_redact_target(monkeypatch):
    from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.drivers.mac_accessibility import (
        MacAccessibilityDriver,
    )
    from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.mac import ax

    monkeypatch.setattr(ax, "ax_import_available", lambda: True)
    monkeypatch.setattr(ax, "ax_is_trusted", lambda: True)
    diagnostics = MacAccessibilityDriver().safe_type_candidate_diagnostics(
        ComputerTarget(
            app="Vivaldi",
            bundle_id="com.vivaldi.Vivaldi",
            pid=123,
            window_id=456,
            window_title="Private title",
        )
    )

    assert diagnostics == {
        "pyobjc_ax_import_available": True,
        "ax_process_trusted": True,
        "ax_set_value_unsafe_app": True,
        "target_app_present": True,
        "target_bundle_present": True,
        "target_pid_present": True,
        "target_window_present": True,
        "attempted": False,
        "result_code": "AX_SET_VALUE_UNSAFE_APP",
    }
    serialized = str(diagnostics)
    for private in ("Vivaldi", "com.vivaldi.Vivaldi", "123", "456", "Private title"):
        assert private not in serialized


def test_macos_cgevent_keycodes_and_modifier_validation():
    from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.mac import cgevent

    expected_f_keys = {
        "f1": 122,
        "f2": 120,
        "f3": 99,
        "f4": 118,
        "f5": 96,
        "f6": 97,
        "f7": 98,
        "f8": 100,
        "f9": 101,
        "f10": 109,
        "f11": 103,
        "f12": 111,
        "f13": 105,
        "f14": 107,
        "f15": 113,
        "f16": 106,
        "f17": 64,
        "f18": 79,
        "f19": 80,
        "f20": 90,
    }
    for key, code in expected_f_keys.items():
        assert cgevent._key_code(key) == code
    assert cgevent._key_code("left") == 123
    assert cgevent._key_code("right") == 124
    assert cgevent._key_code("down") == 125
    assert cgevent._key_code("up") == 126
    assert cgevent._key_combo_parts("cmd+ctrl+option+shift+delete")[0] == 51
    assert cgevent._key_combo_parts("fn+delete") == (None, 0)
    assert cgevent._key_combo_parts("typo+delete") == (None, 0)


def test_audit_logger_called():
    from tobkiri_runtime.ecosystem.rumi_default_tools_pack.domain.computer.audit import AuditLogger

    logger = AuditLogger(log_path="/dev/null")
    logger.record = MagicMock(return_value=None)
    reg = DriverRegistry()
    reg.register(MockDriver("mock1"))
    svc = ComputerSeatService(reg, audit_logger=logger)
    svc._platform = "darwin"
    svc.click({"app": "Test"})
    assert logger.record.called
