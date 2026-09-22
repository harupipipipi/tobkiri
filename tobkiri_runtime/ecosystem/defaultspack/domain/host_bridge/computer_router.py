from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from ..tool_policy.internal_context import tool_server_approval_context_is_internal
from core_runtime.defaultspack_host_contract_adapter import run_host_contract_action
from .viewer_broker_client import ViewerBrokerClient

_VIEWER_RECOVERY_MESSAGE = (
    "Rumi Viewer が未接続です。foreground/on-screen 操作は承認と Rumi Viewer 接続後に利用できます。"
    "承認してください。Rumi Viewer を起動または前面表示して macOS 権限を許可するか、表/前面で作業しますか?"
)

# Test and embedding callers may inject a controller explicitly.  Runtime
# dispatch never populates this hook; it must use the captured v4 contract.
BrowserComputerController: type[Any] | None = None
_BROWSER_TEXT_INPUT_RECOMMENDED_NEXT_ACTIONS = (
    "computer.type",
    "computer.key",
    "computer.click",
    "computer.screenshot",
    "computer.observe",
)
_BROWSER_TEXT_INPUT_GUIDANCE = (
    "If the browser page or search field is ready, use computer.type for text input "
    "and computer.key for Enter or shortcuts; normal approval gates still apply. "
    "The computer.type text must be the literal user-requested URL, query, or form "
    "text to enter; do not type the current URL, app name, or window title unless "
    "that is exactly what the user asked to enter."
)


def should_route_to_viewer(action: str) -> bool:
    if os.environ.get("RUMI_COMPUTER_HOST_INTERNAL") == "1":
        return False
    if platform.system() != "Darwin":
        return False
    return str(action or "").startswith("computer.")


def run_computer_action(
    action: str,
    payload: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    *,
    tool_name: str = "computer_use",
    tool_arguments: dict[str, Any] | None = None,
    artifact_root: Path | None = None,
    yolo_mode: bool = False,
    controller_cls: type[Any] | None = None,
) -> dict[str, Any]:
    normalized_action = str(action or "")
    normalized_payload = dict(payload or {})
    normalized_context = dict(context or {}) if isinstance(context, dict) else {}
    if not _approval_token_present(normalized_payload):
        approval_token = _approval_token_from_context(normalized_context, tool_name, normalized_action)
        if approval_token:
            normalized_payload["approval_token"] = approval_token
    effective_yolo_mode = bool(yolo_mode) or _context_has_server_approval(normalized_context)
    if should_route_to_viewer(normalized_action):
        client = ViewerBrokerClient.from_environment()
        if client.available():
            try:
                result = client.run_computer(
                    normalized_action,
                    normalized_payload,
                    context=normalized_context,
                    artifact_root=artifact_root,
                )
                if not isinstance(result, dict):
                    return {"action": normalized_action, "result": result}
                if _is_request_approval_needed(result):
                    return _approval_required_response(
                        tool_name,
                        str(result.get("action") or normalized_action),
                        normalized_payload,
                        result,
                        normalized_context,
                    )
                return _with_browser_text_input_recommendations(normalized_action, dict(result))
            except Exception as exc:
                return _viewer_connection_required_response(
                    normalized_action,
                    f"Rumi Viewer host broker is unavailable: {exc}",
                )
        return _viewer_connection_required_response(
            normalized_action,
            "Rumi Viewer is required for computer control on macOS.",
        )
    return _run_local_controller(
        normalized_action,
        normalized_payload,
        tool_name=tool_name,
        tool_arguments=tool_arguments,
        artifact_root=artifact_root,
        yolo_mode=effective_yolo_mode,
        context=normalized_context,
        controller_cls=controller_cls,
    )


