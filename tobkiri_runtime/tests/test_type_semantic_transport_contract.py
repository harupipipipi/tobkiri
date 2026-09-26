from __future__ import annotations

import json

import pytest

from core_runtime.host_broker.computer_delivery import SAFE_TYPE_PREDISPATCH_ERROR_CODES

pytestmark = pytest.mark.contract


def _probe_result(*, ready: bool, stage: str, code: str = "") -> dict[str, object]:
    diagnostics = {
        "probe_completed": True,
        "semantic_control_ready": ready,
        "input_dispatched": False,
        "mutation_attempted": False,
        "semantic_discovery_stage": stage,
        "semantic_traversal_order": "breadth_first",
        "semantic_window_scan_complete": stage != "scan_incomplete",
        "semantic_window_scan_truncated": stage == "scan_incomplete",
        "semantic_window_nodes_visited_count": 999,
        "semantic_window_max_depth_reached": 999,
        "semantic_forbidden_subtree_pruned_count": 999,
        "semantic_unlisted_role_class": "multiple",
        "saw_unlisted_static_value_class": True,
        "semantic_actionable_counts_truncated": False,
        "semantic_app_diagnostic_counts_truncated": True,
        "semantic_app_diagnostic_stage": "scan_incomplete",
        "semantic_app_diagnostic_scope": "application_tree_owned",
        "semantic_app_diagnostic_ownership_proof": "multiple",
        "semantic_unlisted_value_settable_count": 999,
        "semantic_unlisted_selected_text_settable_count": 3,
        "semantic_unlisted_selected_range_settable_count": 2,
        "semantic_unlisted_focus_settable_count": 1,
        "semantic_unlisted_attribute_capability_known_count": 4,
        "semantic_unlisted_under_toolbar_count": 5,
        "semantic_unlisted_relation_scan_complete": True,
        "semantic_unlisted_related_allowed_role_count": 999,
        "semantic_unlisted_relation_kind": "linked_relation",
        "semantic_app_diagnostic_raw_role": "CANARY_PRIVATE_ROLE",
        "title": "CANARY_PRIVATE_TITLE",
        "value": "CANARY_PRIVATE_VALUE",
        "pid": 99123,
        "window_id": 88,
        "geometry": {"x": 1},
        "path": "/private/canary",
        "raw_error": "CANARY_RAW_ERROR",
    }
    if code:
        diagnostics["error_code"] = code
    return {
        "action": "computer.probe_text_control",
        "executed": True,
        "probe_completed": True,
        "semantic_control_ready": ready,
        "input_dispatched": False,
        "mutation_attempted": False,
        "diagnostics": diagnostics,
    }


def _type_failure(*, dispatched, code: str) -> dict[str, object]:
    diagnostics = {
        "input_dispatched": dispatched,
        "completion_verified": False,
        "error_code": code,
        "semantic_nodes_visited_count": 999,
        "semantic_final_candidate_count": 99,
        "semantic_counts_truncated": False,
        "semantic_scan_scope": "exact_window_descendants",
        "semantic_discovery_stage": "role_absent",
        "semantic_coordinate_status": "window_frame_matched",
        "semantic_ownership_proof": "window_descendant",
        "saw_ax_text_field": False,
        "title": "CANARY_PRIVATE_TITLE",
        "pid": 99123,
        "geometry": {"x": 1, "y": 2},
    }
    return {
        "action": "computer.type",
        "executed": False,
        "is_error": True,
        "diagnostics": diagnostics,
        "error": "CANARY_RAW_ERROR",
    }


def test_helper_preserves_allowlisted_predispatch_semantic_code_and_bounded_facts():
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    envelope = _computer_result_envelope(
        "computer.type",
        _type_failure(dispatched=False, code="TYPE_SEMANTIC_CONTROL_NOT_FOUND"),
    )

    assert envelope["ok"] is False
    assert envelope["error_code"] == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert envelope["error"] == "Computer text-input precondition failed."
    diagnostics = envelope["diagnostics"]
    assert diagnostics["input_dispatched"] is False
    assert diagnostics["semantic_nodes_visited_count"] == 255
    assert diagnostics["semantic_final_candidate_count"] == 8
    assert diagnostics["semantic_counts_truncated"] is True
    assert "CANARY" not in json.dumps(envelope)
    assert "title" not in diagnostics
    assert "pid" not in diagnostics
    assert "geometry" not in diagnostics


def test_helper_dispatched_unverified_normalizes_to_completion_not_verified():
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    envelope = _computer_result_envelope(
        "computer.type",
        _type_failure(dispatched=True, code="TYPE_SEMANTIC_CONTROL_NOT_FOUND"),
    )

    assert envelope["error_code"] == "TYPE_COMPLETION_NOT_VERIFIED"
    assert envelope["result"]["input_dispatched"] is True


def test_repeated_stale_branch_code_is_safe_only_before_dispatch():
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    code = "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    assert code in SAFE_TYPE_PREDISPATCH_ERROR_CODES

    predispatch = _computer_result_envelope(
        "computer.type", _type_failure(dispatched=False, code=code)
    )
    dispatched = _computer_result_envelope(
        "computer.type", _type_failure(dispatched=True, code=code)
    )
    lookalike = _computer_result_envelope(
        "computer.type", _type_failure(dispatched=False, code=f"{code}_LOOKALIKE")
    )

    assert predispatch["error_code"] == code
    assert predispatch["error"] == "Computer text-input precondition failed."
    assert dispatched["error_code"] == "TYPE_COMPLETION_NOT_VERIFIED"
    assert dispatched["result"]["input_dispatched"] is True
    assert lookalike["error_code"] == "TYPE_DIAGNOSTICS_INVALID"
    assert "LOOKALIKE" not in json.dumps(lookalike)


