from __future__ import annotations

from copy import deepcopy
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from core_runtime.host_broker.computer_delivery import safe_window_selection_facts
from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
from ecosystem.rumi_default_tools_pack.domain.computer.trace import (
    computer_action_trace,
    emit_computer_trace,
    result_trace_facts,
)
from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

pytestmark = pytest.mark.contract


def _window(**overrides):
    value = {
        "app": "ChatGPT Atlas",
        "title": "private title",
        "pid": 321,
        "window_id": 654,
        "x": 10,
        "y": 20,
        "width": 1200,
        "height": 800,
        "active": False,
    }
    value.update(overrides)
    return value


def _controller(tmp_path, windows):
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    controller._list_windows = lambda: deepcopy(windows)
    return controller


def _select(controller, **overrides):
    payload = {
        "app": "ChatGPT Atlas",
        "focus": False,
        "require_exact_binding": True,
    }
    payload.update(overrides)
    return controller.run("computer.select_window", payload, yolo_mode=True)


_SELECTION_INVENTORY_BOOL_FIELDS = (
    "selection_window_owner_alias_matched",
    "selection_requested_alias_valid",
    "selection_requested_bundle_alias_available",
    "selection_swift_helper_available",
    "selection_swift_helper_invoked",
    "selection_swift_helper_compile_attempted",
    "selection_swift_helper_compile_succeeded",
    "selection_native_snapshot_atomic",
    "selection_nsworkspace_observation_completed",
    "selection_nsworkspace_target_process_present",
    "selection_nsworkspace_localized_name_match",
    "selection_nsworkspace_bundle_id_match",
    "selection_target_pid_match_available",
    "selection_target_bundle_match_available",
    "selection_primary_source_nonempty",
    "selection_later_sources_suppressed_by_selection_policy",
    "selection_diagnostic_sources_compared",
    "selection_primary_source_target_match_absent",
    "selection_later_source_target_match_present",
    "selection_primary_source_suppressed_target_observation",
    "selection_inventory_instrumentation_consistent",
    "selection_reobservation_eligible",
    "selection_reobservation_attempted",
    "selection_reobservation_recovered",
    "selection_permission_request_api_invoked",
    *(
        f"selection_{source}_permission_check_colocated"
        for source in ("swift", "quartz", "system_events")
    ),
    *(
        f"selection_{source}_{suffix}"
        for source in ("swift", "quartz")
        for suffix in (
            "target_pid_set_constructed_privately", "on_screen_omission_confirmed",
            "all_windows_nonactionable",
        )
    ),
    *(
        f"selection_{source}_{suffix}"
        for source in ("swift", "quartz", "system_events")
        for suffix in (
            "inventory_observed", "inventory_contract_valid", "pid_match_available",
            "bundle_match_available", "on_screen_only_filter_applied",
            "layer_zero_filter_applied",
        )
    ),
)
_SELECTION_INVENTORY_COUNT_CAPS = {
    "selection_nsworkspace_target_process_match_count": 4,
    "selection_inventory_cause_count": 4,
    "selection_observation_index": 2,
    "selection_observation_count": 2,
    **{
        f"selection_{source}_{suffix}": cap
        for source in ("swift", "quartz", "system_events")
        for suffix, cap in (
            ("window_total_count", 64),
            ("usable_window_count", 64),
            ("target_name_match_count", 8),
            ("target_pid_match_count", 8),
            ("target_bundle_match_count", 8),
        )
    },
    **{
        f"selection_{source}_{suffix}": cap
        for source in ("swift", "quartz")
        for suffix, cap in (
            ("owner_name_present_count", 64),
            ("window_name_present_count", 64),
            ("raw_target_pid_match_count", 8),
            ("raw_target_bundle_match_count", 8),
            ("all_windows_target_pid_match_count", 8),
            ("target_rejected_not_on_screen_count", 8),
            ("target_rejected_nonzero_layer_count", 8),
            ("target_rejected_invalid_identity_count", 8),
            ("target_rejected_nonpositive_geometry_count", 8),
            ("rejected_target_pid_mismatch_count", 64),
            ("rejected_target_bundle_mismatch_count", 8),
        )
    },
    "selection_permission_fact_change_count": 4,
}
_SELECTION_INVENTORY_ENUMS = {
    "selection_activation_policy": "not_requested",
    "selection_swift_helper_response_contract": "valid_success",
    "selection_swift_helper_binary_class": "isolated_reused_current",
    "selection_swift_helper_contract_version_class": "expected",
    "selection_inventory_source_used": "swift_host",
    "selection_authoritative_permission_source": "swift_host",
    "selection_inventory_diagnostic_stage": "complete",
    "selection_inventory_diagnostic_outcome": "primary_source_divergence",
    "selection_reobservation_outcome": "not_recovered",
    "selection_swift_execution_component": "swift_helper",
    "selection_quartz_execution_component": "isolated_python_runtime",
    "selection_system_events_execution_component": "system_events_child",
    "selection_swift_helper_signing_class": "ad_hoc",
    "selection_swift_helper_persistence_class": "reused_current",
    "selection_swift_helper_path_stability": "same",
    "selection_swift_helper_signature_stability": "same",
    "selection_codex_permission_comparison": "not_observable",
    "selection_swift_ax_trust": "not_trusted",
    "selection_quartz_ax_trust": "trusted",
    "selection_swift_ax_target_probe_outcome": "skipped_not_trusted",
    "selection_quartz_ax_target_probe_outcome": "no_value",
    "selection_system_events_automation_preflight": "would_require_consent",
    "selection_system_events_execution_outcome": "skipped_non_authoritative",
    "selection_swift_screen_capture_preflight": "denied",
    "selection_quartz_screen_capture_preflight": "granted",
    "selection_swift_cg_on_screen_query_outcome": "success_nonempty",
    "selection_swift_cg_all_windows_query_outcome": "success_empty",
    "selection_quartz_cg_on_screen_query_outcome": "success_nonempty",
    "selection_quartz_cg_all_windows_query_outcome": "nil_or_unavailable",
    "selection_permission_diagnostic_outcome": "multiple",
    "selection_authoritative_permission_outcome": "permissions_ok",
    "selection_secondary_permission_outcome": "skipped_non_authoritative",
    "selection_permission_fact_stability": "stable",
}