def _run_local_controller(
    action: str,
    payload: dict[str, Any],
    *,
    tool_name: str,
    tool_arguments: dict[str, Any] | None,
    artifact_root: Path | None,
    yolo_mode: bool,
    context: dict[str, Any] | None,
    controller_cls: type[Any] | None = None,
) -> dict[str, Any]:
    if controller_cls is None:
        controller_cls = BrowserComputerController
    if controller_cls is None:
        try:
            result = run_host_contract_action(
                action,
                {
                    **payload,
                    "artifact_root": str(artifact_root) if artifact_root else "",
                    "yolo_mode": yolo_mode,
                },
                source_function_id="defaultspack.domain.host_bridge.computer_router",
            )
        except Exception as exc:
            return {
                "action": action,
                "is_error": True,
                "error_type": "v4_host_contract_unavailable",
                "reason": str(exc),
            }
    else:
        result = controller_cls(artifact_root=artifact_root).run(
            action,
            payload,
            yolo_mode=yolo_mode,
        )
    if not isinstance(result, dict):
        return {"action": action, "result": result}
    if _is_request_approval_needed(result):
        approval_payload = result.get("payload") if isinstance(result.get("payload"), dict) else payload
        return _approval_required_response(
            tool_name,
            str(result.get("action") or action),
            dict(approval_payload),
            result,
            context,
        )
    return dict(result)