@pytest.mark.parametrize(
    "code",
    ["TYPE_ACCESSIBILITY_API_UNAVAILABLE", "TYPE_SEMANTIC_PROTOCOL_INVALID"],
)
def test_new_closed_ax_failure_codes_are_safe_only_before_dispatch(code):
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    assert code in SAFE_TYPE_PREDISPATCH_ERROR_CODES
    predispatch = _computer_result_envelope(
        "computer.type", _type_failure(dispatched=False, code=code)
    )
    dispatched = _computer_result_envelope(
        "computer.type", _type_failure(dispatched=True, code=code)
    )

    assert predispatch["error_code"] == code
    assert dispatched["error_code"] == "TYPE_COMPLETION_NOT_VERIFIED"


@pytest.mark.parametrize("dispatched", [False, None])
def test_helper_unknown_or_missing_diagnostics_fail_closed(dispatched):
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    envelope = _computer_result_envelope(
        "computer.type", _type_failure(dispatched=dispatched, code="CANARY_ARBITRARY_CODE")
    )

    assert envelope["error_code"] == "TYPE_DIAGNOSTICS_INVALID"
    assert envelope["error"] == "Computer text-input diagnostics were invalid."
    assert "CANARY" not in json.dumps(envelope)
    assert "error_code" not in envelope["diagnostics"]


@pytest.mark.parametrize("code", sorted(SAFE_TYPE_PREDISPATCH_ERROR_CODES))
def test_viewer_client_preserves_each_safe_predispatch_code(monkeypatch, code):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret")
    failure = _type_failure(dispatched=False, code=code)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *args, **kwargs: {
            "ok": False,
            "error": {"code": code, "message": "CANARY_RAW_ERROR"},
            "result": failure,
            "diagnostics": failure["diagnostics"],
        },
    )

    result = client.run_computer("computer.type", {})

    assert result["is_error"] is True
    expected = (
        "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
        if code == "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE"
        else code
    )
    assert result["error_code"] == expected
    assert result["reason"] == "Computer text-input precondition failed."
    assert result["diagnostics"]["input_dispatched"] is False
    assert result["diagnostics"]["semantic_nodes_visited_count"] == 255
    assert "CANARY" not in json.dumps(result)


def test_trace_extracts_only_bounded_semantic_diagnostics():
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts

    code = "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE"
    facts = result_trace_facts(
        _type_failure(dispatched=False, code=code)
    )

    assert facts["semantic_nodes_visited_count"] == 255
    assert facts["semantic_final_candidate_count"] == 8
    assert facts["semantic_scan_scope"] == "exact_window_descendants"
    assert facts["semantic_discovery_stage"] == "role_absent"
    assert facts["error_code"] == "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    assert "CANARY" not in json.dumps(facts)


@pytest.mark.parametrize(
    "code",
    ["TYPE_ACCESSIBILITY_API_UNAVAILABLE", "TYPE_SEMANTIC_PROTOCOL_INVALID"],
)
def test_trace_preserves_each_new_closed_ax_failure_code(code):
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts

    facts = result_trace_facts(_type_failure(dispatched=False, code=code))

    assert facts["error_code"] == code


def test_helper_treats_not_ready_probe_as_valid_content_free_result():
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    envelope = _computer_result_envelope(
        "computer.probe_text_control",
        _probe_result(
            ready=False,
            stage="scan_incomplete",
            code="TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
        ),
    )

    assert envelope["ok"] is True
    result = envelope["result"]
    assert result["executed"] is True
    assert result["probe_completed"] is True
    assert result["semantic_control_ready"] is False
    assert result["input_dispatched"] is False
    assert result["mutation_attempted"] is False
    assert result["error_code"] == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    diagnostics = result["diagnostics"]
    assert diagnostics["semantic_window_nodes_visited_count"] == 255
    assert diagnostics["semantic_window_max_depth_reached"] == 20
    assert diagnostics["semantic_forbidden_subtree_pruned_count"] == 64
    assert diagnostics["semantic_counts_truncated"] is True
    assert diagnostics["semantic_unlisted_role_class"] == "multiple"
    assert diagnostics["saw_unlisted_static_value_class"] is True
    assert diagnostics["semantic_actionable_counts_truncated"] is False
    assert diagnostics["semantic_app_diagnostic_counts_truncated"] is True
    assert diagnostics["semantic_app_diagnostic_stage"] == "scan_incomplete"
    assert diagnostics["semantic_app_diagnostic_scope"] == "application_tree_owned"
    assert diagnostics["semantic_app_diagnostic_ownership_proof"] == "multiple"
    assert diagnostics["semantic_unlisted_value_settable_count"] == 64
    assert diagnostics["semantic_unlisted_related_allowed_role_count"] == 64
    assert diagnostics["semantic_unlisted_relation_scan_complete"] is True
    assert diagnostics["semantic_unlisted_relation_kind"] == "linked_relation"
    assert "semantic_app_diagnostic_raw_role" not in diagnostics
    assert "CANARY" not in json.dumps(envelope)
    for key in ("title", "value", "pid", "window_id", "geometry", "path", "raw_error"):
        assert key not in diagnostics


def test_helper_probe_protocol_failure_is_not_reported_as_valid_diagnostic_result():
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope

    raw = _probe_result(ready=False, stage="role_absent")
    raw["diagnostics"].pop("mutation_attempted")
    raw.pop("mutation_attempted")
    envelope = _computer_result_envelope("computer.probe_text_control", raw)

    assert envelope["ok"] is False
    assert envelope["error_code"] == "TYPE_DIAGNOSTICS_INVALID"
    assert envelope["result"]["probe_completed"] is False
    assert "CANARY" not in json.dumps(envelope)