def _selection_inventory_failure() -> dict[str, object]:
    return {
        "action": "computer.select_window",
        "selected": False,
        "is_error": True,
        "error_code": "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED",
        "selection_failure_stage": "app_match",
        "selection_exact_binding_required": True,
        "selection_exact_binding_present": False,
        **{
            key: index % 2 == 0
            for index, key in enumerate(_SELECTION_INVENTORY_BOOL_FIELDS)
        },
        **{
            key: min(index + 1, cap)
            for index, (key, cap) in enumerate(_SELECTION_INVENTORY_COUNT_CAPS.items())
        },
        **_SELECTION_INVENTORY_ENUMS,
        "raw_apps": [{"name": "PRIVATE_APP", "pid": 424242}],
        "raw_windows": [{"title": "PRIVATE_TITLE", "window_id": 313131}],
        "helper_path": "/private/PRIVATE_HELPER",
        "bundle_id": "PRIVATE_BUNDLE",
        "frame": {"x": 99},
        "signing_identity": "PRIVATE_SIGNING_IDENTITY",
        "signature_hash": "PRIVATE_SIGNATURE_HASH",
        "tcc_error": "PRIVATE_TCC_ERROR",
        "ax_error_message": "PRIVATE_AX_ERROR",
        "cg_error_message": "PRIVATE_CG_ERROR",
        "system_events_stderr": "PRIVATE_SYSTEM_EVENTS_ERROR",
    }


def test_generic_selection_keeps_weak_window_compatibility(tmp_path):
    controller = _controller(tmp_path, [_window(pid=None, window_id=None)])

    result = controller.run(
        "computer.select_window",
        {"app": "ChatGPT Atlas", "focus": False},
        yolo_mode=True,
    )

    assert result["selected"] is True
    assert result["selection_exact_binding_required"] is False
    assert result["selection_exact_binding_present"] is False


@pytest.mark.parametrize("missing", ["pid", "window_id"])
def test_exact_selection_rejects_missing_identifier_before_state_write(tmp_path, missing):
    weak = _window()
    weak.pop(missing)
    controller = _controller(tmp_path, [weak])

    result = _select(controller)

    assert result["selected"] is False
    assert result["error_code"] == "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"
    assert result[f"selection_{missing}_present"] is False
    assert "target_window" not in controller._computer_state()


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("x", 10.5, "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"),
        ("height", "800.25", "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"),
        ("width", 0, "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND"),
        ("height", -1, "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND"),
    ],
)
def test_exact_selection_rejects_non_integral_or_non_positive_geometry(
    tmp_path, field, value, expected_code
):
    controller = _controller(tmp_path, [_window(**{field: value})])

    result = _select(controller)

    assert result["selected"] is False
    assert result["error_code"] == expected_code
    assert "target_window" not in controller._computer_state()


@pytest.mark.parametrize("missing", ["x", "width"])
def test_exact_selection_rejects_missing_geometry(tmp_path, missing):
    incomplete = _window()
    incomplete.pop(missing)
    controller = _controller(tmp_path, [incomplete])

    result = _select(controller)

    assert result["selected"] is False
    assert result["selection_geometry_complete"] is False
    assert "target_window" not in controller._computer_state()


def test_exact_selection_rejects_selected_app_that_does_not_match_request(tmp_path):
    controller = _controller(tmp_path, [])

    result = _select(controller, window=_window(app="ChatGPT"))

    assert result["error_code"] == "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"
    assert result["selection_app_verified"] is False
    assert result["selection_exact_binding_present"] is False


def test_exact_selection_reports_app_and_usable_window_failure_stages(tmp_path):
    missing_app = _select(_controller(tmp_path / "missing", [_window(app="Codex")]))
    unusable = _select(_controller(tmp_path / "small", [_window(width=100, height=80)]))

    assert missing_app["error_code"] == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
    assert missing_app["selection_failure_stage"] == "window_observation"
    assert unusable["error_code"] == "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND"
    assert unusable["selection_matched_app"] is True
    assert unusable["selection_failure_stage"] == "window_match"


def test_exact_selection_reports_secondary_source_divergence_without_selecting_it(tmp_path):
    controller = _controller(tmp_path, [])
    primary = [_window(app="Codex", pid=11, window_id=12)]
    controller._darwin_window_inventory_observation = lambda app: {
        "windows": primary,
        "facts": {
            "selection_inventory_source_used": "swift_host",
            "selection_primary_source_nonempty": True,
            "selection_diagnostic_sources_compared": True,
            "selection_primary_source_target_match_absent": True,
            "selection_later_source_target_match_present": True,
            "selection_primary_source_suppressed_target_observation": True,
            "selection_inventory_instrumentation_consistent": True,
            "selection_inventory_diagnostic_stage": "complete",
            "selection_inventory_diagnostic_outcome": "primary_source_divergence",
            "selection_inventory_cause_count": 1,
        },
    }

    result = _select(controller)

    assert result["selected"] is False
    assert result["error_code"] == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
    assert result["selection_inventory_source_used"] == "swift_host"
    assert result["selection_primary_source_suppressed_target_observation"] is True
    assert result["selection_window_owner_alias_matched"] is False
    assert "target_window" not in result
    assert "target_window" not in controller._computer_state()


