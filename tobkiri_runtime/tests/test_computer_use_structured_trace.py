from __future__ import annotations

import json
import io
import sys
from pathlib import Path


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_trace_writer_uses_fixed_allowlist_and_drops_content(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import (
        computer_action_trace,
        emit_computer_trace,
    )

    path = tmp_path / "computer.jsonl"
    monkeypatch.setenv("RUMI_COMPUTER_USE_TRACE_PATH", str(path))
    secrets = {
        "text": "never-log-typed-text",
        "url": "https://example.invalid/?secret=never-log-query",
        "approval_token": "never-log-approval-token",
        "window_title": "never-log-window-title",
        "clipboard": "never-log-clipboard",
        "environment": {"CEREBRAS_API_KEY": "never-log-key"},
        "raw_result": {"value": "never-log-value"},
    }
    with computer_action_trace("computer.type", run_id="run-1", action_id="host-audit-1"):
        event = emit_computer_trace(
            "controller.result",
            "computer.type",
            selected_driver="mac_swift_host",
            requested_delivery_mode="background",
            executed=True,
            completion_verified=True,
            dispatched_units=7,
            duration_ms=12.25,
            **secrets,
        )

    assert set(event) == {
        "timestamp_ms",
        "run_id",
        "action_id",
        "stage",
        "action",
        "selected_driver",
        "requested_delivery_mode",
        "executed",
        "completion_verified",
        "dispatched_units",
        "duration_ms",
    }
    serialized = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600
    assert not any(secret in serialized for secret in (
        "never-log-typed-text",
        "never-log-query",
        "never-log-approval-token",
        "never-log-window-title",
        "never-log-clipboard",
        "never-log-key",
        "never-log-value",
    ))


def test_controller_trace_never_serializes_payload_or_result_content(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    path = tmp_path / "controller.jsonl"
    monkeypatch.setenv("RUMI_COMPUTER_USE_TRACE_PATH", str(path))
    controller = BrowserComputerController(artifact_root=tmp_path / "artifacts")
    monkeypatch.setattr(
        controller,
        "_run_action",
        lambda action, payload, yolo_mode=False: {
            "action": action,
            "executed": True,
            "driver": "mac_swift_host",
            "completion_verified": True,
            "text": "controller-secret-text",
            "url": "https://example.invalid/?secret=controller-query",
            "window_title": "controller-secret-title",
            "diagnostics": {"dispatched_units": 7, "final_ax_value": "controller-secret-value"},
        },
    )

    result = controller.run(
        "computer.type",
        {
            "text": "payload-secret-text",
            "url": "https://example.invalid/?secret=payload-query",
            "approval_token": "payload-secret-token",
            "window_title": "payload-secret-title",
            "app": "ChatGPT Atlas",
            "fallback": "background",
        },
        yolo_mode=True,
    )

    assert result["text"] == "controller-secret-text"  # result contract remains untouched
    events = _events(path)
    assert [event["stage"] for event in events] == ["controller.start", "controller.result"]
    assert len({event["action_id"] for event in events}) == 1
    assert events[0]["target_app_present"] is True
    assert events[0]["requested_delivery_mode"] == "background"
    assert events[0]["approval_replay"] is True
    assert events[1]["selected_driver"] == "mac_swift_host"
    assert events[1]["completion_verified"] is True
    assert events[1]["dispatched_units"] == 7
    serialized = path.read_text(encoding="utf-8")
    assert "secret" not in serialized
    assert "ChatGPT Atlas" not in serialized


def test_broker_response_trace_correlates_host_audit_without_logging_args(monkeypatch, tmp_path):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    path = tmp_path / "broker.jsonl"
    monkeypatch.setenv("RUMI_COMPUTER_USE_TRACE_PATH", str(path))

    def fake_request(self, method, route, payload=None):
        return {
            "ok": True,
            "audit_id": "host-audit-safe-1",
            "result": {
                "action": "computer.type",
                "executed": True,
                "driver": "mac_swift_host",
                "completion_verified": True,
                "text": "broker-secret-text",
                "window_title": "broker-secret-title",
            },
        }

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    result = ViewerBrokerClient(url="http://127.0.0.1:8770", token="broker-secret-token").run_computer(
        "computer.type",
        {
            "text": "request-secret-text",
            "approval_token": "approval-secret-token",
            "app": "ChatGPT Atlas",
            "fallback": "background",
        },
        context={"conversation_id": "conversation-1"},
    )

    assert result["text"] == "broker-secret-text"  # broker result remains unchanged
    event = _events(path)[0]
    assert event["stage"] == "broker.response"
    assert event["run_id"] == "conversation-1"
    assert event["action_id"] == "host-audit-safe-1"
    assert event["approval_replay"] is True
    assert event["result_ok"] is True
    assert event["target_app_present"] is True
    serialized = path.read_text(encoding="utf-8")
    assert "secret" not in serialized
    assert "ChatGPT Atlas" not in serialized


def test_result_trace_facts_are_scalar_allowlisted_diagnostics_only():
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts

    facts = result_trace_facts(
        {
            "executed": False,
            "is_error": True,
            "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
            "diagnostics": {
                "input_dispatched": True,
                "dispatched_units": 3,
                "failure_stage": "post_dispatch_verification",
                "target_pid_stable": True,
                "focused_element_stable": False,
                "final_ax_value": "must-not-escape",
                "raw_payload": {"text": "must-not-escape"},
            },
            "ax_candidate": {
                "driver_registered": True,
                "driver_available": False,
                "background_type_capable": True,
                "pyobjc_ax_import_available": False,
                "ax_process_trusted": False,
                "ax_set_value_unsafe_app": False,
                "attempted": False,
                "result_code": "AX_IMPORT_UNAVAILABLE",
                "raw_target": "must-not-escape",
            },
        }
    )

    assert facts["delivered"] is True
    assert facts["dispatched_units"] == 3
    assert facts["failure_stage"] == "post_dispatch_verification"
    assert facts["target_pid_stable"] is True
    assert facts["focused_element_stable"] is False
    assert facts["driver_registered"] is True
    assert facts["driver_available"] is False
    assert facts["pyobjc_ax_import_available"] is False
    assert facts["ax_attempted"] is False
    assert facts["ax_result_code"] == "AX_IMPORT_UNAVAILABLE"
    assert "final_ax_value" not in facts
    assert "raw_payload" not in facts

    envelope_facts = result_trace_facts(
        {
            "ok": False,
            "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
            "result": {
                "executed": False,
                "diagnostics": {"input_dispatched": True, "dispatched_units": 2},
            },
        }
    )
    assert envelope_facts["result_ok"] is False
    assert envelope_facts["executed"] is False
    assert envelope_facts["delivered"] is True
    assert envelope_facts["dispatched_units"] == 2
    assert envelope_facts["error_code"] == "TYPE_COMPLETION_NOT_VERIFIED"


def test_helper_trace_exposes_executed_but_unverified_background_type(monkeypatch, tmp_path):
    from core_runtime.host_broker import computer_host_helper

    path = tmp_path / "helper.jsonl"
    monkeypatch.setenv("RUMI_COMPUTER_USE_TRACE_PATH", str(path))
    monkeypatch.setenv("RUMI_COMPUTER_HOST_INTERNAL", "test-restore-marker")
    class FakeSession:
        profile_id = "profile-1"

    class FakeContainer:
        def get_or_none(self, name):
            assert name == "v4_dispatch_session"
            return FakeSession()

    monkeypatch.setattr(
        computer_host_helper,
        "get_container",
        lambda: FakeContainer(),
    )
    monkeypatch.setattr(
        computer_host_helper,
        "invoke_global_contract",
        lambda _session, _contract_id, _operation, _request: {
            "action": "computer.type",
            "executed": True,
            "background": True,
            "driver": "mac_accessibility",
            # Deliberately no completion_verified: the helper must reject this.
            "text": "helper-secret-text",
        },
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "function_id": "computer.type",
                    "args": {"text": "request-secret-text"},
                    "viewer_host_approved": True,
                    "trace_context": {"run_id": "run-2", "action_id": "host-audit-2"},
                }
            )
        ),
    )
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    assert computer_host_helper.main() == 0
    envelope = json.loads(stdout.getvalue())
    assert envelope["ok"] is False
    assert envelope["error_code"] == "TYPE_DIAGNOSTICS_INVALID"
    event = next(
        item for item in _events(path) if item.get("stage") == "helper.result"
    )
    assert event["stage"] == "helper.result"
    assert event["action_id"] == "host-audit-2"
    assert event["selected_driver"] == "mac_accessibility"
    assert event["background"] is True
    assert event["executed"] is True
    assert event["completion_verified"] is False
    assert event["result_ok"] is False
    assert event["error_code"] == "TYPE_DIAGNOSTICS_INVALID"
    assert "secret" not in path.read_text(encoding="utf-8")