def test_trace_preserves_probe_contract_without_private_content():
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts

    facts = result_trace_facts(
        _probe_result(
            ready=False,
            stage="scan_incomplete",
            code="TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
        )
    )

    assert facts["probe_completed"] is True
    assert facts["semantic_control_ready"] is False
    assert facts["mutation_attempted"] is False
    assert facts["semantic_window_nodes_visited_count"] == 255
    assert facts["semantic_window_max_depth_reached"] == 20
    assert facts["semantic_discovery_stage"] == "scan_incomplete"
    assert facts["semantic_app_diagnostic_stage"] == "scan_incomplete"
    assert facts["semantic_app_diagnostic_scope"] == "application_tree_owned"
    assert facts["semantic_app_diagnostic_ownership_proof"] == "multiple"
    assert facts["semantic_actionable_counts_truncated"] is False
    assert facts["semantic_app_diagnostic_counts_truncated"] is True
    assert facts["semantic_unlisted_value_settable_count"] == 64
    assert facts["semantic_unlisted_relation_scan_complete"] is True
    assert facts["semantic_unlisted_related_allowed_role_count"] == 64
    assert facts["semantic_unlisted_relation_kind"] == "linked_relation"
    assert facts["error_code"] == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    assert "CANARY" not in json.dumps(facts)


def test_viewer_client_sanitizes_valid_not_ready_probe_result(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    raw = _probe_result(
        ready=False,
        stage="role_absent",
        code="TYPE_SEMANTIC_CONTROL_NOT_FOUND",
    )
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "audit_id": "host-audit-probe", "result": raw},
    )

    result = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )

    assert result["executed"] is True
    assert result["probe_completed"] is True
    assert result["semantic_control_ready"] is False
    assert result.get("is_error") is not True
    assert result["host_audit_id"] == "host-audit-probe"
    assert result["diagnostics"]["semantic_discovery_stage"] == "role_absent"
    assert "CANARY" not in json.dumps(result)


def test_viewer_client_preserves_not_ready_persistent_stale_subtree_probe(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    code = "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE"
    raw = _probe_result(ready=False, stage="scan_incomplete", code=code)
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "result": raw},
    )

    result = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )

    assert result["executed"] is True
    assert result["probe_completed"] is True
    assert result["semantic_control_ready"] is False
    assert result.get("is_error") is not True
    assert result["input_dispatched"] is False
    assert result["mutation_attempted"] is False
    assert result["error_code"] == "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    assert result["diagnostics"]["error_code"] == "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    assert "CANARY" not in json.dumps(result)


_CHILD_COUNT_FIELDS = (
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
)
_CHILD_BOOL_FIELDS = (
    "semantic_children_failure_on_window_root",
    "semantic_children_failure_under_toolbar",
    "semantic_children_attribute_advertised",
    "semantic_children_count_known",
    "semantic_children_count_nonzero",
    "semantic_children_branch_proven_empty",
)


def _probe_with_child_diagnostics() -> dict[str, object]:
    result = _probe_result(
        ready=False,
        stage="scan_incomplete",
        code="TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
    )
    diagnostics = result["diagnostics"]
    diagnostics.update({key: index + 1 for index, key in enumerate(_CHILD_COUNT_FIELDS)})
    diagnostics.update({key: index % 2 == 0 for index, key in enumerate(_CHILD_BOOL_FIELDS)})
    diagnostics.update({
        "semantic_children_failure_class": "multiple",
        "semantic_children_incomplete_branch_class": "static_value",
        "semantic_children_ax_error_class": "not_implemented",
        "semantic_children_structural_empty_proof": "attribute_not_advertised",
        "semantic_stale_branch_scope": "candidate_node",
        "semantic_stale_node_self_eligible": True,
        "semantic_stale_node_class": "text_control",
        "accessibility_trust_preflight": "granted",
        "ax_error_description": "CANARY_AX_ERROR",
        "role": "CANARY_AX_ROLE",
        "value": "CANARY_VALUE",
        "label": "CANARY_LABEL",
        "title": "CANARY_TITLE",
        "pid": 99123,
        "window_id": 88123,
        "geometry": {"x": 1, "y": 2},
        "path": "/private/CANARY_PATH",
    })
    return result


def test_child_read_diagnostics_survive_all_safe_transport_extractors(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_with_child_diagnostics()
    diagnostics = raw["diagnostics"]
    controller_safe = BrowserComputerController._safe_semantic_text_diagnostics(
        {"data": {"diagnostics": diagnostics}}
    )
    delivery_safe = safe_type_diagnostic_facts(raw)
    helper = _computer_result_envelope("computer.probe_text_control", raw)
    trace_safe = result_trace_facts(raw)
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "result": raw},
    )
    viewer = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )
    outputs = (
        controller_safe,
        delivery_safe,
        helper["result"]["diagnostics"],
        trace_safe,
        viewer["diagnostics"],
    )

    assert helper["ok"] is True
    for output in outputs:
        for index, key in enumerate(_CHILD_COUNT_FIELDS):
            assert output[key] == index + 1
        for index, key in enumerate(_CHILD_BOOL_FIELDS):
            assert output[key] is (index % 2 == 0)
        assert output["semantic_children_failure_class"] == "multiple"
        assert output["semantic_children_incomplete_branch_class"] == "static_value"
        assert output["semantic_children_ax_error_class"] == "not_implemented"
        assert output["semantic_children_structural_empty_proof"] == "attribute_not_advertised"
        assert output["semantic_stale_branch_scope"] == "candidate_node"
        assert output["semantic_stale_node_self_eligible"] is True
        assert output["semantic_stale_node_class"] == "text_control"
        assert output["accessibility_trust_preflight"] == "granted"
        serialized = json.dumps(output)
        assert "CANARY" not in serialized
        for key in (
            "ax_error_description", "role", "value", "label", "title", "pid",
            "window_id", "geometry", "path",
        ):
            assert key not in output


