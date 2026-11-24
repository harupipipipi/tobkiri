from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from core_runtime.host_broker.computer_host_helper import (
    _computer_result_envelope,
    _safe_type_diagnostics,
    _verified_type_result,
)
from ecosystem.rumi_default_tools_pack.domain.computer import (
    ComputerSeatService,
    DriverRegistry,
)
from ecosystem.rumi_default_tools_pack.domain.computer.drivers.mac_swift_host import (
    MacSwiftHostDriver,
)
from ecosystem.rumi_default_tools_pack.domain.computer.models import (
    ActionResult,
    ComputerCapabilities,
    ComputerTarget,
)
from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import (
    BrowserComputerController,
)


ROOT = Path(__file__).resolve().parent.parent
SWIFT_HOST_SOURCE = (
    ROOT
    / "ecosystem"
    / "rumi_default_tools_pack"
    / "domain"
    / "computer"
    / "mac"
    / "ComputerUseHost.swift"
)


class FakeSwiftHost:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def available(self) -> bool:
        return True

    def run(self, action: str, args: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, dict(args)))
        return dict(self.response)


@pytest.mark.parametrize(
    ("response", "expected_executed"),
    [
        (
            {
                "action": "computer.type",
                "executed": True,
                "input_dispatched": True,
                "completion_verified": False,
            },
            False,
        ),
        (
            {
                "action": "computer.type",
                "executed": True,
                "input_dispatched": True,
                "completion_verified": True,
                "completion_check": "focused_ax_value",
                "input_strategy": "ax_value",
            },
            True,
        ),
    ],
)
def test_mac_swift_driver_requires_verified_type_completion(response, expected_executed) -> None:
    host = FakeSwiftHost(response)
    driver = MacSwiftHostDriver(host=host)

    result = driver.type_text(ComputerTarget(app="Atlas", pid=42), text="youtube")

    assert result.executed is expected_executed
    assert result.data["input_dispatched"] is True
    if expected_executed:
        assert result.data["input_strategy"] == "ax_value"
    assert host.calls == [
        (
            "computer.type",
            {"coordinate_space": "window", "app": "Atlas", "pid": 42, "text": "youtube"},
        )
    ]


def test_mac_swift_driver_background_semantic_text_contract() -> None:
    host = FakeSwiftHost({
        "action": "computer.set_text_control",
        "executed": True,
        "input_dispatched": True,
        "completion_verified": True,
        "completion_check": "same_element_ax_value",
        "input_strategy": "semantic_ax_value",
    })
    driver = MacSwiftHostDriver(host=host)
    target = ComputerTarget(
        app="ChatGPT Atlas", pid=42, window_id=99,
        window_bounds={"x": 10, "y": 20, "width": 1200, "height": 800},
    )
    selector = {
        "roles": ["AXTextField", "AXComboBox", "AXTextArea"],
        "relative_region": {"min_x": 0.08, "max_x": 0.94, "min_y": 0.0, "max_y": 0.22},
        "require_enabled": True, "require_settable": True,
        "preference": "widest", "require_background": True,
    }

    result = driver.set_text_control(target, text="youtube", selector=selector)

    assert result.executed is True
    assert result.can_parallel_user_work is True
    assert result.requires_foreground is False
    assert result.uses_physical_input is False
    assert host.calls == [("computer.set_text_control", {
        "coordinate_space": "window", "app": "ChatGPT Atlas", "pid": 42,
        "window_id": 99, "window_x": 10, "window_y": 20,
        "window_width": 1200, "window_height": 800,
        "text": "youtube", "selector": selector,
    })]


def test_mac_swift_driver_read_only_semantic_probe_contract() -> None:
    host = FakeSwiftHost({
        "action": "computer.probe_text_control",
        "executed": True,
        "probe_completed": True,
        "semantic_control_ready": False,
        "semantic_discovery_stage": "scan_incomplete",
        "error_code": "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
        "input_dispatched": False,
        "mutation_attempted": False,
    })
    driver = MacSwiftHostDriver(host=host)
    target = ComputerTarget(
        app="ChatGPT Atlas", pid=42, window_id=99,
        window_bounds={"x": 10, "y": 20, "width": 1200, "height": 800},
    )
    selector = {
        "roles": ["AXTextField", "AXComboBox", "AXTextArea"],
        "forbidden_ancestor_roles": ["AXWebArea"],
    }

    result = driver.probe_text_control(target, selector=selector)

    assert result.executed is True
    assert result.can_parallel_user_work is True
    assert result.requires_foreground is False
    assert result.uses_physical_input is False
    assert result.data["probe_completed"] is True
    assert result.data["semantic_control_ready"] is False
    assert result.data["input_dispatched"] is False
    assert result.data["mutation_attempted"] is False
    assert host.calls == [("computer.probe_text_control", {
        "coordinate_space": "window", "app": "ChatGPT Atlas", "pid": 42,
        "window_id": 99, "window_x": 10, "window_y": 20,
        "window_width": 1200, "window_height": 800,
        "selector": selector,
    })]


class TypeCompletionDriver:
    def __init__(self, name: str, result: ActionResult) -> None:
        self.name = name
        self.platform = "test"
        self.result = result
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def capabilities(self) -> ComputerCapabilities:
        return ComputerCapabilities()

    def type_text(self, target: ComputerTarget, text: str = "") -> ActionResult:
        self.calls += 1
        return self.result


