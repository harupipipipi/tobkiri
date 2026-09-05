import os
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.host_bridge.computer_router import run_computer_action
from core_runtime.global_contracts.computer_trace import (
    computer_action_trace,
    emit_computer_trace,
    requested_delivery_mode,
    result_trace_facts,
    target_trace_facts,
)


def run(input_data, context=None):
    action = input_data.get("action")
    if not action:
        return error("'action' is required", code="INVALID_INPUT")
    started = time.monotonic()
    payload = dict(input_data.get("payload") or {})
    try:
        yolo_mode = _truthy(context.get("yolo_mode")) if isinstance(context, dict) else False
        result = run_computer_action(
            str(action),
            payload,
            context if isinstance(context, dict) else None,
            tool_name=str(input_data.get("tool_name") or "browser_computer"),
            artifact_root=_artifact_root(context),
            yolo_mode=yolo_mode,
        )
    except Exception:
        with computer_action_trace(str(action), run_id=_context_trace_run_id(context)):
            emit_computer_trace(
                "pack.result",
                str(action),
                duration_ms=(time.monotonic() - started) * 1000,
                requested_delivery_mode=requested_delivery_mode(payload),
                result_ok=False,
                error_code="PACK_ACTION_EXCEPTION",
                **target_trace_facts(payload),
            )
        return error("Browser computer action failed.", code="BROWSER_COMPUTER_FAILED")
    with computer_action_trace(
        str(action),
        run_id=_context_trace_run_id(context),
        action_id=str(result.get("host_audit_id") or "") if isinstance(result, dict) else "",
    ):
        trace_facts = target_trace_facts(payload)
        trace_facts.update(result_trace_facts(result))
        emit_computer_trace(
            "pack.result",
            str(action),
            duration_ms=(time.monotonic() - started) * 1000,
            requested_delivery_mode=requested_delivery_mode(payload),
            **trace_facts,
        )
    # Keep the complete result available to the UI and audit path, while giving
    # the model a small, deterministic state-transition contract.  In
    # particular, screenshots contain useful UI metadata but that metadata must
    # not drown out the fact that observation is complete and interaction is
    # now the next phase.
    return ok({
        "result": json.dumps(_model_facing_result(str(action), result), ensure_ascii=False),
        "is_error": bool(result.get("is_error")),
        "widget": result,
    })


def _model_facing_result(action, result):
    """Return provider-agnostic progress facts without changing host semantics."""
    if not isinstance(result, dict):
        return {"model_context": {"action": str(action), "outcome": {"succeeded": False}}}

    canonical_action = str(result.get("action") or action or "").strip()
    pending_approval = bool(result.get("approval_required") or result.get("requires_approval"))
    failed = bool(result.get("is_error") or result.get("error"))
    succeeded = not failed and not pending_approval
    context = {
        "action": canonical_action,
        "outcome": {
            "succeeded": succeeded,
            "approval_pending": pending_approval,
        },
    }

    target = _model_target_facts(result)
    if target:
        context["target"] = target

    navigation = _model_navigation_facts(canonical_action, result, succeeded=succeeded)
    if navigation:
        context["navigation"] = navigation

    context["task_transition"] = _model_task_transition(
        canonical_action,
        succeeded=succeeded,
        pending_approval=pending_approval,
        target=target,
        probe_ready=(result.get("semantic_control_ready") is True),
    )
    compact = {"model_context": context}
    for key in (
        "reason", "message", "recovery", "diagnostics", "error_code", "executed",
        "delivered", "input_dispatched", "completion_verified", "effect_observed",
        "postcondition_verified", "outcome", "verification_required",
        "probe_completed", "semantic_control_ready", "mutation_attempted",
    ):
        value = result.get(key)
        if value not in (None, "", {}, []):
            compact[key] = value
    return compact


def _model_target_facts(result):
    target = result.get("target_window")
    selected = result.get("selected_window")
    if not isinstance(target, dict):
        target = selected if isinstance(selected, dict) else {}
    facts = {}
    app = target.get("app") or target.get("name") or result.get("target_app")
    if app:
        facts["app"] = str(app)
    if target.get("pid") not in (None, ""):
        facts["pid"] = target.get("pid")
    if target.get("window_id") not in (None, ""):
        facts["window_id"] = target.get("window_id")
    if isinstance(selected, dict) and selected:
        binding_valid = _same_window(target, selected)
        facts["window_selected"] = binding_valid
        facts["binding_valid"] = binding_valid
    active = result.get("active_window")
    if isinstance(active, dict) and active and target:
        facts["window_foreground"] = _same_window(target, active)
    return facts


def _same_window(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict) or not left or not right:
        return False
    for key in ("window_id", "pid"):
        left_value = left.get(key)
        right_value = right.get(key)
        if left_value not in (None, "") and right_value not in (None, ""):
            return left_value == right_value
    left_app = str(left.get("app") or left.get("name") or "").strip().casefold()
    right_app = str(right.get("app") or right.get("name") or "").strip().casefold()
    return bool(left_app and right_app and left_app == right_app)