def test_inventory_comparison_preserves_nonempty_swift_authority(tmp_path, monkeypatch):
    controller = BrowserComputerController(artifact_root=tmp_path)
    swift_windows = [_window(app="Codex", pid=11, window_id=12)]
    secondary_windows = [_window(app="ChatGPT Atlas", pid=321, window_id=654)]
    monkeypatch.setattr(
        controller,
        "_darwin_swift_inventory_observation",
        lambda **kwargs: {
            "windows": swift_windows,
            "target_pids": {321},
            "bundle_pids": {321},
            "facts": {
                "selection_swift_helper_available": True,
                "selection_swift_helper_invoked": True,
                "selection_swift_helper_response_contract": "valid_success",
                "selection_swift_helper_binary_class": "isolated_reused_current",
                "selection_swift_helper_contract_version_class": "expected",
                "selection_swift_helper_compile_attempted": False,
                "selection_swift_helper_compile_succeeded": False,
                "selection_native_snapshot_atomic": True,
                "selection_nsworkspace_observation_completed": True,
                "selection_nsworkspace_target_process_present": True,
                "selection_nsworkspace_localized_name_match": True,
                "selection_nsworkspace_bundle_id_match": True,
                "selection_nsworkspace_target_process_match_count": 1,
                "selection_target_pid_match_available": True,
                "selection_target_bundle_match_available": True,
                **controller._selection_source_facts(
                    "swift", swift_windows, app="chatgpt atlas",
                    observed=True, contract_valid=True, total_count=1,
                    target_pids={321}, bundle_pids={321},
                    pid_match_available=True, bundle_match_available=True,
                    on_screen_only=True, layer_zero=True,
                ),
            },
        },
    )
    monkeypatch.setattr(
        controller, "_darwin_windows_quartz", lambda: secondary_windows,
    )
    monkeypatch.setattr(
        controller, "_darwin_quartz_permission_observation",
        lambda **kwargs: {
            "selection_quartz_cg_on_screen_query_outcome": "success_nonempty",
            "selection_quartz_cg_all_windows_query_outcome": "success_nonempty",
            "selection_quartz_ax_trust": "trusted",
            "selection_quartz_screen_capture_preflight": "granted",
            "selection_permission_request_api_invoked": False,
        },
    )
    monkeypatch.setattr(
        controller, "_darwin_system_events_permission_observation",
        lambda **kwargs: {
            "windows": [],
            "facts": {
                "selection_system_events_automation_preflight": "authorized",
                "selection_system_events_execution_outcome": "skipped_non_authoritative",
                "selection_permission_request_api_invoked": False,
            },
        },
    )

    observation = controller._darwin_window_inventory_observation("chatgpt atlas")

    assert observation["windows"] == swift_windows
    assert observation["facts"]["selection_inventory_source_used"] == "swift_host"
    assert observation["facts"]["selection_primary_source_target_match_absent"] is True
    assert observation["facts"]["selection_later_source_target_match_present"] is True
    assert observation["facts"]["selection_primary_source_suppressed_target_observation"] is True
    assert observation["facts"]["selection_inventory_diagnostic_outcome"] == "primary_source_divergence"


def test_inventory_skips_system_events_enumeration_when_authoritative_source_is_nonempty(
    tmp_path, monkeypatch
):
    controller = BrowserComputerController(artifact_root=tmp_path)
    swift_windows = [_window(app="Codex", pid=11, window_id=12)]
    system_calls = []
    monkeypatch.setattr(
        controller,
        "_darwin_swift_inventory_observation",
        lambda **kwargs: {
            "windows": swift_windows,
            "target_pids": set(),
            "bundle_pids": set(),
            "facts": {
                "selection_swift_helper_response_contract": "valid_success",
                "selection_swift_helper_contract_version_class": "expected",
                "selection_swift_ax_trust": "trusted",
                "selection_swift_screen_capture_preflight": "granted",
                "selection_swift_cg_on_screen_query_outcome": "success_nonempty",
                "selection_swift_cg_all_windows_query_outcome": "success_nonempty",
                **controller._selection_source_facts(
                    "swift", swift_windows, app="chatgpt atlas", observed=True,
                    contract_valid=True, on_screen_only=True, layer_zero=True,
                ),
            },
        },
    )
    monkeypatch.setattr(controller, "_darwin_windows_quartz", lambda: [])
    monkeypatch.setattr(
        controller,
        "_darwin_quartz_permission_observation",
        lambda **kwargs: {
            "selection_quartz_ax_trust": "trusted",
            "selection_quartz_screen_capture_preflight": "granted",
            "selection_quartz_cg_on_screen_query_outcome": "success_empty",
            "selection_quartz_cg_all_windows_query_outcome": "success_empty",
            "selection_permission_request_api_invoked": False,
        },
    )

    def system_events(**kwargs):
        system_calls.append(kwargs)
        assert kwargs["enumerate_windows"] is False
        return {
            "windows": [],
            "facts": {
                "selection_system_events_automation_preflight": "authorized",
                "selection_system_events_execution_outcome": "skipped_non_authoritative",
                "selection_permission_request_api_invoked": False,
            },
        }

    monkeypatch.setattr(controller, "_darwin_system_events_permission_observation", system_events)

    observation = controller._darwin_window_inventory_observation("chatgpt atlas")

    assert system_calls
    assert observation["windows"] == swift_windows
    facts = observation["facts"]
    assert facts["selection_inventory_source_used"] == "swift_host"
    assert facts["selection_system_events_execution_outcome"] == "skipped_non_authoritative"
    assert facts["selection_authoritative_permission_source"] == "swift_host"
    assert facts["selection_authoritative_permission_outcome"] == "permissions_ok_no_target"
    assert facts["selection_secondary_permission_outcome"] == "permissions_ok_no_target"
    # The legacy global reducer remains informational; it does not reinterpret
    # a deliberately skipped secondary System Events enumeration as success.
    assert facts["selection_permission_diagnostic_outcome"] == "unknown"


def test_system_events_preflight_can_skip_enumeration_without_executing_applescript(tmp_path, monkeypatch):
    controller = BrowserComputerController(artifact_root=tmp_path)
    monkeypatch.setattr(
        controller,
        "_darwin_system_events_automation_preflight",
        lambda: {
            "selection_system_events_execution_component": "system_events_child",
            "selection_system_events_permission_check_colocated": True,
            "selection_permission_request_api_invoked": False,
            "selection_system_events_automation_preflight": "authorized",
            "selection_system_events_execution_outcome": "unknown",
        },
    )
    monkeypatch.setattr(
        controller,
        "_darwin_system_events_enumeration",
        lambda: pytest.fail("System Events enumeration must not run for a non-authoritative source"),
    )

    observation = controller._darwin_system_events_permission_observation(
        app="ChatGPT Atlas", enumerate_windows=False
    )

    assert observation == {
        "windows": [],
        "facts": {
            "selection_system_events_execution_component": "system_events_child",
            "selection_system_events_permission_check_colocated": True,
            "selection_permission_request_api_invoked": False,
            "selection_system_events_automation_preflight": "authorized",
            "selection_system_events_execution_outcome": "skipped_non_authoritative",
        },
    }


