"""Approve a pending local coding operation."""

from blocks._common import error, ok
from domain.safety.approval import (
    approve,
    register_debug_resume_handle,
    register_native_resume_handle,
)
from domain.safety.audit import record_approval
from domain.safety.debug_cli_operator import DebugCliOperatorError, verify_debug_cli_decision
from domain.safety.coding_ui_operator import CodingUiOperatorError, verify_coding_ui_operator
from domain.safety.approval import get_approval_request


def run(input_data, context=None):
    request_id = str(input_data.get("approval_request_id") or input_data.get("request_id") or "").strip()
    if not request_id:
        return error("'approval_request_id' is required", code="INVALID_INPUT")
    operator = input_data.get("debug_cli_operator")
    continuation_conversation_id = str(
        input_data.get("continuation_conversation_id") or ""
    ).strip()
    request = None
    if operator is not None:
        if str(operator.get("decision") or "") != "approve":
            result = error("debug operator decision mismatch", code="DEBUG_CLI_OPERATOR_INVALID")
            result["_http_status"] = 403
            return result
        try:
            verify_debug_cli_decision(
                request_id,
                str(input_data.get("expected_digest") or "").strip(),
                operator,
            )
        except DebugCliOperatorError as exc:
            result = error(str(exc), code="DEBUG_CLI_OPERATOR_INVALID")
            result["_http_status"] = 403
            return result
    else:
        request = get_approval_request(request_id)
        try:
            verify_coding_ui_operator(
                input_data.get("ui_operator"),
                request_id=request_id,
                expected_digest=str((request or {}).get("args_hash") or ""),
                decision="approve",
            )
        except CodingUiOperatorError as exc:
            result = error(str(exc), code="APPROVAL_OPERATOR_REQUIRED")
            result["_http_status"] = 403
            return result
        if continuation_conversation_id:
            request_map = request if isinstance(request, dict) else {}
            raw_details = request_map.get("details")
            details = raw_details if isinstance(raw_details, dict) else {}
            request_conversation_id = str(
                request_map.get("conversation_id")
                or details.get("conversation_id")
                or ""
            ).strip()
            if request_conversation_id != continuation_conversation_id:
                result = error(
                    "approval continuation conversation does not match",
                    code="APPROVAL_RESUME_MISMATCH",
                )
                result["_http_status"] = 409
                return result
    decision = approve(request_id, debug_operator=operator)
    record_approval(
        "coding.approval",
        request_id,
        "approved" if decision.get("approved") else "failed",
        decision_source="delegated_debug_cli" if operator is not None else "native_launcher_ui",
        human_approved=False if operator is not None else True,
    )
    if not decision.get("approved"):
        result = error(str(decision.get("reason") or "approval failed"), code="APPROVAL_FAILED")
        result["_http_status"] = 403
        return result
    token = str(decision.get("token", "") or "")
    if operator is not None:
        decision.pop("token", None)
        if not token:
            result = error(
                "approved debug request did not issue a resume credential",
                code="APPROVAL_FAILED",
            )
            result["_http_status"] = 500
            return result
        decision["resume_id"] = register_debug_resume_handle(
            request_id,
            token,
            operator=operator,
        )
    elif continuation_conversation_id:
        try:
            decision.pop("token", None)
            decision["resume_id"] = register_native_resume_handle(
                request or {},
                token,
            )
        except ValueError as exc:
            result = error(str(exc), code="APPROVAL_RESUME_UNAVAILABLE")
            result["_http_status"] = 409
            return result
    return ok(decision)