def test_type_dispatch_without_completion_is_not_replayed_by_fallback_driver() -> None:
    partial = TypeCompletionDriver(
        "partial_native",
        ActionResult(
            action="type_text",
            driver="partial_native",
            executed=False,
            confidence="failed",
            data={"input_dispatched": True, "completion_verified": False},
            notes=["full completion was not verified"],
        ),
    )
    fallback = TypeCompletionDriver(
        "fallback",
        ActionResult(action="type_text", driver="fallback", executed=True),
    )
    registry = DriverRegistry()
    registry.register(partial)
    registry.register(fallback)
    service = ComputerSeatService(registry)
    service._platform = "test"

    result = service.type_text({"app": "Atlas"}, text="youtube")

    assert result["executed"] is False
    assert result["driver"] == "partial_native"
    assert result["data"]["input_dispatched"] is True
    assert partial.calls == 1
    assert fallback.calls == 0


class ControllerSeat:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls = 0

    def type_text(self, target, text=""):
        self.calls += 1
        return dict(self.result)


class SemanticControllerSeat:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls: list[tuple[dict[str, object], str, dict[str, object]]] = []
        self.probe_calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def set_text_control(self, target, text="", selector=None):
        self.calls.append((dict(target), text, dict(selector or {})))
        return dict(self.result)

    def probe_text_control(self, target, selector=None):
        self.probe_calls.append((dict(target), dict(selector or {})))
        return dict(self.result)


def _controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BrowserComputerController:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "mac swift type completion")
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))
    controller = BrowserComputerController(artifact_root=tmp_path / "artifacts")
    controller._session_path = tmp_path / "user_data" / "shared" / "browser_sessions.json"
    monkeypatch.setattr(controller, "_focus_action_target", lambda payload: None)
    monkeypatch.setattr(controller, "_foreground_action_focus_error", lambda action, payload: None)
    monkeypatch.setattr(
        controller,
        "_computer_seat_target",
        lambda payload: {"kind": "desktop", "app": "Atlas", "coordinate_space": "window"},
    )
    return controller


def test_controller_returns_error_instead_of_legacy_replay_after_partial_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path, monkeypatch)
    controller._computer_seat = ControllerSeat(
        {
            "action": "type_text",
            "driver": "mac_swift_host",
            "executed": False,
            "data": {"input_dispatched": True, "completion_verified": False},
            "notes": ["full completion was not verified"],
        }
    )
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        controller,
        "_darwin_type",
        lambda payload: pytest.fail("partial native input must not be replayed"),
    )

    result = controller.run(
        "computer.type",
        {
            "text": "youtube",
            "fallback": "foreground",
            "app": "Atlas",
            "include_screenshot": False,
        },
        yolo_mode=True,
    )

    assert result["executed"] is False
    assert result["is_error"] is True
    assert result["input_dispatched"] is True
    assert result["completion_verified"] is False
    assert result["recovery"]["kind"] == "type_completion_unverified"