def _approval_token_present(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(str(payload.get("approval_token") or "").strip())


def _with_browser_text_input_recommendations(action: str, result: dict[str, Any]) -> dict[str, Any]:
    normalized_action = str(result.get("action") or action or "").strip()
    result.setdefault("action", normalized_action)
    pending_approval = bool(result.get("requires_approval") or result.get("approval_required"))
    if normalized_action in {"computer.observe", "computer.screenshot"} and not (
        result.get("is_error") or pending_approval
    ):
        recommendations = list(result.get("recommended_next_actions") or [])
        for next_action in _BROWSER_TEXT_INPUT_RECOMMENDED_NEXT_ACTIONS:
            if next_action not in recommendations:
                recommendations.append(next_action)
        result["recommended_next_actions"] = recommendations
        result.setdefault("input_guidance", _BROWSER_TEXT_INPUT_GUIDANCE)
    return result


def _viewer_connection_required_response(action: str, reason: str) -> dict[str, Any]:
    return {
        "action": action,
        "is_error": True,
        "reason": reason,
        "message": _VIEWER_RECOVERY_MESSAGE,
        "user_prompt": _VIEWER_RECOVERY_MESSAGE,
        "recovery": {
            "kind": "viewer_connection_required",
            "requires_approval": True,
            "requires_viewer_connection": True,
            "prompt": _VIEWER_RECOVERY_MESSAGE,
            "note": (
                "Open Rumi Viewer and approve the request; foreground/on-screen operation is "
                "available after a connected Rumi Viewer has macOS permissions."
            ),
            "recommended_next_actions": [
                "approve_request",
                "open_rumi_viewer",
                "choose_foreground_work",
            ],
        },
        "permission_subject": "Rumi Viewer",
    }


def _approval_token_from_context(
    context: dict[str, Any] | None,
    tool_name: str,
    action: str,
) -> str:
    if not isinstance(context, dict):
        return ""
    tokens = context.get("tool_approval_tokens")
    if not isinstance(tokens, dict):
        return ""
    for key in (str(tool_name or "").strip(), str(action or "").strip()):
        token = str(tokens.get(key) or "").strip()
        if token:
            return token
    return ""


def _context_has_server_approval(context: dict[str, Any] | None) -> bool:
    if not isinstance(context, dict):
        return False
    return tool_server_approval_context_is_internal(context) or _context_has_verified_server_approval_token(context)


def _context_has_verified_server_approval_token(context: dict[str, Any]) -> bool:
    token = str(context.get("_tool_server_approval_token") or "").strip()
    operation = str(context.get("_tool_server_approval_operation") or "").strip()
    args_hash = str(context.get("_tool_server_approval_args_hash") or "").strip()
    if not token or not operation or not args_hash:
        return False
    pack_id = str(context.get("_tool_server_approval_pack_id") or "").strip()
    conversation_id = str(context.get("_tool_server_approval_conversation_id") or "").strip()
    for approval in _approval_modules():
        try:
            verification = approval.verify_execution_token(
                token,
                operation,
                args_hash,
                consume=False,
                pack_id=pack_id,
                conversation_id=conversation_id,
            )
        except Exception:
            continue
        if bool(getattr(verification, "valid", False)):
            return True
    return False


def _context_value(context: dict[str, Any] | None, *keys: str) -> str:
    if not isinstance(context, dict):
        return ""
    for key in keys:
        value = str(context.get(key) or "").strip()
        if value:
            return value
    return ""


def _is_request_approval_needed(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    if not bool(result.get("requires_approval") or result.get("approval_required")):
        return False
    return not str(result.get("approval_request_id") or result.get("request_id") or "").strip()


def _approval_required_response(
    tool_name: str,
    action: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if str(result.get("approval_request_id") or result.get("request_id") or "").strip():
        return result
    approval = _approval_module()

    safe_tool_name = str(tool_name or "computer_use").strip() or "computer_use"
    safe_action = str(action or safe_tool_name)
    request_arguments = _request_arguments(safe_tool_name, safe_action, payload)
    pack_id = _context_value(context, "owner_pack", "pack_id", "_source_pack_id") or "defaultspack"
    conversation_id = _context_value(context, "conversation_id", "conversation_turn_id")
    request = approval.create_approval_request(
        safe_action,
        "high",
        request_arguments,
        details={
            "tool_name": safe_tool_name,
            "action": safe_action,
            "function_id": safe_action,
            "arguments": dict(request_arguments or {}),
            "payload": dict(payload or {}),
            "pack_id": pack_id,
            "conversation_id": conversation_id,
            "permission_subject": "Rumi Viewer",
        },
    )
    wrapped = dict(result)
    wrapped.pop("approval_token", None)
    wrapped.update(
        {
            "action": safe_action,
            "tool_name": safe_tool_name,
            "operation": safe_action,
            "payload": dict(payload or {}),
            "requires_approval": True,
            "approval_required": True,
            "approval_request_id": request["request_id"],
            "request_id": request["request_id"],
            "risk_level": request.get("risk_level", "high"),
            "args_hash": request.get("args_hash"),
            "expires_at": request.get("expires_at"),
            "display_summary": request.get("display_summary") or safe_action,
            "permission_subject": "Rumi Viewer",
        }
    )
    if not wrapped.get("message") and wrapped.get("approval_hint"):
        wrapped["message"] = wrapped.get("approval_hint")
    wrapped.setdefault("user_prompt", "承認してください")
    wrapped.setdefault("message", "承認してください。表/前面で作業しますか?")
    if isinstance(wrapped.get("recovery"), dict):
        wrapped["recovery"].setdefault("prompt", wrapped["user_prompt"])
    warning = result.get("approval_warning")
    if isinstance(warning, str) and warning.strip():
        wrapped["approval_warning"] = warning
    expires_in = result.get("approval_expires_in_seconds")
    if expires_in is not None:
        wrapped["approval_expires_in_seconds"] = expires_in
    return wrapped


def _request_arguments(tool_name: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"action": action, "payload": dict(payload or {})}


def _approval_module():
    from ..safety import approval

    return approval


def _approval_modules() -> list[Any]:
    modules: list[Any] = []
    for import_name in (
        "ecosystem.defaultspack.domain.safety.approval",
        "domain.safety.approval",
    ):
        try:
            module = __import__(import_name, fromlist=["approval"])
        except Exception:
            continue
        if module not in modules:
            modules.append(module)
    if not modules:
        try:
            modules.append(_approval_module())
        except Exception:
            pass
    return modules


sys.modules.setdefault("domain.host_bridge.computer_router", sys.modules[__name__])
sys.modules.setdefault("ecosystem.defaultspack.domain.host_bridge.computer_router", sys.modules[__name__])
for _parent_name in ("domain.host_bridge", "ecosystem.defaultspack.domain.host_bridge"):
    _parent = sys.modules.get(_parent_name)
    if _parent is not None:
        setattr(_parent, "computer_router", sys.modules[__name__])