def test_child_read_diagnostic_counts_clamp_and_unknown_enums_drop(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_with_child_diagnostics()
    diagnostics = raw["diagnostics"]
    diagnostics["semantic_children_read_success_count"] = 999
    diagnostics["semantic_children_failure_class"] = "CANARY_RAW_AX_ERROR"
    diagnostics["semantic_children_incomplete_branch_class"] = "AXToolbar"

    delivery_safe = safe_type_diagnostic_facts(raw)
    controller_safe = BrowserComputerController._safe_semantic_text_diagnostics(
        {"data": {"diagnostics": diagnostics}}
    )
    helper_safe = _computer_result_envelope("computer.probe_text_control", raw)["result"]["diagnostics"]
    trace_safe = result_trace_facts(raw)
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "result": raw},
    )
    viewer_safe = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )["diagnostics"]

    for output in (delivery_safe, controller_safe, helper_safe, trace_safe, viewer_safe):
        assert output["semantic_children_read_success_count"] == 64
        assert output["semantic_counts_truncated"] is True
        assert "semantic_children_failure_class" not in output
        assert "semantic_children_incomplete_branch_class" not in output
        assert "CANARY" not in json.dumps(output)
        assert "AXToolbar" not in json.dumps(output)


_NAVIGATION_ORDER_COUNT_CAPS = {
    "semantic_navigation_order_fallback_attempted_count": 8,
    "semantic_navigation_order_fallback_succeeded_count": 8,
    "semantic_navigation_order_recovered_invalid_count": 8,
    "semantic_navigation_order_page_read_count": 16,
}