def test_controller_maps_browser_address_intent_to_terminal_generic_background_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path, monkeypatch)
    seat = SemanticControllerSeat({
        "action": "set_text_control", "driver": "mac_swift_host", "executed": True,
        "confidence": "verified", "can_parallel_user_work": True,
        "requires_foreground": False, "uses_physical_input": False,
        "data": {"input_dispatched": True, "completion_verified": True,
                 "completion_check": "same_element_ax_value"},
    })
    controller._computer_seat = seat
    target = {
        "kind": "window", "app": "ChatGPT Atlas", "pid": 42, "window_id": 99,
        "window_bounds": {"x": 10, "y": 20, "width": 1200, "height": 800},
        "coordinate_space": "window",
    }
    monkeypatch.setattr(controller, "_computer_seat_target", lambda payload: dict(target))
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(controller, "_focus_action_target", lambda payload: pytest.fail("must stay background"))
    monkeypatch.setattr(controller, "_darwin_type", lambda payload: pytest.fail("must not use legacy typing"))

    result = controller.run(
        "computer.type",
        {"text": "youtube", "target_control": "browser_address", "include_screenshot": False},
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert result["background"] is True
    assert result["completion_verified"] is True
    assert len(seat.calls) == 1
    called_target, called_text, called_selector = seat.calls[0]
    assert called_target == target
    assert called_text == "youtube"
    assert called_selector["roles"] == ["AXTextField", "AXComboBox", "AXTextArea"]
    assert called_selector["require_background"] is True
    assert called_selector["forbidden_ancestor_roles"] == ["AXWebArea"]


def test_controller_probe_maps_browser_address_without_text_approval_or_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    seat = SemanticControllerSeat({
        "action": "computer.probe_text_control",
        "driver": "mac_swift_host",
        "executed": True,
        "data": {
            "probe_completed": True,
            "semantic_control_ready": False,
            "input_dispatched": False,
            "mutation_attempted": False,
            "semantic_discovery_stage": "scan_incomplete",
            "semantic_traversal_order": "breadth_first",
            "semantic_window_scan_complete": False,
            "semantic_window_scan_truncated": True,
            "semantic_window_nodes_visited_count": 255,
            "error_code": "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
            "title": "CANARY_PRIVATE_TITLE",
            "value": "CANARY_PRIVATE_VALUE",
            "pid": 99123,
        },
    })
    controller._computer_seat = seat
    target = {
        "kind": "window", "app": "ChatGPT Atlas", "pid": 42, "window_id": 99,
        "window_bounds": {"x": 10, "y": 20, "width": 1200, "height": 800},
        "coordinate_space": "window",
    }
    monkeypatch.setattr(controller, "_computer_seat_target", lambda payload: dict(target))
    monkeypatch.setattr(
        controller,
        "_desktop_action",
        lambda *args, **kwargs: pytest.fail("probe must not enter write/fallback routing"),
    )

    result = controller.run(
        "computer.probe_text_control",
        {
            "target_control": "browser_address", "app": "ChatGPT Atlas",
            "background": True, "focus": False, "include_screenshot": False,
        },
        yolo_mode=False,
    )

    assert result["executed"] is True
    assert result["probe_completed"] is True
    assert result["semantic_control_ready"] is False
    assert result.get("is_error") is not True
    assert result["input_dispatched"] is False
    assert result["mutation_attempted"] is False
    assert result["error_code"] == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    assert seat.calls == []
    assert seat.probe_calls == [(target, {
        "roles": ["AXTextField", "AXComboBox", "AXTextArea"],
        "relative_region": {"min_x": 0.08, "max_x": 0.94, "min_y": 0.0, "max_y": 0.22},
        "require_enabled": True,
        "require_settable": True,
        "preference": "widest",
        "require_background": True,
        "forbidden_ancestor_roles": ["AXWebArea"],
    })]
    serialized = json.dumps(result)
    assert "CANARY" not in serialized
    assert "99123" not in serialized


def test_controller_semantic_discovery_diagnostics_are_bounded_and_content_free() -> None:
    raw = {
        "data": {"diagnostics": {
            "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
            "input_dispatched": False,
            "completion_verified": False,
            "semantic_nodes_visited_count": 255,
            "semantic_role_match_count": 3,
            "semantic_final_candidate_count": 0,
            "semantic_counts_truncated": True,
            "saw_ax_text_field": True,
            "saw_ax_search_field_subrole": True,
            "saw_ax_web_area_ancestor": True,
            "semantic_scan_scope": "application_tree_owned",
            "semantic_discovery_stage": "web_content_excluded",
            "semantic_coordinate_status": "consistent",
            "semantic_ownership_proof": "ax_window_attribute",
            "window_frame_match": True,
            "child_frame_valid": True,
            "child_center_inside_window": True,
            "relative_region_evaluable": True,
            "relative_region_matched": True,
            "text": "private text", "title": "private title", "pid": 123,
            "window_id": 456, "element_id": "private element",
            "geometry": {"x": 1}, "path": "/private/path", "raw_error": "private error",
        }}
    }

    safe = BrowserComputerController._safe_semantic_text_diagnostics(raw)

    assert safe["error_code"] == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert safe["semantic_nodes_visited_count"] == 255
    assert safe["semantic_role_match_count"] == 3
    assert safe["semantic_counts_truncated"] is True
    assert safe["semantic_scan_scope"] == "application_tree_owned"
    assert safe["semantic_discovery_stage"] == "web_content_excluded"
    serialized = str(safe)
    for private in ("private text", "private title", "123", "456", "private element", "/private/path", "private error"):
        assert private not in serialized


def test_controller_normalizes_legacy_stale_subtree_probe_code_to_repeated_branch() -> None:
    code = "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE"
    expected = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    safe = BrowserComputerController._safe_semantic_text_diagnostics({
        "data": {
            "diagnostics": {
                "error_code": code,
                "probe_completed": True,
                "semantic_control_ready": False,
                "input_dispatched": False,
                "mutation_attempted": False,
                "semantic_discovery_stage": "scan_incomplete",
                "title": "CANARY_PRIVATE_TITLE",
            }
        }
    })

    assert safe["error_code"] == expected
    assert safe["semantic_control_ready"] is False
    assert safe["input_dispatched"] is False
    assert safe["mutation_attempted"] is False
    assert "CANARY" not in json.dumps(safe)

    lookalike = BrowserComputerController._safe_semantic_text_diagnostics({
        "data": {"diagnostics": {"error_code": f"{code}_LOOKALIKE"}}
    })
    assert "error_code" not in lookalike


def test_controller_preserves_zero_dispatch_native_failure_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path, monkeypatch)
    diagnostics = {
        "error_code": "TYPE_VERIFICATION_UNAVAILABLE",
        "input_strategy": "none",
        "completion_verified": False,
        "input_dispatched": False,
        "dispatched_units": 0,
        "failure_stage": "initial_target_verification",
        "direct_ax_attempted": False,
    }
    controller._computer_seat = ControllerSeat({
        "action": "type_text",
        "driver": "mac_swift_host",
        "executed": False,
        "data": {"diagnostics": diagnostics},
        "notes": ["The focused text field could not be verified."],
    })
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        controller,
        "_darwin_type",
        lambda payload: pytest.fail("structured native failure must not use legacy typing"),
    )

    result = controller.run(
        "computer.type",
        {"text": "youtube", "app": "Atlas", "fallback": "foreground", "include_screenshot": False},
        yolo_mode=True,
    )

    assert result["executed"] is False
    assert result["input_dispatched"] is False
    assert result["diagnostics"] == diagnostics
    assert result["recovery"]["kind"] == "type_completion_unverified"