def test_quartz_diagnostic_bridge_is_capability_based_bounded_and_aggregate_only(
    tmp_path, monkeypatch, capsys
):
    controller = BrowserComputerController(artifact_root=tmp_path)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.subprocess.run", fake_run
    )

    controller._darwin_quartz_permission_observation(
        app="ChatGPT Atlas", aliases={"chatgptatlas"}, target_pids={321}, bundle_pids={321}
    )

    assert len(commands) == 1
    code = commands[0][-1]
    assert "MAX_BRIDGED_RECORDS = 256" in code
    assert "has_mapping_capability" in code
    assert "mapping_get" in code
    assert "len(value)" in code
    assert "objectAtIndex_" in code
    assert "isinstance(value, (list, tuple))" not in code
    assert "print(json.dumps(facts))" in code
    assert "print(json.dumps(on_records))" not in code
    compile(code, "<quartz-diagnostic-child>", "exec")

    quartz = ModuleType("Quartz")
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGWindowListExcludeDesktopElements = 2
    quartz.kCGWindowListOptionAll = 4
    quartz.kCGNullWindowID = 0
    quartz.CGPreflightScreenCaptureAccess = lambda: True
    quartz.CGWindowListCopyWindowInfo = lambda *_: [
        {
            "kCGWindowOwnerName": "private owner",
            "kCGWindowOwnerPID": 321,
            "kCGWindowNumber": index + 1,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"Width": 10, "Height": 10},
        }
        for index in range(257)
    ]
    application_services = ModuleType("ApplicationServices")
    application_services.AXIsProcessTrusted = lambda: False
    application_services.AXUIElementCreateApplication = lambda *_: None
    application_services.AXUIElementCopyAttributeValue = lambda *_: (0, None)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)

    exec(compile(code, "<quartz-diagnostic-child>", "exec"), {})
    facts = json.loads(capsys.readouterr().out)

    assert facts["selection_quartz_cg_on_screen_query_outcome"] == "success_nonempty_truncated"
    assert facts["selection_quartz_cg_all_windows_query_outcome"] == "success_nonempty_truncated"
    assert facts["selection_quartz_cg_all_windows_records_aggregated_count"] == 256
    assert facts["selection_quartz_all_windows_target_pid_match_count"] == 256
    assert "private owner" not in json.dumps(facts)


def test_quartz_diagnostic_bridge_supports_objc_collections_and_rejects_malformed_canary(
    tmp_path, monkeypatch, capsys
):
    controller = BrowserComputerController(artifact_root=tmp_path)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.subprocess.run", fake_run
    )
    controller._darwin_quartz_permission_observation(
        app="ChatGPT Atlas", aliases={"chatgptatlas"}, target_pids={321}, bundle_pids={321}
    )
    code = commands[0][-1]

    def record(index, *, target=False):
        return {
            "kCGWindowOwnerName": "private owner",
            "kCGWindowOwnerPID": 321 if target else 999,
            "kCGWindowNumber": index + 1,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"Width": 10, "Height": 10},
        }

    class ObjCRecords:
        def __init__(self, items):
            self._items = items

        def count(self):
            return len(self._items)

        def objectAtIndex_(self, index):
            return self._items[index]

        def __iter__(self):
            raise AssertionError("generic iterator must not run for ObjC records")

    quartz = ModuleType("Quartz")
    quartz.kCGWindowListOptionOnScreenOnly = 1
    quartz.kCGWindowListExcludeDesktopElements = 2
    quartz.kCGWindowListOptionAll = 4
    quartz.kCGNullWindowID = 0
    quartz.CGPreflightScreenCaptureAccess = lambda: True
    quartz.CGWindowListCopyWindowInfo = lambda *_: ObjCRecords([record(index) for index in range(257)])
    application_services = ModuleType("ApplicationServices")
    application_services.AXIsProcessTrusted = lambda: True
    application_services.AXUIElementCreateApplication = lambda *_: None
    application_services.AXUIElementCopyAttributeValue = lambda *_: (0, None)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)

    exec(compile(code, "<quartz-diagnostic-child>", "exec"), {})
    facts = json.loads(capsys.readouterr().out)

    assert facts["selection_quartz_cg_all_windows_query_outcome"] == "success_nonempty_truncated"
    assert facts["selection_quartz_cg_all_windows_records_aggregated_count"] == 256
    assert facts["selection_quartz_all_windows_target_pid_match_count"] == 0
    assert BrowserComputerController._selection_source_permission_outcome(facts, "quartz") == (
        "permissions_ok_target_unknown"
    )
    assert "private owner" not in json.dumps(facts)

    class CanaryRecords:
        def __iter__(self):
            yield from (record(index) for index in range(256))
            yield object()

    quartz.CGWindowListCopyWindowInfo = lambda *_: CanaryRecords()
    exec(compile(code, "<quartz-diagnostic-child>", "exec"), {})
    malformed_facts = json.loads(capsys.readouterr().out)

    assert malformed_facts["selection_quartz_cg_on_screen_query_outcome"] == "invalid_payload"
    assert malformed_facts["selection_quartz_cg_all_windows_query_outcome"] == "invalid_payload"
    assert malformed_facts["selection_quartz_cg_all_windows_records_aggregated_count"] == 0


def test_quartz_diagnostic_bridge_sanitizes_truncation_aggregate_count(tmp_path, monkeypatch):
    controller = BrowserComputerController(artifact_root=tmp_path)
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({
            "selection_quartz_cg_all_windows_query_outcome": "success_nonempty_truncated",
            "selection_quartz_cg_all_windows_records_aggregated_count": 999,
            "selection_quartz_cg_on_screen_query_outcome": "not_a_closed_outcome",
        })),
    )

    facts = controller._darwin_quartz_permission_observation(
        app="ChatGPT Atlas", aliases={"chatgptatlas"}, target_pids={321}, bundle_pids={321}
    )

    assert facts["selection_quartz_cg_all_windows_query_outcome"] == "success_nonempty_truncated"
    assert facts["selection_quartz_cg_all_windows_records_aggregated_count"] == 256
    assert facts["selection_quartz_cg_on_screen_query_outcome"] == "nil_or_unavailable"


def test_truncated_quartz_permission_reducers_preserve_system_events_skip_visibility():
    facts = {
        "selection_permission_request_api_invoked": False,
        "selection_swift_ax_trust": "trusted",
        "selection_swift_screen_capture_preflight": "granted",
        "selection_swift_cg_on_screen_query_outcome": "success_empty",
        "selection_swift_cg_all_windows_query_outcome": "success_empty",
        "selection_quartz_ax_trust": "trusted",
        "selection_quartz_screen_capture_preflight": "granted",
        "selection_quartz_cg_on_screen_query_outcome": "success_nonempty",
        "selection_quartz_cg_all_windows_query_outcome": "success_nonempty_truncated",
        "selection_quartz_all_windows_target_pid_match_count": 0,
        "selection_system_events_automation_preflight": "authorized",
        "selection_system_events_execution_outcome": "success",
    }

    assert BrowserComputerController._selection_source_permission_outcome(facts, "quartz") == (
        "permissions_ok_target_unknown"
    )
    assert BrowserComputerController._selection_permission_outcome(facts) == "permissions_ok_target_unknown"
    facts["selection_system_events_execution_outcome"] = "skipped_non_authoritative"
    assert BrowserComputerController._selection_permission_outcome(facts) == "unknown"
    assert BrowserComputerController._selection_secondary_permission_outcome(
        facts, ["quartz", "system_events"]
    ) == "permissions_ok_target_unknown"
    assert facts["selection_system_events_execution_outcome"] == "skipped_non_authoritative"