def test_navigation_order_fallback_facts_survive_safe_transport_without_bypassing_readiness(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.blocks.tool.browser_computer import _model_facing_result
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_result(
        ready=False, stage="scan_incomplete", code="TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    )
    diagnostics = raw["diagnostics"]
    diagnostics.update({
        "semantic_navigation_order_fallback_attempted_count": 1,
        "semantic_navigation_order_fallback_succeeded_count": 1,
        "semantic_navigation_order_recovered_invalid_count": 1,
        "semantic_navigation_order_page_read_count": 1,
        "semantic_navigation_order_fallback_outcome": "complete_children",
        "semantic_navigation_order_failure_class": "none",
        "semantic_navigation_order_ax_error_class": "invalid_element",
        "semantic_navigation_order_cardinality_class": "nine_to_64",
        "semantic_navigation_order_parent_proof": "all_direct",
        "semantic_navigation_order_count_stable": True,
        "semantic_navigation_order_complete": True,
        "semantic_actionable_scan_complete": False,
        "semantic_control_ready": False,
        "semantic_navigation_order_raw_children": ["CANARY_AX_REFERENCE"],
        "semantic_navigation_order_private_path": "/private/CANARY_PATH",
    })
    controller = BrowserComputerController._safe_semantic_text_diagnostics(
        {"data": {"diagnostics": diagnostics}}
    )
    central = safe_type_diagnostic_facts(raw)
    helper = _computer_result_envelope("computer.probe_text_control", raw)["result"]["diagnostics"]
    trace = result_trace_facts(raw)
    monkeypatch.setattr(ViewerBrokerClient, "_request", lambda *args, **kwargs: {"ok": True, "result": raw})
    viewer = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )["diagnostics"]
    pack = _model_facing_result("computer.probe_text_control", {"diagnostics": viewer})["diagnostics"]

    for output in (controller, central, helper, trace, viewer, pack):
        assert output["semantic_navigation_order_fallback_attempted_count"] == 1
        assert output["semantic_navigation_order_fallback_succeeded_count"] == 1
        assert output["semantic_navigation_order_recovered_invalid_count"] == 1
        assert output["semantic_navigation_order_page_read_count"] == 1
        assert output["semantic_navigation_order_fallback_outcome"] == "complete_children"
        assert output["semantic_navigation_order_failure_class"] == "none"
        assert output["semantic_navigation_order_ax_error_class"] == "invalid_element"
        assert output["semantic_navigation_order_cardinality_class"] == "nine_to_64"
        assert output["semantic_navigation_order_parent_proof"] == "all_direct"
        assert output["semantic_navigation_order_count_stable"] is True
        assert output["semantic_navigation_order_complete"] is True
        assert output["semantic_actionable_scan_complete"] is False
        assert output["semantic_control_ready"] is False
        assert "CANARY" not in json.dumps(output)
        assert "raw_children" not in output
        assert "private_path" not in output


def test_navigation_order_fallback_counts_clamp_and_unknown_enums_drop(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_result(
        ready=False, stage="scan_incomplete", code="TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    )
    diagnostics = raw["diagnostics"]
    diagnostics.update({key: 999 for key in _NAVIGATION_ORDER_COUNT_CAPS})
    for key in (
        "semantic_navigation_order_fallback_outcome",
        "semantic_navigation_order_failure_class",
        "semantic_navigation_order_ax_error_class",
        "semantic_navigation_order_cardinality_class",
        "semantic_navigation_order_parent_proof",
    ):
        diagnostics[key] = "CANARY_UNKNOWN_ENUM"
    outputs = [
        BrowserComputerController._safe_semantic_text_diagnostics(
            {"data": {"diagnostics": diagnostics}}
        ),
        safe_type_diagnostic_facts(raw),
        _computer_result_envelope("computer.probe_text_control", raw)["result"]["diagnostics"],
        result_trace_facts(raw),
    ]
    monkeypatch.setattr(ViewerBrokerClient, "_request", lambda *args, **kwargs: {"ok": True, "result": raw})
    outputs.append(
        ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
            "computer.probe_text_control", {"target_control": "browser_address"}
        )["diagnostics"]
    )

    for output in outputs:
        for key, cap in _NAVIGATION_ORDER_COUNT_CAPS.items():
            assert output[key] == cap
        assert output["semantic_counts_truncated"] is True
        assert "CANARY" not in json.dumps(output)
        assert all(key not in output for key in (
            "semantic_navigation_order_fallback_outcome",
            "semantic_navigation_order_failure_class",
            "semantic_navigation_order_ax_error_class",
            "semantic_navigation_order_cardinality_class",
            "semantic_navigation_order_parent_proof",
        ))


def test_allowed_role_geometry_diagnostics_are_bounded_closed_and_content_free(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_result(ready=False, stage="role_absent", code="TYPE_SEMANTIC_CONTROL_NOT_FOUND")
    diagnostics = raw["diagnostics"]
    count_fields = (
        "semantic_allowed_ax_text_field_count",
        "semantic_allowed_ax_combo_box_count",
        "semantic_allowed_ax_text_area_count",
        "semantic_allowed_frame_inside_window_count",
        "semantic_allowed_region_x_match_count",
        "semantic_allowed_region_y_match_count",
    )
    diagnostics.update({key: 999 for key in count_fields})
    diagnostics.update({
        "semantic_allowed_role_class": "multiple",
        "semantic_allowed_region_miss_axis": "outside_window",
        "semantic_allowed_center_y_band": "upper_22_35",
        "semantic_allowed_width_band": "near_full_80_100",
        "semantic_allowed_height_band": "tall_40_100",
        "semantic_allowed_private_frame": {"x": 99123},
        "semantic_allowed_private_label": "CANARY_PRIVATE_LABEL",
    })
    controller = BrowserComputerController._safe_semantic_text_diagnostics(
        {"data": {"diagnostics": diagnostics}}
    )
    delivery = safe_type_diagnostic_facts(raw)
    helper = _computer_result_envelope("computer.probe_text_control", raw)["result"]["diagnostics"]
    trace = result_trace_facts(raw)
    monkeypatch.setattr(ViewerBrokerClient, "_request", lambda *args, **kwargs: {"ok": True, "result": raw})
    viewer = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )["diagnostics"]

    for output in (controller, delivery, helper, trace, viewer):
        assert {key: output[key] for key in count_fields} == {key: 8 for key in count_fields}
        assert output["semantic_counts_truncated"] is True
        assert output["semantic_allowed_role_class"] == "multiple"
        assert output["semantic_allowed_region_miss_axis"] == "outside_window"
        assert output["semantic_allowed_center_y_band"] == "upper_22_35"
        assert output["semantic_allowed_width_band"] == "near_full_80_100"
        assert output["semantic_allowed_height_band"] == "tall_40_100"
        assert "CANARY" not in json.dumps(output)
        assert "frame" not in output

    diagnostics.update({
        "semantic_allowed_ax_text_field_count": True,
        "semantic_allowed_region_miss_axis": "CANARY_UNKNOWN_AXIS",
        "semantic_allowed_center_y_band": 22,
    })
    invalid = safe_type_diagnostic_facts(raw)
    assert "semantic_allowed_ax_text_field_count" not in invalid
    assert "semantic_allowed_region_miss_axis" not in invalid
    assert "semantic_allowed_center_y_band" not in invalid
    assert "CANARY" not in json.dumps(invalid)


_STALE_RECOVERY_BOOL_FIELDS = (
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
)
_STALE_RECOVERY_COUNT_CAPS = {
    "semantic_stale_parent_refresh_count": 1,
    "semantic_stale_parent_refresh_read_count": 2,
    "semantic_stale_additional_ax_read_count": 64,
    "semantic_discovery_pass_count": 3,
    "semantic_stale_recovery_restart_count": 2,
    "semantic_first_pass_stale_count": 64,
    "semantic_second_pass_stale_count": 64,
    "semantic_first_pass_unknown_branch_count": 64,
    "semantic_second_pass_unknown_branch_count": 64,
    "semantic_first_pass_nodes_visited_count": 255,
    "semantic_second_pass_nodes_visited_count": 255,
    "semantic_second_pass_final_candidate_count": 8,
    "semantic_third_pass_stale_count": 64,
    "semantic_third_pass_unknown_branch_count": 64,
    "semantic_third_pass_nodes_visited_count": 255,
    "semantic_third_pass_final_candidate_count": 8,
}


def _probe_with_stale_recovery_diagnostics() -> dict[str, object]:
    result = _probe_result(ready=True, stage="ready")
    diagnostics = result["diagnostics"]
    diagnostics.update({key: index % 2 == 0 for index, key in enumerate(_STALE_RECOVERY_BOOL_FIELDS)})
    diagnostics.update({
        "semantic_stale_parent_refresh_count": 1,
        "semantic_stale_parent_refresh_read_count": 2,
        "semantic_stale_additional_ax_read_count": 6,
        "semantic_discovery_pass_count": 3,
        "semantic_stale_recovery_restart_count": 2,
        "semantic_first_pass_stale_count": 3,
        "semantic_second_pass_stale_count": 0,
        "semantic_first_pass_unknown_branch_count": 4,
        "semantic_second_pass_unknown_branch_count": 0,
        "semantic_first_pass_nodes_visited_count": 31,
        "semantic_second_pass_nodes_visited_count": 37,
        "semantic_second_pass_final_candidate_count": 1,
        "semantic_third_pass_stale_count": 0,
        "semantic_third_pass_unknown_branch_count": 0,
        "semantic_third_pass_nodes_visited_count": 45,
        "semantic_third_pass_final_candidate_count": 1,
        "semantic_stale_reference_refresh_class": "same_stale_reference_returned",
        "semantic_stale_branch_comparison": "same_class_and_depth",
        "semantic_second_third_stale_reference_class": "same_parent_new_reference",
        "semantic_stale_recovery_outcome": "recovered_after_parent_refresh",
        "element_identity": "CANARY_IDENTITY",
        "role": "CANARY_ROLE",
        "value": "CANARY_VALUE",
        "label": "CANARY_LABEL",
        "title": "CANARY_TITLE",
        "pid": 99123,
        "window_id": 88123,
        "geometry": {"x": 1, "y": 2},
        "path": "/private/CANARY_PATH",
        "raw_error": "CANARY_RAW_ERROR",
    })
    return result


def test_stale_recovery_diagnostics_survive_existing_safe_transport_and_pack(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.blocks.tool.browser_computer import _model_facing_result
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_with_stale_recovery_diagnostics()
    diagnostics = raw["diagnostics"]
    controller_safe = BrowserComputerController._safe_semantic_text_diagnostics(
        {"data": {"diagnostics": diagnostics}}
    )
    central_safe = safe_type_diagnostic_facts(raw)
    helper = _computer_result_envelope("computer.probe_text_control", raw)
    trace_safe = result_trace_facts(raw)
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "result": raw},
    )
    viewer = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )
    pack_safe = _model_facing_result("computer.probe_text_control", viewer)["diagnostics"]
    outputs = (
        controller_safe,
        central_safe,
        helper["result"]["diagnostics"],
        trace_safe,
        viewer["diagnostics"],
        pack_safe,
    )

    assert helper["ok"] is True
    for output in outputs:
        for index, key in enumerate(_STALE_RECOVERY_BOOL_FIELDS):
            assert output[key] is (index % 2 == 0)
        for key, expected in {
            "semantic_stale_parent_refresh_count": 1,
            "semantic_stale_parent_refresh_read_count": 2,
            "semantic_stale_additional_ax_read_count": 6,
            "semantic_discovery_pass_count": 3,
            "semantic_stale_recovery_restart_count": 2,
            "semantic_first_pass_stale_count": 3,
            "semantic_second_pass_stale_count": 0,
            "semantic_first_pass_unknown_branch_count": 4,
            "semantic_second_pass_unknown_branch_count": 0,
            "semantic_first_pass_nodes_visited_count": 31,
            "semantic_second_pass_nodes_visited_count": 37,
            "semantic_second_pass_final_candidate_count": 1,
            "semantic_third_pass_stale_count": 0,
            "semantic_third_pass_unknown_branch_count": 0,
            "semantic_third_pass_nodes_visited_count": 45,
            "semantic_third_pass_final_candidate_count": 1,
        }.items():
            assert output[key] == expected
        assert output["semantic_stale_reference_refresh_class"] == "same_stale_reference_returned"
        assert output["semantic_stale_branch_comparison"] == "same_class_and_depth"
        assert output["semantic_second_third_stale_reference_class"] == "same_parent_new_reference"
        assert output["semantic_stale_recovery_outcome"] == "recovered_after_parent_refresh"
        serialized = json.dumps(output)
        assert "CANARY" not in serialized
        for key in (
            "element_identity", "role", "value", "label", "title", "pid", "window_id",
            "geometry", "path", "raw_error",
        ):
            assert key not in output