def test_controller_propagates_verified_completion_to_viewer_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer

    controller = _controller(tmp_path, monkeypatch)
    controller._computer_seat = ControllerSeat(
        {
            "action": "type_text",
            "driver": "mac_swift_host",
            "executed": True,
            "data": {
                "input_dispatched": True,
                "completion_verified": True,
                "completion_check": "focused_ax_value",
            },
        }
    )
    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")

    result = controller.run(
        "computer.type",
        {
            "text": "youtube",
            "fallback": "foreground",
            "app": "Atlas",
            "include_screenshot": False,
        },
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert result["completion_verified"] is True
    assert result["completion_check"] == "focused_ax_value"
    assert _verified_type_result(result) is True


def test_viewer_helper_rejects_success_without_verified_full_type_completion() -> None:
    assert _verified_type_result({"executed": True}) is False
    assert _verified_type_result({"executed": True, "completion_verified": False}) is False
    assert _verified_type_result({"executed": False, "completion_verified": True}) is False
    assert _verified_type_result({"executed": True, "completion_verified": True}) is True

    incomplete = {"executed": False, "input_dispatched": True, "completion_verified": False}
    envelope = _computer_result_envelope("computer.type", incomplete)
    assert envelope["ok"] is False
    assert envelope["error_code"] == "TYPE_COMPLETION_NOT_VERIFIED"
    assert envelope["result"]["action"] == "computer.type"
    assert envelope["result"]["executed"] is False
    assert envelope["result"]["delivered"] is True
    assert envelope["result"]["outcome"] == "posted_unverified"
    assert envelope["result"]["verification_required"] == "screenshot"
    assert envelope["result"]["diagnostics"] == {
        "input_dispatched": True,
        "completion_verified": False,
    }
    assert envelope["diagnostics"] == {"input_dispatched": True, "completion_verified": False}

    complete = {"executed": True, "completion_verified": True}
    assert _computer_result_envelope("computer.type", complete) == {"ok": True, "result": complete}


def test_viewer_helper_preserves_posted_delivery_without_leaking_content() -> None:
    envelope = _computer_result_envelope(
        "computer.type",
        {
            "executed": True,
            "background": True,
            "driver": "mac_accessibility",
            "can_parallel_user_work": True,
            "requires_foreground": False,
            "uses_physical_input": False,
            "completion_verified": False,
            "ax_candidate": {
                "driver_registered": True,
                "driver_available": True,
                "background_type_capable": True,
                "pyobjc_ax_import_available": True,
                "ax_process_trusted": True,
                "ax_set_value_unsafe_app": False,
                "target_app_present": True,
                "target_bundle_present": True,
                "target_pid_present": True,
                "target_window_present": True,
                "attempted": True,
                "result_code": "AX_TYPE_POSTED_UNVERIFIED",
                "raw_target": "private AX target",
            },
            "text": "private typed text",
            "url": "https://example.invalid/?secret=query",
            "window_title": "private title",
            "pid": 123,
            "window_id": 456,
            "approval_token": "private token",
            "error": "private raw error",
        },
    )

    result = envelope["result"]
    assert envelope["ok"] is False
    assert result["executed"] is True
    assert result["delivered"] is True
    assert result["background"] is True
    assert result["can_parallel_user_work"] is True
    assert result["requires_foreground"] is False
    assert result["uses_physical_input"] is False
    assert result["completion_verified"] is False
    assert result["outcome"] == "posted_unverified"
    assert result["verification_required"] == "screenshot"
    assert result["ax_candidate"] == {
        "driver_registered": True,
        "driver_available": True,
        "background_type_capable": True,
        "pyobjc_ax_import_available": True,
        "ax_process_trusted": True,
        "ax_set_value_unsafe_app": False,
        "target_app_present": True,
        "target_bundle_present": True,
        "target_pid_present": True,
        "target_window_present": True,
        "attempted": True,
        "result_code": "AX_TYPE_POSTED_UNVERIFIED",
    }
    serialized = str(envelope)
    for private in (
        "private typed text",
        "secret=query",
        "private title",
        "123",
        "456",
        "private token",
        "private raw error",
        "private AX target",
    ):
        assert private not in serialized


def test_viewer_helper_preserves_safe_pre_native_foreground_diagnostics() -> None:
    foreground_rejection = {
        "action": "computer.type",
        "executed": False,
        "is_error": True,
        "diagnostics": {
            "error_code": "TYPE_FOREGROUND_TARGET_NOT_VERIFIED",
            "input_strategy": "none",
            "completion_verified": False,
            "input_dispatched": False,
            "dispatched_units": 0,
            "target_pid_stable": False,
            "focused_element_stable": False,
            "failure_stage": "foreground_target_verification",
            "direct_ax_attempted": False,
            "mutation_observed": False,
            "private_window_title": "must not escape",
        },
    }

    envelope = _computer_result_envelope("computer.type", foreground_rejection)

    assert envelope["ok"] is False
    assert envelope["diagnostics"]["error_code"] == "TYPE_FOREGROUND_TARGET_NOT_VERIFIED"
    assert envelope["diagnostics"]["failure_stage"] == "foreground_target_verification"
    assert envelope["diagnostics"]["input_dispatched"] is False
    assert "private_window_title" not in envelope["diagnostics"]


def test_type_diagnostics_are_allowlisted_and_redacted() -> None:
    diagnostics = _safe_type_diagnostics({
        "error_code": "TYPE_TARGET_DRIFTED",
        "input_dispatched": False,
        "dispatched_units": 0,
        "failure_stage": "before_grapheme_dispatch",
        "direct_ax_attempted": True,
        "text": "secret text",
        "approval_token": "secret token",
        "pid": 4321,
        "window_title": "Private window",
        "arbitrary": {"nested": "secret"},
        "data": {"focused_element_stable": False, "raw_args": {"text": "secret"}},
    })

    assert diagnostics == {
        "error_code": "TYPE_TARGET_DRIFTED",
        "input_dispatched": False,
        "dispatched_units": 0,
        "failure_stage": "before_grapheme_dispatch",
        "direct_ax_attempted": True,
        "focused_element_stable": False,
    }


def test_zero_dispatch_structured_native_failure_is_not_replayed() -> None:
    native = TypeCompletionDriver(
        "native",
        ActionResult(
            action="type_text",
            driver="native",
            executed=False,
            data={"diagnostics": {
                "input_dispatched": False,
                "dispatched_units": 0,
                "failure_stage": "initial_target_verification",
                "error_code": "TYPE_VERIFICATION_UNAVAILABLE",
            }},
        ),
    )
    fallback = TypeCompletionDriver(
        "fallback", ActionResult(action="type_text", driver="fallback", executed=True)
    )
    registry = DriverRegistry()
    registry.register(native)
    registry.register(fallback)
    service = ComputerSeatService(registry)
    service._platform = "test"

    result = service.type_text({"app": "Atlas"}, text="youtube")

    assert result["driver"] == "native"
    assert result["executed"] is False
    assert fallback.calls == 0


def test_native_type_completion_self_test(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS native helper test")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("swiftc is unavailable")

    binary = tmp_path / "computer_use_host"
    module_cache = tmp_path / "swift-module-cache"
    module_cache.mkdir()
    compile_env = {
        **os.environ,
        "CLANG_MODULE_CACHE_PATH": str(module_cache),
        "SWIFT_MODULECACHE_PATH": str(module_cache),
    }
    subprocess.run(
        [swiftc, str(SWIFT_HOST_SOURCE), "-o", str(binary)],
        check=True,
        capture_output=True,
        env=compile_env,
        text=True,
        timeout=30,
    )
    completed = subprocess.run(
        [str(binary), "--self-test"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    response = json.loads(completed.stdout)

    assert response == {
        "ok": True,
        "result": {
            "ax_value_replacement": True,
            "direct_no_mutation_fallback": True,
            "direct_replacement": True,
            "explicit_target_rebind": True,
            "explicit_target_rebind_failure_rejected": True,
            "semantic_selector_validated": True,
            "exact_window_geometry_validated": True,
            "semantic_staged_discovery_validated": True,
            "semantic_relative_coordinates_validated": True,
            "semantic_diagnostics_bounded": True,
            "semantic_children_outcomes_validated": True,
            "semantic_children_retry_bounded": True,
            "semantic_exposure_probe_validated": True,
            "semantic_stale_recovery_validated": True,
            "paced_units": 7,
            "partial_direct_not_retried": True,
            "partial_rejected": True,
            "pid_drift_rejected": True,
            "pid_targeted_routing": True,
            "selected_text_priority": True,
            "self_test": True,
            "target_drift_rejected": True,
            "typing_completion": True,
            "unicode_replacement": True,
            "visibility_topology_diagnostics_validated": True,
        },
    }


def test_native_source_direct_ax_insertion_is_strict_and_fallback_is_ordered() -> None:
    source = SWIFT_HOST_SOURCE.read_text(encoding="utf-8")

    assert "AXUIElementIsAttributeSettable" in source
    assert source.index('strategy = "selected_text"') < source.index('strategy = "ax_value"')
    assert "expectation.finalValue" in source
    assert 'currentValue: "A👩‍💻Z"' in source
    assert 'visibleValue = "yo"' in source
    assert "down.postToPid(targetPid)" in source
    assert "up.postToPid(targetPid)" in source
    assert "targetStability: () -> TextInputTargetStability" in source
    assert 'failureCode: "TYPE_TARGET_DRIFTED"' in source
    assert '"direct_ax_attempted": true' in source
    assert '"mutation_observed": observedValue.map { $0 != initialState.value } ?? false' in source
    assert '"fallback_reason": "partial_mutation_rejection"' in source
    assert '"fallback_reason": directWasAttempted ? "direct_no_mutation_fallback"' in source
    assert source.count('"direct_no_mutation_fallback": directWasAttempted') == 2
    assert '"input_strategy": "post_to_pid"' in source
    assert "func resolvedExplicitTextInputTargetPid" in source
    assert "func ensureResolvedTextInputTargetIsFrontmost" in source
    assert "func activateExactTextInputTarget" in source
    assert "resolvedTargetPid ?? targetPid(args: args)" in source
    assert '"failure_stage": "initial_target_rebind"' in source
    assert 'case "computer.set_text_control", "set_text_control"' in source
    assert 'case "computer.probe_text_control", "probe_text_control"' in source
    assert '"semantic_traversal_order": "breadth_first"' in source
    assert 'facts.windowScanComplete && facts.roleMatchCount == 0' in source
    assert 'facts.windowScanTruncated = true' in source
    assert 'facts.windowDepthTruncated = true' in source
    assert 'return "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"' in source
    predicate_body = source[
        source.index("func semanticRepeatedlyStaleBranch("):
        source.index("func semanticDiscoveryErrorCode(")
    ]
    for fact in (
        "staleRecoveryOutcome == \"final_pass_stale\"",
        "staleReferenceRefreshClass == \"same_stale_reference_returned\"",
        "staleBranchComparison == \"same_class_and_depth\"",
        "secondThirdStaleReferenceClass == \"same_parent_same_reference\"",
        "discoveryPassCount == 3",
        "staleRecoveryRestartCount == 2",
        "!facts.windowScanComplete",
        "!facts.staleRecoveryFinalScanComplete",
    ):
        assert fact in predicate_body
    assert "AXUIElement" not in predicate_body
    error_code_body = source[
        source.index("func semanticDiscoveryErrorCode("):
        source.index("func probeSemanticTextControl(")
    ]
    assert error_code_body.index("semanticRepeatedlyStaleBranch(facts)") < error_code_body.index(
        '"TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"'
    )
    assert 'var queue: [SemanticTraversalQueueEntry]' in source
    assert 'element: child, parent: element, depth: depth + 1' in source
    assert 'let parent = entry.parent' in source
    assert 'if error == .cannotComplete' in source
    assert 'AXUIElementCopyAttributeNames' in source
    assert 'AXUIElementGetAttributeValueCount' in source
    assert 'childrenCountKnown: cardinality.known' in source
    assert 'case .provenEmpty:' in source
    assert 'increment(\\.childrenEmptyCount)' in source
    assert 'guard result.scanIncomplete else { return }' in source
    exact_scan = source[source.index("func semanticTextDiscoveryPass("):source.index("// Diagnostic-only app scan")]
    assert exact_scan.index("let candidate = evaluateSemanticCandidate(") < exact_scan.index(
        "let primaryChildRead = semanticChildren("
    )
    assert exact_scan.index("if forbiddenRoot") < exact_scan.index("let primaryChildRead = semanticChildren(")
    assert exact_scan.index("let primaryChildRead = semanticChildren(") < exact_scan.index(
        "let authoritativeChildRead = semanticAuthoritativeChildren("
    )
    assert "facts.increment(\\.windowDuplicateNodesSkippedCount" in exact_scan
    assert "CFEqual($0, child)" in exact_scan
    assert "semantic_actionable_scan_complete" in source
    assert "semantic_unresolved_selector_branch_count" in source
    assert "semantic_preinvalidation_candidate_count" in source
    assert "semantic_stale_branch_scope" in source
    assert "semantic_stale_node_self_eligible" in source
    assert "semantic_stale_node_class" in source
    assert '"candidate_node"' in source
    assert '"selector_relevant_unknown"' in source
    assert '"structurally_empty"' in source
    assert '"accessibility_trust_preflight"' in source
    assert '"TYPE_ACCESSIBILITY_API_UNAVAILABLE"' in source
    assert '"TYPE_SEMANTIC_PROTOCOL_INVALID"' in source
    for field in (
        "semantic_children_read_success_count",
        "semantic_children_empty_count",
        "semantic_children_unsupported_count",
        "semantic_children_no_value_count",
        "semantic_children_cannot_complete_count",
        "semantic_children_invalid_element_count",
        "semantic_children_global_failure_count",
        "semantic_children_protocol_failure_count",
        "semantic_children_unknown_branch_count",
        "semantic_children_proven_empty_after_failure_count",
        "semantic_children_retry_attempted_count",
        "semantic_children_retry_recovered_count",
        "semantic_children_failure_class",
        "semantic_children_incomplete_branch_class",
        "semantic_navigation_order_fallback_attempted_count",
        "semantic_navigation_order_fallback_succeeded_count",
        "semantic_navigation_order_recovered_invalid_count",
        "semantic_navigation_order_page_read_count",
        "semantic_navigation_order_fallback_outcome",
        "semantic_navigation_order_failure_class",
        "semantic_navigation_order_ax_error_class",
        "semantic_navigation_order_cardinality_class",
        "semantic_navigation_order_parent_proof",
        "semantic_navigation_order_count_stable",
        "semantic_navigation_order_complete",
    ):
        assert f'"{field}"' in source
    for field in (
        "semantic_stale_recovery_eligible",
        "semantic_stale_recovery_attempted",
        "semantic_stale_recovery_window_rebound",
        "semantic_stale_recovery_window_stable",
        "semantic_stale_recovery_second_pass_complete",
        "semantic_stale_recovery_succeeded",
        "semantic_stale_parent_refresh_attempted",
        "semantic_stale_parent_refresh_succeeded",
        "semantic_stale_recovery_final_scan_complete",
        "semantic_stale_additional_read_budget_exhausted",
        "semantic_discovery_pass_count",
        "semantic_stale_recovery_restart_count",
        "semantic_stale_parent_refresh_count",
        "semantic_stale_parent_refresh_read_count",
        "semantic_stale_additional_ax_read_count",
        "semantic_first_pass_stale_count",
        "semantic_second_pass_stale_count",
        "semantic_first_pass_unknown_branch_count",
        "semantic_second_pass_unknown_branch_count",
        "semantic_first_pass_nodes_visited_count",
        "semantic_second_pass_nodes_visited_count",
        "semantic_second_pass_final_candidate_count",
        "semantic_third_pass_stale_count",
        "semantic_third_pass_unknown_branch_count",
        "semantic_third_pass_nodes_visited_count",
        "semantic_third_pass_final_candidate_count",
        "semantic_stale_reference_refresh_class",
        "semantic_stale_branch_comparison",
        "semantic_second_third_stale_reference_class",
        "semantic_stale_recovery_outcome",
    ):
        assert f'"{field}"' in source
    for outcome in (
        "not_needed",
        "recovered_clean",
        "recovery_not_eligible",
        "exact_window_rebind_failed",
        "exact_window_changed",
        "frontmost_changed",
        "parent_refresh_not_eligible",
        "parent_refresh_failed",
        "parent_refresh_budget_exhausted",
        "recovered_after_parent_refresh",
        "final_pass_stale",
        "final_pass_incomplete",
    ):
        assert f'"{outcome}"' in source
    stale_comparison_body = source[
        source.index("func semanticSecondThirdStaleReferenceClass("):
        source.index("func semanticTextDiscoveryWithStaleRecovery(")
    ]
    assert stale_comparison_body.count("CFEqual(") == 2
    for outcome in (
        "same_parent_same_reference",
        "same_parent_new_reference",
        "new_parent_same_reference",
        "new_parent_new_reference",
        "not_comparable",
    ):
        assert f'"{outcome}"' in stale_comparison_body
    semantic_body = source[source.index("func setSemanticTextControl("):source.index("func axAttributeIsSettable(")]
    probe_body = source[source.index("func probeSemanticTextControl("):source.index("func setSemanticTextControl(")]
    assert "semanticTextDiscoveryWithStaleRecovery(" in probe_body
    assert "semanticTextDiscoveryWithStaleRecovery(" in semantic_body
    assert "setAXTextAttribute" not in probe_body
    assert "setSelectedTextRange" not in probe_body
    assert "postToPid" not in probe_body
    assert "activate(" not in probe_body
    assert '"input_dispatched": false' in probe_body
    assert '"mutation_attempted": false' in probe_body
    assert "guard discovery.facts.actionableScanComplete()" in semantic_body
    assert 'discovery.facts.discoveryStage() == "ready"' in semantic_body
    assert "activate(" not in semantic_body
    assert "postToPid" not in semantic_body
    assert "OCR" not in semantic_body
    assert '"completion_check"] = "same_element_ax_value"' in semantic_body
    assert 'forbiddenAncestorRoles: Set<String>' in source
    assert '"forbidden_ancestor_roles"' in source
    assert 'facts.scanScope = "application_tree_owned"' not in source
    assert 'semantic_app_diagnostic_scope": "application_tree_owned"' in source
    assert 'semantic_app_diagnostic_stage' in source
    assert 'semantic_actionable_counts_truncated' in source
    assert 'semantic_app_diagnostic_counts_truncated' in source
    assert '_ = evaluateSemanticCandidate(' in source
    assert 'eligibleCandidates.count == 1' in semantic_body
    assert 'role == "AXTextArea"' in source
    assert 'subrole == "AXSearchField"' in source
    assert 'roles: ["AXTextField", "AXComboBox", "AXTextArea"]' not in semantic_body

    direct_switch = source.index("switch direct")
    direct_failure = source.index('case .unverified(let strategy, let observedValue):', direct_switch)
    physical_fallback = source.index("let delivery = deliverPacedText(", direct_switch)
    assert direct_switch < direct_failure < physical_fallback
    guarded_direct_failure = source[direct_failure:physical_fallback]
    assert "observedValue == initialState.value" in guarded_direct_failure
    assert 'fail("TYPE_COMPLETION_NOT_VERIFIED"' in guarded_direct_failure
    type_body = source[source.index("func typeText("):source.index("func typingCompletionSelfTest()")]
    assert '"text": text' not in type_body
    assert "let explicitTargetPid = resolvedExplicitTextInputTargetPid(args: args)" in type_body
    assert "ensureResolvedTextInputTargetIsFrontmost" in type_body
    assert "activate: activateExactTextInputTarget" in type_body
    assert "resolvedTargetPid: explicitTargetPid" in type_body


def test_native_source_navigation_order_fallback_is_narrow_complete_and_nonretrying() -> None:
    source = SWIFT_HOST_SOURCE.read_text(encoding="utf-8")
    body = source[
        source.index("func semanticNavigationOrderChildren("):
        source.index("func semanticAuthoritativeChildren(")
    ]
    authoritative = source[
        source.index("func semanticAuthoritativeChildren("):
        source.index("func semanticChildrenErrorClass(")
    ]
    exact_scan = source[
        source.index("func semanticTextDiscoveryPass("):
        source.index("// Diagnostic-only app scan")
    ]

    assert "pageSize: CFIndex = 32" in body
    assert "maximumElements: CFIndex = 255" in body
    assert "maximumPageReads: Int = 8" in body
    assert "AXChildrenInNavigationOrder" in body
    assert "AXUIElementCopyAttributeValues" in body
    assert body.count("count(element)") == 2
    assert "values.count == Int(requested)" in body
    assert "converted.count == values.count" in body
    assert "!CFEqual(child, element)" in body
    assert "!children.contains(where: { CFEqual($0, child) })" in body
    assert "CFEqual(actualParent, element)" in body
    assert "usleep" not in body
    assert "retry" not in body.lower()
    assert "guard primary.outcome == .staleElement" in authoritative
    assert "guard fallback.succeeded" in authoritative
    assert exact_scan.count("semanticAuthoritativeChildren(") == 1
    assert "semanticAuthoritativeChildren(" not in source[
        source.index("// Diagnostic-only app scan"):
        source.index("func semanticExposureFullEligibility(")
    ]
    assert 'case "AXTextField", "AXComboBox", "AXTextArea"' in source


def test_native_source_semantic_exposure_probe_is_bounded_read_only_and_non_actionable() -> None:
    source = SWIFT_HOST_SOURCE.read_text(encoding="utf-8")
    exposure = source[
        source.index("func semanticAlternateExposureProbe("):
        source.index("func semanticTextDiscoveryWithStaleRecovery(")
    ]
    probe = source[
        source.index("func probeSemanticTextControl("):
        source.index("func setSemanticTextControl(")
    ]
    setter = source[
        source.index("func setSemanticTextControl("):
        source.index("func axAttributeIsSettable(")
    ]

    for attribute in (
        "AXContents",
        "AXVisibleChildren",
        "AXChildrenInNavigationOrder",
        "AXSharedTextUIElements",
        "AXTitleUIElement",
        "AXServesAsTitleForUIElements",
        "AXLinkedUIElements",
        "AXParent",
    ):
        assert f'"{attribute}"' in source
    for capability in (
        "AXUIElementsForSearchPredicate",
        "AXUIElementForTextMarker",
        "AXTextMarkerRangeForUIElement",
    ):
        assert f'"{capability}"' in source
    for field in (
        "semantic_exposure_probe_performed",
        "semantic_exposure_probe_complete",
        "semantic_exposure_probe_truncated",
        "semantic_alt_allowed_role_found",
        "semantic_alt_full_eligibility_found",
        "semantic_exposure_nodes_visited_count",
        "semantic_exposure_edge_reads_count",
        "semantic_exposure_edge_read_failure_count",
        "semantic_exposure_exact_owned_count",
        "semantic_exposure_non_web_count",
        "semantic_exposure_allowed_role_count",
        "semantic_exposure_full_eligibility_count",
        "semantic_exposure_shared_text_relation_count",
        "semantic_exposure_parameterized_capability_count",
        "semantic_exposure_page_control_count",
        "semantic_exposure_stage",
        "semantic_exposure_source",
        "semantic_parameterized_capability_class",
    ):
        assert f'"{field}"' in source

    assert "AXUIElementCopyParameterizedAttributeNames" in source
    assert "proxySeeds.prefix(4)" in exposure
    assert "facts.nodesVisitedCount < 64" in exposure
    assert "facts.edgeReadsCount < 128" in exposure
    assert "depth < 4" in exposure
    assert "maximumElements: $2" in exposure
    assert 'if role == "AXWebArea" { continue }' in exposure
    assert "CFEqual" in exposure
    assert "eligibleCandidates" not in exposure
    assert "AXUIElementSetAttributeValue" not in exposure
    assert "AXUIElementPerformAction" not in exposure
    assert "postToPid" not in exposure
    assert "activate(" not in exposure
    assert "AXUIElementCopyParameterizedAttributeValue" not in exposure
    assert "semanticAlternateExposureProbe(" in probe
    assert "includeBroadAppDiagnostic: false" in probe
    assert '!key.hasPrefix("semantic_app_")' in probe
    assert 'key != "semantic_other_window_pruned_count"' in probe
    assert 'discovery.facts.discoveryStage() == "role_absent"' in probe
    assert "semanticAlternateExposureProbe(" not in setter
    assert "includeBroadAppDiagnostic: false" not in setter

    payload = source[
        source.index("struct SemanticExposureProbeFacts"):
        source.index("func semanticCGRect(")
    ]
    for forbidden_key in (
        '"attribute"',
        '"parameterized_attribute"',
        '"role"',
        '"subrole"',
        '"value"',
        '"label"',
        '"title"',
        '"element"',
        '"frame"',
        '"pid"',
        '"window_id"',
    ):
        assert forbidden_key not in payload


def test_native_source_allowed_role_geometry_diagnostics_are_bounded_and_non_actionable() -> None:
    source = SWIFT_HOST_SOURCE.read_text(encoding="utf-8")
    geometry = source[
        source.index("mutating func observeAllowedRoleGeometry("):
        source.index("func discoveryStage()")
    ]
    candidate = source[
        source.index("func evaluateSemanticCandidate("):
        source.index("func semanticDiagnosticProxySeed(")
    ]

    for field in (
        "semantic_allowed_ax_text_field_count",
        "semantic_allowed_ax_combo_box_count",
        "semantic_allowed_ax_text_area_count",
        "semantic_allowed_frame_inside_window_count",
        "semantic_allowed_region_x_match_count",
        "semantic_allowed_region_y_match_count",
        "semantic_allowed_role_class",
        "semantic_allowed_region_miss_axis",
        "semantic_allowed_center_y_band",
        "semantic_allowed_width_band",
        "semantic_allowed_height_band",
    ):
        assert f'"{field}"' in source
    for enum_value in (
        "upper_22_35",
        "near_full_80_100",
        "outside_window",
        "frame_unavailable",
    ):
        assert f'"{enum_value}"' in geometry
    assert "cap: 8" in geometry
    assert "semanticCGRect(" not in geometry
    assert "observeAllowedRoleGeometry(" in candidate
    assert candidate.index("observeAllowedRoleGeometry(") < candidate.index(
        "guard relative.relativeRegionMatched"
    )
    assert "semantic_control_ready" not in geometry
    assert "eligibleCandidates.append" not in geometry
    assert 'selectedWindowIdentityDiagnosticContract = "rumi.mac.selected_window_identity.v1"' in source