def test_exact_selection_rejects_activation_policy_before_focus(tmp_path, monkeypatch):
    controller = _controller(tmp_path, [_window()])
    focused = []
    monkeypatch.setattr(controller, "_focus_window", lambda value: focused.append(value))

    result = controller.run(
        "computer.select_window",
        {"app": "ChatGPT Atlas", "focus": True, "require_exact_binding": True},
        yolo_mode=True,
    )

    assert result["selected"] is False
    assert result["selection_activation_policy"] == "invalid_requested"
    assert result["selection_focus_requested"] is True
    assert result["selection_focus_attempted"] is False
    assert result["selection_failure_stage"] == "activation_policy"
    assert focused == []


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"selection_swift_ax_trust": "not_trusted"}, "accessibility_denied"),
        ({"selection_quartz_screen_capture_preflight": "denied"}, "screen_capture_denied"),
        ({"selection_system_events_automation_preflight": "would_require_consent"}, "system_events_denied"),
        ({"selection_swift_on_screen_omission_confirmed": True}, "on_screen_filter_exclusion"),
        ({"selection_quartz_target_rejected_nonzero_layer_count": 1}, "layer_filter_exclusion"),
        ({"selection_swift_target_rejected_nonpositive_geometry_count": 1}, "geometry_filter_exclusion"),
        ({"selection_quartz_rejected_target_bundle_mismatch_count": 1}, "identity_correlation_failure"),
        (
            {"selection_swift_ax_trust": "not_trusted", "selection_quartz_screen_capture_preflight": "denied"},
            "multiple",
        ),
    ],
)
def test_permission_reducer_uses_closed_diagnostic_causes(updates, expected):
    facts = {
        "selection_permission_request_api_invoked": False,
        "selection_swift_ax_trust": "trusted",
        "selection_quartz_ax_trust": "trusted",
        "selection_swift_screen_capture_preflight": "granted",
        "selection_quartz_screen_capture_preflight": "granted",
        "selection_system_events_automation_preflight": "authorized",
        "selection_system_events_execution_outcome": "success",
        "selection_swift_cg_on_screen_query_outcome": "success_nonempty",
        "selection_swift_cg_all_windows_query_outcome": "success_nonempty",
        "selection_quartz_cg_on_screen_query_outcome": "success_nonempty",
        "selection_quartz_cg_all_windows_query_outcome": "success_nonempty",
    }
    facts.update(updates)

    assert BrowserComputerController._selection_permission_outcome(facts) == expected


def test_permission_reducer_reports_fully_observed_no_target():
    facts = {
        "selection_permission_request_api_invoked": False,
        "selection_swift_ax_trust": "trusted",
        "selection_quartz_ax_trust": "trusted",
        "selection_swift_screen_capture_preflight": "granted",
        "selection_quartz_screen_capture_preflight": "granted",
        "selection_system_events_automation_preflight": "authorized",
        "selection_system_events_execution_outcome": "success",
        "selection_swift_cg_on_screen_query_outcome": "success_empty",
        "selection_swift_cg_all_windows_query_outcome": "success_empty",
        "selection_quartz_cg_on_screen_query_outcome": "success_empty",
        "selection_quartz_cg_all_windows_query_outcome": "success_empty",
        "selection_swift_all_windows_target_pid_match_count": 0,
        "selection_quartz_all_windows_target_pid_match_count": 0,
    }

    assert BrowserComputerController._selection_permission_outcome(facts) == "permissions_ok_no_target"


def test_source_permission_reducers_keep_authoritative_and_secondary_states_separate():
    facts = {
        "selection_permission_request_api_invoked": False,
        "selection_swift_ax_trust": "trusted",
        "selection_swift_screen_capture_preflight": "granted",
        "selection_swift_cg_on_screen_query_outcome": "success_empty",
        "selection_swift_cg_all_windows_query_outcome": "success_empty",
        "selection_quartz_ax_trust": "trusted",
        "selection_quartz_screen_capture_preflight": "denied",
        "selection_quartz_cg_on_screen_query_outcome": "success_empty",
        "selection_quartz_cg_all_windows_query_outcome": "success_empty",
        "selection_system_events_automation_preflight": "would_require_consent",
        "selection_system_events_execution_outcome": "automation_denied",
    }

    assert BrowserComputerController._selection_permission_outcome(facts) == "multiple"
    assert BrowserComputerController._selection_source_permission_outcome(facts, "swift") == "permissions_ok_no_target"
    assert BrowserComputerController._selection_secondary_permission_outcome(
        facts, ["quartz", "system_events"]
    ) == "multiple"


def test_complete_exact_selection_persists_only_after_validation_and_never_focuses_when_false(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, [_window()])
    focused = []
    monkeypatch.setattr(controller, "_focus_window", lambda value: focused.append(value))

    result = _select(controller)

    assert result["selected"] is True
    assert result["selection_exact_binding_present"] is True
    assert result["selection_app_verified"] is True
    assert result["selection_pid_present"] is True
    assert result["selection_window_id_present"] is True
    assert result["selection_geometry_complete"] is True
    assert result["selection_geometry_integral"] is True
    assert result["selection_focus_requested"] is False
    assert result["selection_focus_attempted"] is False
    assert controller._computer_state()["target_window"]["window_id"] == 654
    assert focused == []


def test_safe_selection_facts_are_root_owned_and_content_free():
    facts = safe_window_selection_facts(
        {
            "action": "computer.select_window",
            "selected": False,
            "selection_exact_binding_required": True,
            "selection_exact_binding_present": False,
            "selection_failure_stage": "exact_binding",
            "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
            "title": "private title",
            "nested": {
                "action": "computer.select_window",
                "selected": True,
                "selection_exact_binding_present": True,
                "target_window": _window(),
            },
        },
        requested_app="ChatGPT Atlas",
    )

    assert facts == {
        "selection_exact_binding_required": True,
        "selection_exact_binding_present": False,
        "selection_selected": False,
        "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
        "selection_failure_stage": "exact_binding",
    }
    assert "private title" not in str(facts)
    assert "321" not in str(facts)
    assert "654" not in str(facts)