def test_stale_recovery_counts_clamp_and_unknown_outcome_drops(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_with_stale_recovery_diagnostics()
    diagnostics = raw["diagnostics"]
    diagnostics.update({key: 999 for key in _STALE_RECOVERY_COUNT_CAPS})
    diagnostics["semantic_stale_recovery_outcome"] = "CANARY_UNKNOWN_OUTCOME"
    diagnostics["semantic_stale_reference_refresh_class"] = "CANARY_RAW_REFERENCE"
    diagnostics["semantic_stale_branch_comparison"] = "CANARY_RAW_BRANCH"
    diagnostics["semantic_second_third_stale_reference_class"] = "CANARY_RAW_SECOND_THIRD_REFERENCE"
    controller_safe = BrowserComputerController._safe_semantic_text_diagnostics(
        {"data": {"diagnostics": diagnostics}}
    )
    central_safe = safe_type_diagnostic_facts(raw)
    helper_safe = _computer_result_envelope("computer.probe_text_control", raw)["result"]["diagnostics"]
    trace_safe = result_trace_facts(raw)
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "result": raw},
    )
    viewer_safe = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )["diagnostics"]

    for output in (controller_safe, central_safe, helper_safe, trace_safe, viewer_safe):
        for key, cap in _STALE_RECOVERY_COUNT_CAPS.items():
            assert output[key] == cap
        assert output["semantic_counts_truncated"] is True
        assert "semantic_stale_recovery_outcome" not in output
        assert "semantic_stale_reference_refresh_class" not in output
        assert "semantic_stale_branch_comparison" not in output
        assert "semantic_second_third_stale_reference_class" not in output
        assert "CANARY" not in json.dumps(output)