def _model_navigation_facts(action, result, *, succeeded):
    if action != "browser.open_url":
        return {}
    facts = {
        "open_succeeded": bool(succeeded and result.get("opened", True)),
    }
    url = result.get("url")
    if isinstance(url, str) and url:
        facts["last_requested_url"] = url
    if facts["open_succeeded"]:
        facts["same_url_reopen_needed"] = False
    return facts


def _model_task_transition(action, *, succeeded, pending_approval, target=None, probe_ready=False):
    if pending_approval:
        return {
            "completed_phase": "approval_requested",
            "next_phase": "await_approval",
            "recommended_actions": ["approve_request"],
            "avoid_actions": ["repeat_unapproved_action"],
        }
    if not succeeded:
        return {
            "completed_phase": "action_failed",
            "next_phase": "recover_from_failure",
            "recommended_actions": ["inspect_failure_and_recover"],
            "avoid_actions": ["assume_action_succeeded"],
        }
    if action == "computer.probe_text_control":
        if probe_ready:
            return {
                "completed_phase": "probe_text_control",
                "next_phase": "request_write_approval_and_type",
                "recommended_actions": ["computer.type"],
                "avoid_actions": ["repeat_probe_without_state_change"],
            }
        return {
            "completed_phase": "probe_text_control",
            "next_phase": "stop_before_write_approval",
            "recommended_actions": ["inspect_probe_diagnostics"],
            "avoid_actions": [
                "computer.type", "request_type_approval", "computer.screenshot",
                "retry_other_driver",
            ],
        }
    if action == "browser.open_url":
        return {
            "completed_phase": "navigate_to_requested_page",
            "next_phase": "observe_opened_page",
            "recommended_actions": ["computer.screenshot"],
            "avoid_actions": ["browser.open_url:same_url"],
        }
    if action in {"computer.screenshot", "computer.observe"}:
        binding_valid = target.get("binding_valid") if isinstance(target, dict) else None
        if binding_valid is False:
            return {
                "completed_phase": "observe_current_target",
                "next_phase": "bind_foreground_target",
                "recommended_actions": ["computer.select_window", "computer.show_app"],
                "avoid_actions": ["send_input_before_target_binding"],
            }
        avoid_actions = [
            "repeat_observation_without_state_change",
            "browser.open_url:same_url",
        ]
        if binding_valid is True:
            avoid_actions.append("repeat_target_selection_when_binding_is_valid")
        return {
            "completed_phase": "observe_current_target",
            "next_phase": "interact_with_visible_target",
            "recommended_actions": ["computer.click", "computer.type", "computer.key"],
            "avoid_actions": avoid_actions,
        }
    if action in {"computer.show_app", "computer.select_app", "computer.select_window"}:
        return {
            "completed_phase": "bind_foreground_target",
            "next_phase": "observe_selected_target",
            "recommended_actions": ["computer.screenshot"],
            "avoid_actions": ["repeat_target_selection_without_binding_failure"],
        }
    if action == "computer.type":
        return {
            "completed_phase": "enter_text",
            "next_phase": "submit_or_verify_typed_text",
            "recommended_actions": ["computer.key", "computer.screenshot"],
            "avoid_actions": ["browser.open_url:same_setup_page", "repeat_same_text"],
        }
    if action in {"computer.click", "computer.key", "computer.scroll", "computer.drag"}:
        return {
            "completed_phase": "interact_with_visible_target",
            "next_phase": "observe_action_effect",
            "recommended_actions": ["computer.screenshot"],
            "avoid_actions": ["repeat_action_before_observing_effect"],
        }
    return {
        "completed_phase": "action_completed",
        "next_phase": "continue_unresolved_task",
        "recommended_actions": [],
        "avoid_actions": ["repeat_completed_action_without_state_change"],
    }


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _artifact_root(context):
    workspace = context.get("conversation_workspace_dir") if isinstance(context, dict) else None
    if isinstance(workspace, str) and workspace:
        return Path(workspace) / "tools" / "computer"
    return _trusted_direct_http_artifact_root()


def _trusted_direct_http_artifact_root():
    workspace_value = str(os.environ.get("RUMI_DEFAULTSPACK_DIRECT_CONVERSATION_WORKSPACE") or "").strip()
    chat_store_value = str(os.environ.get("RUMI_DEFAULTSPACK_CHAT_STORE_PATH") or "").strip()
    if not workspace_value or not chat_store_value:
        return None
    try:
        workspace = Path(workspace_value).expanduser().resolve()
        conversations = Path(chat_store_value).expanduser().resolve().parent / "conversations"
        relative = workspace.relative_to(conversations.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if len(relative.parts) != 2 or relative.parts[1] != "workspace" or not workspace.is_dir():
        return None
    return workspace / "tools" / "computer"


def _context_trace_run_id(context):
    if not isinstance(context, dict):
        return ""
    for key in ("conversation_id", "conversation_turn_id", "run_id"):
        value = str(context.get(key) or "").strip()
        if value:
            return value
    return ""