def test_selection_inventory_facts_survive_helper_viewer_and_trace_content_free(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    raw = _selection_inventory_failure()
    central = safe_window_selection_facts(raw, requested_app="ChatGPT Atlas")
    helper = _computer_result_envelope(
        "computer.select_window",
        raw,
        request_args={"app": "ChatGPT Atlas", "focus": False, "require_exact_binding": True},
    )
    trace_facts = result_trace_facts(raw)
    trace_event = emit_computer_trace(
        "controller.result", "computer.select_window", **trace_facts
    )
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {
            "ok": False,
            "error": {"code": "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"},
            "result": raw,
        },
    )
    viewer = ViewerBrokerClient(
        url="http://127.0.0.1:8770", token="private-token"
    ).run_computer(
        "computer.select_window",
        {"app": "ChatGPT Atlas", "focus": False, "require_exact_binding": True},
    )

    outputs = (central, helper["result"], trace_facts, trace_event, viewer)
    for output in outputs:
        assert output["error_code"] == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
        for index, key in enumerate(_SELECTION_INVENTORY_BOOL_FIELDS):
            assert output[key] is (index % 2 == 0)
        for index, (key, cap) in enumerate(_SELECTION_INVENTORY_COUNT_CAPS.items()):
            assert output[key] == min(index + 1, cap)
        for key, value in _SELECTION_INVENTORY_ENUMS.items():
            assert output[key] == value
        serialized = str(output)
        for canary in (
            "PRIVATE_APP", "PRIVATE_TITLE", "424242", "313131",
            "PRIVATE_HELPER", "PRIVATE_BUNDLE", "'x': 99", "private-token",
            "PRIVATE_SIGNING_IDENTITY", "PRIVATE_SIGNATURE_HASH", "PRIVATE_TCC_ERROR",
            "PRIVATE_AX_ERROR", "PRIVATE_CG_ERROR", "PRIVATE_SYSTEM_EVENTS_ERROR",
        ):
            assert canary not in serialized


def test_selection_inventory_counts_clamp_and_unknown_enums_drop():
    raw = _selection_inventory_failure()
    raw.update({key: 10_000 for key in _SELECTION_INVENTORY_COUNT_CAPS})
    raw.update({key: f"PRIVATE_{key}" for key in _SELECTION_INVENTORY_ENUMS})

    outputs = (
        safe_window_selection_facts(raw, requested_app="ChatGPT Atlas"),
        _computer_result_envelope(
            "computer.select_window",
            raw,
            request_args={"app": "ChatGPT Atlas", "focus": False, "require_exact_binding": True},
        )["result"],
        result_trace_facts(raw),
    )
    for output in outputs:
        for key, cap in _SELECTION_INVENTORY_COUNT_CAPS.items():
            assert output[key] == cap
        for key in _SELECTION_INVENTORY_ENUMS:
            assert key not in output
        assert "PRIVATE_selection" not in str(output)


def test_helper_maps_exact_failure_to_protocol_failure_with_safe_facts_only(tmp_path):
    controller = _controller(tmp_path, [_window(pid=None)])
    controller_result = _select(controller)

    envelope = _computer_result_envelope(
        "computer.select_window",
        controller_result,
        request_args={"app": "ChatGPT Atlas", "require_exact_binding": True},
    )

    assert envelope["ok"] is False
    assert envelope["error_code"] == "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"
    assert envelope["result"]["selection_exact_binding_required"] is True
    assert envelope["result"]["selection_exact_binding_present"] is False
    assert "target_window" not in envelope["result"]
    assert "private title" not in str(envelope)


def test_helper_independently_rejects_claimed_exact_binding_with_invalid_root_target():
    claimed = {
        "action": "computer.select_window",
        "selected": True,
        "selection_exact_binding_required": True,
        "selection_exact_binding_present": True,
        "target_window": _window(pid=None),
    }

    envelope = _computer_result_envelope(
        "computer.select_window",
        claimed,
        request_args={"app": "ChatGPT Atlas", "require_exact_binding": True},
    )

    assert envelope["ok"] is False
    assert envelope["error_code"] == "SELECT_WINDOW_RESULT_INVALID"
    assert envelope["result"]["selection_pid_present"] is False


def test_helper_preserves_complete_exact_binding_on_success(tmp_path):
    result = _select(_controller(tmp_path, [_window()]))

    envelope = _computer_result_envelope(
        "computer.select_window",
        result,
        request_args={"app": "ChatGPT Atlas", "require_exact_binding": True},
    )

    assert envelope == {"ok": True, "result": result}
    assert envelope["result"]["target_window"]["pid"] == 321


def test_trace_uses_selection_contract_for_result_ok_and_drops_target_content():
    generic_failure = result_trace_facts({"action": "computer.select_window", "selected": False})
    exact_success = result_trace_facts(
        {
            "action": "computer.select_window",
            "selected": True,
            "selection_exact_binding_required": True,
            "selection_exact_binding_present": True,
            "selection_failure_stage": "none",
            "target_window": _window(),
        }
    )
    invalid_exact = result_trace_facts(
        {
            "action": "computer.select_window",
            "selected": True,
            "selection_exact_binding_required": True,
            "selection_exact_binding_present": False,
            "target_window": _window(title="trace private"),
        }
    )

    assert generic_failure["result_ok"] is False
    assert exact_success["result_ok"] is True
    assert invalid_exact["result_ok"] is False
    assert "trace private" not in str(invalid_exact)
    assert "321" not in str(invalid_exact)
    assert "654" not in str(invalid_exact)


def test_selection_trace_emits_only_fixed_failure_values(monkeypatch, tmp_path):
    trace_path = tmp_path / "selection.jsonl"
    monkeypatch.setenv("RUMI_COMPUTER_USE_TRACE_PATH", str(trace_path))

    with computer_action_trace("computer.select_window"):
        event = emit_computer_trace(
            "controller.result",
            "computer.select_window",
            result_ok=False,
            selection_selected=False,
            selection_exact_binding_required=True,
            selection_exact_binding_present=False,
            selection_failure_stage="private-stage",
            error_code="PRIVATE_ERROR",
            title="private title",
            pid=321,
            window_id=654,
        )

    assert event["result_ok"] is False
    assert event["selection_selected"] is False
    assert event["selection_exact_binding_required"] is True
    assert event["selection_exact_binding_present"] is False
    assert "selection_failure_stage" not in event
    assert "error_code" not in event
    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records
    for record in records:
        assert "selection_failure_stage" not in record
        assert "error_code" not in record
        assert "title" not in record
        assert "pid" not in record
        assert "window_id" not in record