def test_trace_keeps_only_bounded_final_stale_recovery_fields_and_closed_enums(monkeypatch, tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import (
        computer_action_trace,
        emit_computer_trace,
        result_trace_facts,
    )

    raw = {
        "action": "computer.probe_text_control",
        "diagnostics": {
            "semantic_stale_parent_refresh_attempted": True,
            "semantic_stale_parent_refresh_succeeded": False,
            "semantic_stale_recovery_final_scan_complete": False,
            "semantic_stale_additional_read_budget_exhausted": True,
            "semantic_stale_parent_refresh_count": 2,
            "semantic_stale_parent_refresh_read_count": 3,
            "semantic_stale_additional_ax_read_count": 65,
            "semantic_discovery_pass_count": 4,
            "semantic_stale_recovery_restart_count": 3,
            "semantic_third_pass_stale_count": 65,
            "semantic_third_pass_unknown_branch_count": 65,
            "semantic_third_pass_nodes_visited_count": 256,
            "semantic_third_pass_final_candidate_count": 9,
            "semantic_stale_reference_refresh_class": "branch_now_empty",
            "semantic_stale_branch_comparison": "different_class_or_depth",
            "semantic_second_third_stale_reference_class": "new_parent_new_reference",
            "semantic_stale_recovery_outcome": "recovered_after_parent_refresh",
            "ax_ref": "CANARY_AX_REF",
            "raw_branch_path": "/private/CANARY_PATH",
        },
    }

    facts = result_trace_facts(raw)
    assert facts["semantic_stale_parent_refresh_count"] == 1
    assert facts["semantic_stale_parent_refresh_read_count"] == 2
    assert facts["semantic_stale_additional_ax_read_count"] == 64
    assert facts["semantic_discovery_pass_count"] == 3
    assert facts["semantic_stale_recovery_restart_count"] == 2
    assert facts["semantic_third_pass_nodes_visited_count"] == 255
    assert facts["semantic_third_pass_final_candidate_count"] == 8
    assert facts["semantic_stale_reference_refresh_class"] == "branch_now_empty"
    assert facts["semantic_stale_branch_comparison"] == "different_class_or_depth"
    assert facts["semantic_second_third_stale_reference_class"] == "new_parent_new_reference"
    assert facts["semantic_stale_recovery_outcome"] == "recovered_after_parent_refresh"
    assert facts["semantic_counts_truncated"] is True

    path = tmp_path / "computer.jsonl"
    monkeypatch.setenv("RUMI_COMPUTER_USE_TRACE_PATH", str(path))
    with computer_action_trace("computer.probe_text_control", run_id="run-1", action_id="safe-1"):
        event = emit_computer_trace("controller.result", "computer.probe_text_control", **facts)

    assert event["semantic_stale_recovery_outcome"] == "recovered_after_parent_refresh"
    assert event["semantic_stale_reference_refresh_class"] == "branch_now_empty"
    assert event["semantic_second_third_stale_reference_class"] == "new_parent_new_reference"
    assert event["semantic_third_pass_nodes_visited_count"] == 255
    serialized = path.read_text(encoding="utf-8")
    assert "CANARY" not in serialized


def test_selection_trace_keeps_only_truncated_quartz_aggregate_contract():
    from ecosystem.rumi_default_tools_pack.domain.computer.trace import result_trace_facts

    facts = result_trace_facts(
        {
            "action": "computer.select_window",
            "selected": False,
            "selection_quartz_cg_all_windows_query_outcome": "success_nonempty_truncated",
            "selection_quartz_cg_all_windows_records_aggregated_count": 999,
            "selection_authoritative_permission_outcome": "permissions_ok_target_unknown",
            "quartz_records": [{"owner": "CANARY_OWNER", "id": 42}],
        }
    )

    assert facts["selection_quartz_cg_all_windows_query_outcome"] == "success_nonempty_truncated"
    assert facts["selection_quartz_cg_all_windows_records_aggregated_count"] == 256
    assert facts["selection_authoritative_permission_outcome"] == "permissions_ok_target_unknown"
    assert "CANARY" not in json.dumps(facts)