def test_exact_window_resolution_facts_are_closed_bounded_and_content_free():
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import _safe_type_diagnostics
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    diagnostics = {
        "error_code": "TYPE_EXACT_WINDOW_AX_MATCH_AMBIGUOUS",
        "failure_stage": "exact_window_resolution",
        "exact_binding_input_valid": True,
        "exact_running_app_present": True,
        "exact_quartz_query_completed": True,
        "exact_quartz_record_present": True,
        "exact_quartz_owner_matches": True,
        "exact_quartz_layer_allowed": True,
        "exact_quartz_visible": True,
        "exact_quartz_frame_matches": True,
        "exact_ax_windows_attribute_available": True,
        "exact_ax_windows_payload_valid": True,
        "exact_ax_windows_read_completed": True,
        "exact_ax_match_present": True,
        "exact_ax_match_unique": False,
        "exact_window_resolved": False,
        "exact_resolution_retry_attempted": True,
        "exact_resolution_retry_recovered": False,
        "native_frontmost_check_completed": True,
        "native_target_non_frontmost_before": True,
        "native_target_non_frontmost_after": True,
        "native_frontmost_unchanged": True,
        "exact_resolution_attempt_count": 99,
        "exact_quartz_record_match_count": 99,
        "exact_ax_window_count": 99,
        "exact_ax_frame_valid_count": 99,
        "exact_ax_frame_match_count": 99,
        "exact_resolution_stage": "ax_window_match",
        "exact_resolution_outcome": "ax_match_ambiguous",
        "ax_windows_outcome": "success",
        "pid": 99123,
        "window_id": 88,
        "frame": {"x": 1},
        "title": "CANARY_PRIVATE_TITLE",
        "raw_ax_error": "CANARY_RAW_ERROR",
    }
    raw = {
        "action": "computer.probe_text_control", "executed": False, "is_error": True,
        "diagnostics": diagnostics,
    }
    outputs = (
        safe_type_diagnostic_facts(raw),
        _safe_type_diagnostics(raw),
        result_trace_facts(raw),
        BrowserComputerController._safe_semantic_text_diagnostics({"data": {"diagnostics": diagnostics}}),
        _computer_result_envelope("computer.probe_text_control", raw)["diagnostics"],
    )
    expected_caps = {
        "exact_resolution_attempt_count": 2,
        "exact_quartz_record_match_count": 2,
        "exact_ax_window_count": 16,
        "exact_ax_frame_valid_count": 16,
        "exact_ax_frame_match_count": 8,
    }
    for output in outputs:
        assert output["error_code"] == "TYPE_EXACT_WINDOW_AX_MATCH_AMBIGUOUS"
        assert output["exact_resolution_stage"] == "ax_window_match"
        assert output["exact_resolution_outcome"] == "ax_match_ambiguous"
        assert output["ax_windows_outcome"] == "success"
        for key, cap in expected_caps.items():
            assert output[key] == cap
        assert output["semantic_counts_truncated"] is True
        assert "CANARY" not in json.dumps(output)
        for key in ("pid", "window_id", "frame", "title", "raw_ax_error"):
            assert key not in output


def test_exact_window_unknown_code_and_enums_are_dropped():
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts

    safe = safe_type_diagnostic_facts({"diagnostics": {
        "error_code": "TYPE_EXACT_WINDOW_CANARY",
        "exact_resolution_stage": "CANARY_STAGE",
        "exact_resolution_outcome": "CANARY_OUTCOME",
        "ax_windows_outcome": "CANARY_AX",
    }})
    assert safe == {}


_EXPOSURE_BOOL_FIELDS = (
    "semantic_exposure_probe_performed",
    "semantic_exposure_probe_complete",
    "semantic_exposure_probe_truncated",
    "semantic_alt_contents_advertised",
    "semantic_alt_visible_children_advertised",
    "semantic_alt_navigation_order_advertised",
    "semantic_alt_shared_text_advertised",
    "semantic_alt_focused_element_present",
    "semantic_alt_focused_element_exact_owned",
    "semantic_alt_focused_element_non_web",
    "semantic_alt_focused_element_allowed_role",
    "semantic_alt_search_predicate_advertised",
    "semantic_alt_text_marker_relation_advertised",
    "semantic_alt_allowed_role_found",
    "semantic_alt_full_eligibility_found",
    "semantic_exposure_global_node_limit_hit",
    "semantic_exposure_global_read_limit_hit",
    "semantic_exposure_count_saturated",
)
_EXPOSURE_COUNT_CAPS = {
    "semantic_exposure_nodes_visited_count": 64,
    "semantic_exposure_edge_reads_count": 128,
    "semantic_exposure_edge_read_failure_count": 16,
    "semantic_exposure_exact_owned_count": 64,
    "semantic_exposure_non_web_count": 64,
    "semantic_exposure_allowed_role_count": 8,
    "semantic_exposure_full_eligibility_count": 8,
    "semantic_exposure_shared_text_relation_count": 8,
    "semantic_exposure_parameterized_capability_count": 8,
    "semantic_exposure_page_control_count": 8,
    "semantic_exposure_incomplete_cause_count": 8,
    "semantic_exposure_edge_fanout_truncated_count": 16,
    "semantic_exposure_depth_limit_new_target_count": 16,
    "semantic_exposure_depth_limit_queued_target_count": 16,
    "semantic_exposure_queue_remainder_count": 64,
    "semantic_exposure_payload_missing_count": 16,
    "semantic_exposure_payload_invalid_count": 16,
    "semantic_exposure_payload_mixed_count": 16,
    "semantic_exposure_attribute_inventory_unknown_count": 16,
    "semantic_exposure_parameterized_inventory_unknown_count": 5,
    "semantic_exposure_edge_incomplete_without_failure_count": 16,
    "semantic_exposure_node_ownership_rejected_count": 64,
    "semantic_exposure_edge_target_ownership_rejected_count": 64,
}