def test_viewer_client_propagates_safe_exact_selection_failure(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    def fake_request(self, method, route, payload=None):
        return {
            "ok": False,
            "audit_id": "host-audit-safe",
            "error": {"code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"},
            "result": {
                "action": "computer.select_window",
                "selection_matched_app": True,
                "selection_matched_window": True,
                "selection_selected": False,
                "selection_exact_binding_required": True,
                "selection_exact_binding_present": False,
                "selection_pid_present": False,
                "selection_failure_stage": "exact_binding",
                "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
                "target_window": _window(title="client private"),
            },
        }

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    result = ViewerBrokerClient(url="http://127.0.0.1:8770", token="private-token").run_computer(
        "computer.select_window",
        {"app": "ChatGPT Atlas", "focus": False, "require_exact_binding": True},
    )

    assert result["is_error"] is True
    assert result["error_code"] == "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"
    assert result["selection_exact_binding_required"] is True
    assert result["selection_exact_binding_present"] is False
    assert "target_window" not in result
    assert "client private" not in str(result)
    assert "private-token" not in str(result)


def test_viewer_client_preserves_successful_root_exact_binding(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    target = _window()

    def fake_request(self, method, route, payload=None):
        return {
            "ok": True,
            "audit_id": "host-audit-safe",
            "result": {
                "action": "computer.select_window",
                "selected": True,
                "selection_exact_binding_required": True,
                "selection_exact_binding_present": True,
                "target_window": target,
            },
        }

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    result = ViewerBrokerClient(url="http://127.0.0.1:8770", token="private-token").run_computer(
        "computer.select_window",
        {"app": "ChatGPT Atlas", "focus": False, "require_exact_binding": True},
    )

    assert result["target_window"] == target
    assert result["selected"] is True


def test_pack_widget_preserves_safe_exact_selection_failure(monkeypatch):
    from ecosystem.defaultspack.blocks.tool import browser_computer as pack_block

    safe_failure = {
        "action": "computer.select_window",
        "is_error": True,
        "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
        "selection_matched_app": True,
        "selection_matched_window": True,
        "selection_selected": False,
        "selection_exact_binding_required": True,
        "selection_exact_binding_present": False,
        "selection_pid_present": False,
        "selection_failure_stage": "exact_binding",
    }
    monkeypatch.setattr(pack_block, "run_computer_action", lambda *args, **kwargs: safe_failure)

    packed = pack_block.run(
        {
            "action": "computer.select_window",
            "payload": {
                "app": "ChatGPT Atlas",
                "focus": False,
                "require_exact_binding": True,
            },
        }
    )

    assert packed["status"] == "ok"
    assert packed["data"]["widget"] == safe_failure
    assert packed["data"]["widget"]["selection_exact_binding_present"] is False


def _selected_identity_inventory(*records, marker="rumi.mac.selected_window_identity.v1"):
    return {
        "selected_window_identity_diagnostic_contract": marker,
        "windows": list(records),
    }


def _identity_window(**overrides):
    value = _window()
    value.update({
        "_rumi_owner_alias_match": True,
        "_rumi_target_process_match": True,
        "_rumi_target_bundle_match": True,
    })
    value.update(overrides)
    return value


def test_selected_identity_correlates_only_the_exact_selected_binding_and_scrubs_private_fields():
    inventory = _selected_identity_inventory(
        _identity_window(pid=1, window_id=2, _rumi_target_process_match=False,
                         _rumi_target_bundle_match=False),
        _identity_window(pid=321, window_id=654),
    )
    windows, observation = BrowserComputerController._selected_window_identity_inventory(inventory)
    facts = BrowserComputerController._selected_window_identity_facts(observation, _window())

    assert facts == {
        "selection_selected_identity_contract_valid": True,
        "selection_selected_identity_available": True,
        "selection_selected_owner_alias_match": True,
        "selection_selected_target_process_match": True,
        "selection_selected_target_bundle_match": True,
        "selection_selected_identity_class": "bundle_process_match",
    }
    assert all(not any(str(key).startswith("_rumi_") for key in window) for window in windows)
    assert "_rumi_" not in json.dumps(windows)


@pytest.mark.parametrize(
    ("record", "expected_contract_valid"),
    [
        (_identity_window(_rumi_target_process_match="true"), False),
        (_identity_window(_rumi_target_process_match=False, _rumi_target_bundle_match=True), False),
    ],
)
def test_selected_identity_malformed_or_impossible_native_flags_fail_closed(record, expected_contract_valid):
    windows, observation = BrowserComputerController._selected_window_identity_inventory(
        _selected_identity_inventory(record)
    )
    facts = BrowserComputerController._selected_window_identity_facts(observation, _window())

    assert facts["selection_selected_identity_contract_valid"] is expected_contract_valid
    assert facts["selection_selected_identity_available"] is False
    assert facts["selection_selected_identity_class"] == "unavailable"
    assert "_rumi_" not in json.dumps(windows)


@pytest.mark.parametrize(
    ("flags", "identity_class"),
    [
        ((True, True, False), "process_match"),
        ((True, False, False), "owner_name_only"),
        ((False, False, False), "no_match"),
    ],
)
def test_selected_identity_closed_classes_do_not_change_exact_selection_success(
    tmp_path, monkeypatch, flags, identity_class
):
    record = _identity_window(
        _rumi_owner_alias_match=flags[0],
        _rumi_target_process_match=flags[1],
        _rumi_target_bundle_match=flags[2],
    )
    windows, identity = BrowserComputerController._selected_window_identity_inventory(
        _selected_identity_inventory(record)
    )
    controller = _controller(tmp_path, windows)
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.platform.system",
        lambda: "Darwin",
    )
    controller._darwin_window_inventory_observation = lambda _app: {
        "windows": windows,
        "facts": {},
        "_selected_identity_observation": identity,
    }

    result = _select(controller)

    assert result["selected"] is True
    assert result["selection_selected_identity_class"] == identity_class
    assert result["selection_selected_identity_available"] is True
    assert "_rumi_" not in json.dumps(result)
    assert "_rumi_" not in json.dumps(controller._computer_state())


def test_selected_identity_marker_and_safe_transport_are_closed_and_content_free():
    missing_windows, missing_observation = BrowserComputerController._selected_window_identity_inventory(
        _selected_identity_inventory(_identity_window(), marker="PRIVATE_MARKER")
    )
    missing = BrowserComputerController._selected_window_identity_facts(
        missing_observation, _window()
    )
    raw = {
        "action": "computer.select_window",
        "selected": True,
        **missing,
        "selection_selected_identity_class": "PRIVATE_CLASS",
        "target_window": _window(title="PRIVATE_TITLE"),
        "_rumi_owner_alias_match": True,
    }
    safe = safe_window_selection_facts(raw, requested_app="ChatGPT Atlas")
    traced = result_trace_facts(raw)

    assert missing["selection_selected_identity_contract_valid"] is False
    assert missing["selection_selected_identity_available"] is False
    for output in (safe, traced):
        assert output["selection_selected_identity_contract_valid"] is False
        assert output["selection_selected_identity_available"] is False
        assert "selection_selected_identity_class" not in output
        assert "PRIVATE" not in json.dumps(output)
        assert "_rumi_" not in json.dumps(output)


def _mock_inventory_reducer_sources(controller, *, swift, quartz_windows=(), system_observation=None):
    """Mock only lower observation sources so reducer behavior stays under test."""
    controller._darwin_swift_inventory_observation = lambda **_kwargs: swift
    controller._darwin_windows_quartz = lambda: list(quartz_windows)
    controller._darwin_quartz_permission_observation = lambda **_kwargs: {}
    controller._darwin_system_events_permission_observation = lambda **_kwargs: (
        system_observation or {"windows": [], "facts": {}}
    )


def _swift_identity_source(*, record=None, marker="rumi.mac.selected_window_identity.v1"):
    windows, identity = BrowserComputerController._selected_window_identity_inventory(
        _selected_identity_inventory(record or _identity_window(), marker=marker)
    )
    return {
        "windows": windows,
        "facts": {
            "selection_swift_helper_response_contract": "valid_success",
            "selection_swift_helper_contract_version_class": "expected",
            "selection_swift_inventory_contract_valid": True,
        },
        "target_pids": {321},
        "bundle_pids": {321},
        "_selected_identity_observation": identity,
    }


def test_inventory_reducer_relays_only_swift_authoritative_private_identity_map(tmp_path):
    controller = _controller(tmp_path, [])
    swift = _swift_identity_source()
    _mock_inventory_reducer_sources(controller, swift=swift)

    observation = controller._darwin_window_inventory_observation("ChatGPT Atlas")

    assert observation["facts"]["selection_inventory_source_used"] == "swift_host"
    assert observation["_selected_identity_observation"] is swift["_selected_identity_observation"]
    assert all(
        not any(str(key).startswith("_rumi_") for key in record)
        for record in observation["windows"]
    )


@pytest.mark.parametrize(
    ("quartz_windows", "system_observation", "expected_source"),
    [
        ([_window()], {"windows": [], "facts": {}}, "quartz"),
        ([], {"windows": [_window()], "facts": {"selection_system_events_execution_outcome": "success"}}, "system_events"),
    ],
)
def test_inventory_reducer_does_not_relay_swift_identity_to_quartz_or_system_events(
    tmp_path, quartz_windows, system_observation, expected_source
):
    controller = _controller(tmp_path, [])
    swift = _swift_identity_source()
    swift["windows"] = []
    _mock_inventory_reducer_sources(
        controller,
        swift=swift,
        quartz_windows=quartz_windows,
        system_observation=system_observation,
    )

    observation = controller._darwin_window_inventory_observation("ChatGPT Atlas")

    assert observation["facts"]["selection_inventory_source_used"] == expected_source
    assert observation["_selected_identity_observation"] is None


def test_select_window_correlates_swift_identity_through_real_inventory_reducer(tmp_path, monkeypatch):
    controller = _controller(tmp_path, [])
    del controller._list_windows
    swift = _swift_identity_source()
    _mock_inventory_reducer_sources(controller, swift=swift)
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.platform.system",
        lambda: "Darwin",
    )

    result = _select(controller)

    assert result["selected"] is True
    assert {
        key: result[key]
        for key in (
            "selection_selected_identity_contract_valid",
            "selection_selected_identity_available",
            "selection_selected_owner_alias_match",
            "selection_selected_target_process_match",
            "selection_selected_target_bundle_match",
            "selection_selected_identity_class",
        )
    } == {
        "selection_selected_identity_contract_valid": True,
        "selection_selected_identity_available": True,
        "selection_selected_owner_alias_match": True,
        "selection_selected_target_process_match": True,
        "selection_selected_target_bundle_match": True,
        "selection_selected_identity_class": "bundle_process_match",
    }
    assert "_rumi_" not in json.dumps(result)


def test_select_window_invalid_swift_identity_marker_reports_explicit_unavailable_fields(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, [])
    del controller._list_windows
    swift = _swift_identity_source(marker="invalid")
    _mock_inventory_reducer_sources(controller, swift=swift)
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.tool.browser_computer.platform.system",
        lambda: "Darwin",
    )

    result = _select(controller)

    assert result["selected"] is True
    assert {
        key: result[key]
        for key in (
            "selection_selected_identity_contract_valid",
            "selection_selected_identity_available",
            "selection_selected_owner_alias_match",
            "selection_selected_target_process_match",
            "selection_selected_target_bundle_match",
            "selection_selected_identity_class",
        )
    } == {
        "selection_selected_identity_contract_valid": False,
        "selection_selected_identity_available": False,
        "selection_selected_owner_alias_match": False,
        "selection_selected_target_process_match": False,
        "selection_selected_target_bundle_match": False,
        "selection_selected_identity_class": "unavailable",
    }


def test_system_events_success_returns_only_public_windows_and_facts(tmp_path):
    controller = _controller(tmp_path, [])
    controller._darwin_system_events_automation_preflight = lambda: {
        "selection_system_events_automation_preflight": "authorized"
    }
    controller._darwin_system_events_enumeration = lambda: {
        "execution_outcome": "success",
        "output": "ChatGPT Atlas\tprivate title\t10\t20\t1200\t800\tfalse\t321\n",
    }

    observation = controller._darwin_system_events_permission_observation(
        app="ChatGPT Atlas"
    )

    assert set(observation) == {"windows", "facts"}
    assert observation["facts"]["selection_system_events_execution_outcome"] == "success"
    assert observation["windows"] == [{
        "app": "ChatGPT Atlas",
        "title": "private title",
        "pid": 321,
        "x": 10,
        "y": 20,
        "width": 1200,
        "height": 800,
        "active": False,
    }]