_EXPOSURE_CAUSE_ENUMS = {
    "semantic_exposure_incomplete_cause": "edge_fanout",
    "semantic_exposure_fanout_source": "navigation_order",
    "semantic_exposure_depth_limit_source": "shared_text",
    "semantic_exposure_focus_cardinality": "multiple",
    "semantic_exposure_count_saturation_class": "edge_reads",
}


def _probe_with_exposure_diagnostics() -> dict[str, object]:
    raw = _probe_result(
        ready=False,
        stage="role_absent",
        code="TYPE_SEMANTIC_CONTROL_NOT_FOUND",
    )
    diagnostics = raw["diagnostics"]
    diagnostics.update(
        {
            **{key: index % 2 == 0 for index, key in enumerate(_EXPOSURE_BOOL_FIELDS)},
            **{key: index + 1 for index, key in enumerate(_EXPOSURE_COUNT_CAPS)},
            "semantic_exposure_stage": "alternate_structural_role_found",
            "semantic_exposure_source": "contents",
            "semantic_parameterized_capability_class": "search_predicate",
            **_EXPOSURE_CAUSE_ENUMS,
            "attribute_names": ["CANARY_AXContents"],
            "parameterized_names": ["CANARY_SearchPredicate"],
            "role": "CANARY_AXTextField",
            "subrole": "CANARY_Subrole",
            "value": "CANARY_PRIVATE_VALUE",
            "element_identity": "CANARY_PRIVATE_ELEMENT",
            "frame": {"x": 1},
        }
    )
    return raw


def test_exposure_diagnostics_survive_controller_helper_viewer_and_trace(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import (
        emit_computer_trace,
        result_trace_facts,
    )
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_with_exposure_diagnostics()
    controller = BrowserComputerController._safe_semantic_text_diagnostics(
        {"data": {"diagnostics": raw["diagnostics"]}}
    )
    delivery = safe_type_diagnostic_facts(raw)
    helper = _computer_result_envelope("computer.probe_text_control", raw)
    trace_facts = result_trace_facts(raw)
    trace_event = emit_computer_trace(
        "helper.result", "computer.probe_text_control", **trace_facts
    )
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "result": raw},
    )
    viewer = ViewerBrokerClient(
        url="http://127.0.0.1:8770", token="secret"
    ).run_computer(
        "computer.probe_text_control", {"target_control": "browser_address"}
    )
    outputs = (
        controller,
        delivery,
        helper["result"]["diagnostics"],
        trace_facts,
        trace_event,
        viewer["diagnostics"],
    )

    assert helper["ok"] is True
    for output in outputs:
        assert output["error_code"] == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
        for index, key in enumerate(_EXPOSURE_BOOL_FIELDS):
            assert output[key] is (index % 2 == 0)
        for index, (key, cap) in enumerate(_EXPOSURE_COUNT_CAPS.items()):
            assert output[key] == min(index + 1, cap)
        assert output["semantic_exposure_stage"] == "alternate_structural_role_found"
        assert output["semantic_exposure_source"] == "contents"
        assert output["semantic_parameterized_capability_class"] == "search_predicate"
        for key, value in _EXPOSURE_CAUSE_ENUMS.items():
            assert output[key] == value
        serialized = json.dumps(output)
        assert "CANARY" not in serialized
        for key in (
            "attribute_names", "parameterized_names", "role", "subrole", "value",
            "element_identity", "frame",
        ):
            assert key not in output


def test_exposure_counts_clamp_and_unknown_enums_are_dropped(monkeypatch):
    from core_runtime.host_broker.computer_delivery import safe_type_diagnostic_facts
    from core_runtime.host_broker.computer_host_helper import _computer_result_envelope
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    raw = _probe_with_exposure_diagnostics()
    diagnostics = raw["diagnostics"]
    diagnostics.update({key: 999 for key in _EXPOSURE_COUNT_CAPS})
    diagnostics.update(
        {
            "semantic_exposure_stage": "CANARY_STAGE",
            "semantic_exposure_source": "CANARY_SOURCE",
            "semantic_parameterized_capability_class": "CANARY_CAPABILITY",
            **{key: f"CANARY_{key}" for key in _EXPOSURE_CAUSE_ENUMS},
        }
    )
    monkeypatch.setattr(
        ViewerBrokerClient,
        "_request",
        lambda *args, **kwargs: {"ok": True, "result": raw},
    )
    outputs = (
        BrowserComputerController._safe_semantic_text_diagnostics(
            {"data": {"diagnostics": raw["diagnostics"]}}
        ),
        safe_type_diagnostic_facts(raw),
        _computer_result_envelope("computer.probe_text_control", raw)["result"]["diagnostics"],
        result_trace_facts(raw),
        ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
            "computer.probe_text_control", {"target_control": "browser_address"}
        )["diagnostics"],
    )

    for output in outputs:
        for key, cap in _EXPOSURE_COUNT_CAPS.items():
            assert output[key] == cap
        assert output["semantic_counts_truncated"] is True
        assert "semantic_exposure_stage" not in output
        assert "semantic_exposure_source" not in output
        assert "semantic_parameterized_capability_class" not in output
        for key in _EXPOSURE_CAUSE_ENUMS:
            assert key not in output
        assert "CANARY" not in json.dumps(output)
