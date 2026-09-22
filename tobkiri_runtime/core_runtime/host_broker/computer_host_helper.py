from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any


def _ensure_import_path() -> None:
    base = Path(__file__).resolve().parents[2]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


_ensure_import_path()

from core_runtime.host_broker.computer_delivery import (  # noqa: E402
    SAFE_TYPE_PREDISPATCH_ERROR_CODES,
    SAFE_WINDOW_SELECTION_ERROR_CODES,
    safe_ax_candidate_facts,
    safe_computer_delivery_facts,
    safe_type_diagnostic_facts,
    safe_window_selection_facts,
)
from core_runtime.global_contracts.computer_trace import (  # noqa: E402
    computer_action_trace,
    emit_computer_trace,
    result_trace_facts,
)
from core_runtime.di_container import get_container  # noqa: E402
from core_runtime.global_contract_dispatch import (  # noqa: E402
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    invoke_global_contract,
)


_HOST_ACTIONS: dict[str, tuple[str, str]] = {
    "browser.session": ("rumi.resource.browser.host.v1", "browser.session.get"),
    "browser.profiles.list": (
        "rumi.resource.browser.host.v1",
        "browser.profiles.list",
    ),
    "browser.cookies.list": (
        "rumi.resource.browser.host.v1",
        "browser.cookies.list",
    ),
    "browser.open_url": ("rumi.action.browser.host.v1", "browser.navigate"),
    "browser.profile.create": (
        "rumi.action.browser.host.v1",
        "browser.profile.create",
    ),
    "browser.profile.set_active": (
        "rumi.action.browser.host.v1",
        "browser.profile.set_active",
    ),
    "browser.profile.delete": (
        "rumi.action.browser.host.v1",
        "browser.profile.delete",
    ),
    "browser.profile.clear_cache": (
        "rumi.action.browser.host.v1",
        "browser.profile.clear_cache",
    ),
    "browser.profile.clear_cookies": (
        "rumi.action.browser.host.v1",
        "browser.profile.clear_cookies",
    ),
    "browser.cookies.import": (
        "rumi.action.browser.host.v1",
        "browser.cookies.import",
    ),
    "browser.cookies.delete": (
        "rumi.action.browser.host.v1",
        "browser.cookies.delete",
    ),
    "computer.context": ("rumi.resource.desktop.host.v1", "desktop.state"),
    "computer.apps": (
        "rumi.resource.desktop.host.v1",
        "desktop.applications.list",
    ),
    "computer.windows": ("rumi.resource.desktop.host.v1", "desktop.windows.list"),
    "computer.screenshot": (
        "rumi.resource.desktop.host.v1",
        "desktop.capture.frame",
    ),
    "computer.observe": (
        "rumi.resource.desktop.host.v1",
        "desktop.capture.frame",
    ),
    "computer.ax_tree": (
        "rumi.resource.desktop.host.v1",
        "desktop.accessibility.snapshot",
    ),
    "computer.ocr": (
        "rumi.resource.desktop.host.v1",
        "desktop.accessibility.snapshot",
    ),
    "computer.doctor": ("rumi.resource.desktop.host.v1", "desktop.state"),
    "computer.select_app": (
        "rumi.action.desktop.host.v1",
        "desktop.application.select",
    ),
    "computer.show_app": (
        "rumi.action.desktop.host.v1",
        "desktop.application.activate",
    ),
    "computer.select_window": (
        "rumi.action.desktop.host.v1",
        "desktop.window.select",
    ),
    "computer.move": ("rumi.action.desktop.host.v1", "desktop.pointer.move"),
    "computer.click": ("rumi.action.desktop.host.v1", "desktop.pointer.click"),
    "computer.drag": ("rumi.action.desktop.host.v1", "desktop.pointer.drag"),
    "computer.type": ("rumi.action.desktop.host.v1", "desktop.keyboard.type"),
    "computer.key": ("rumi.action.desktop.host.v1", "desktop.keyboard.key"),
    "computer.scroll": ("rumi.action.desktop.host.v1", "desktop.scroll"),
    "computer.semantic_action": (
        "rumi.action.desktop.host.v1",
        "desktop.accessibility.action",
    ),
    "computer.click_text": (
        "rumi.action.desktop.host.v1",
        "desktop.accessibility.action",
    ),
    "computer.pid_event": (
        "rumi.action.desktop.host.v1",
        "desktop.accessibility.action",
    ),
    "computer.clipboard.read": ("rumi.resource.clipboard.v1", "read"),
    "computer.clipboard.get": ("rumi.resource.clipboard.v1", "read"),
    "computer.clipboard.write": ("rumi.action.clipboard.v1", "write"),
    "computer.clipboard.set": ("rumi.action.clipboard.v1", "write"),
    "computer.clipboard.clear": ("rumi.action.clipboard.v1", "write"),
}


def main() -> int:
    os.environ["RUMI_COMPUTER_HOST_INTERNAL"] = "1"

    request = json.loads(sys.stdin.read() or "{}")
    action = str(request.get("function_id") or "").strip()
    payload = dict(request.get("args") or {})
    viewer_host_approved = bool(request.get("viewer_host_approved"))
    started = time.monotonic()

    try:
        artifact_root = _validated_artifact_root(request.get("artifact_root"))
        trace_context = (
            request.get("trace_context")
            if isinstance(request.get("trace_context"), dict)
            else {}
        )
        with computer_action_trace(
            action,
            run_id=str(trace_context.get("run_id") or ""),
            action_id=str(trace_context.get("action_id") or ""),
        ):
            result = _run_v4_host_action(
                action,
                payload,
                viewer_host_approved=viewer_host_approved,
                artifact_root=artifact_root,
            )
            envelope = _computer_result_envelope(
                action,
                result,
                artifact_root=artifact_root,
                request_args=payload,
            )
            facts = result_trace_facts(envelope)
            facts["approval_replay"] = viewer_host_approved
            facts["result_ok"] = envelope.get("ok") is True
            if envelope.get("error_code"):
                facts["error_code"] = envelope.get("error_code")
            emit_computer_trace(
                "helper.result",
                action,
                duration_ms=(time.monotonic() - started) * 1000,
                **facts,
            )
    except ValueError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "INVALID_ARTIFACT_ROOT",
                    "error": "The artifact root is invalid.",
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception:  # pragma: no cover - caller converts to broker error
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "VIEWER_HOST_FAILED",
                    "error": "Viewer host helper failed.",
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(json.dumps(envelope, ensure_ascii=False))
    return 0


def _run_v4_host_action(
    action: str,
    payload: dict[str, Any],
    *,
    viewer_host_approved: bool,
    artifact_root: Path | None,
) -> dict[str, Any]:
    """Dispatch a host action through the captured v4 session only.

    The helper is intentionally unable to discover or import a Pack runtime.
    A missing or stale session therefore returns an explicit unavailable
    result instead of silently selecting an installed implementation.
    """

    if not viewer_host_approved:
        raise PermissionError("Viewer approval is required")
    target = _HOST_ACTIONS.get(action)
    if target is None:
        return {
            "action": action,
            "is_error": True,
            "error_type": "host_operation_unavailable",
            "reason": "The action is not declared by the active v4 catalog.",
        }
    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        return {
            "action": action,
            "is_error": True,
            "error_type": "v4_dispatch_session_unavailable",
            "reason": "A captured v4 dispatch session is required.",
        }
    request = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "approved",
            "approval_token",
            "authority_token",
            "viewer_host_approved",
            "yolo_mode",
        }
    }
    if target[0] == "rumi.action.clipboard.v1":
        request = {"text": "" if action.endswith(".clear") else str(request.get("text", request.get("content", "")))}
    elif target[0] == "rumi.resource.clipboard.v1":
        request = {}
    if action == "computer.ocr":
        request["include_ocr"] = True
    request.update(
        {
            "profile_id": str(getattr(session, "profile_id", "")).strip(),
            "artifact_root": str(artifact_root) if artifact_root else "",
            "_contract_consumer_pack_id": "kernel",
            "_contract_consumer_function_id": "computer_host_helper",
        }
    )
    if not request["profile_id"]:
        return {
            "action": action,
            "is_error": True,
            "error_type": "v4_profile_unavailable",
            "reason": "The captured v4 session has no profile identity.",
        }
    try:
        result = invoke_global_contract(session, target[0], target[1], request)
    except (GlobalContractInvocationError, GlobalContractUnavailable) as exc:
        return {
            "action": action,
            "is_error": True,
            "error_type": "host_operation_unavailable",
            "reason": str(exc),
        }
    if not isinstance(result, dict):
        raise RuntimeError("host contract returned an invalid result")
    return dict(result)


def _run_desktop_action(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Tombstone for the removed pre-v4 desktop implementation."""

    del args, kwargs
    raise GlobalContractUnavailable(
        "legacy desktop action dispatch was removed; use the v4 host contract"
    )


def _computer_result_envelope(
    action: str,
    result: object,
    *,
    artifact_root: Path | None = None,
    request_args: dict[str, object] | None = None,
) -> dict[str, object]:
    result_value = result if isinstance(result, dict) else {}
    exact_selection_required = bool(
        action == "computer.select_window"
        and (
            (isinstance(request_args, dict) and request_args.get("require_exact_binding") is True)
            or result_value.get("selection_exact_binding_required") is True
        )
    )
    if exact_selection_required:
        requested_app = str((request_args or {}).get("app") or "")
        selection = safe_window_selection_facts(
            result_value, requested_app=requested_app
        )
        valid = bool(
            result_value.get("action") == action
            and result_value.get("selected") is True
            and selection.get("selection_exact_binding_required") is True
            and selection.get("selection_exact_binding_present") is True
        )
        if not valid:
            reported_code = str(selection.get("error_code") or "")
            error_code = (
                reported_code
                if reported_code in SAFE_WINDOW_SELECTION_ERROR_CODES
                else "SELECT_WINDOW_RESULT_INVALID"
            )
            return {
                "ok": False,
                "error_code": error_code,
                "error": "Computer window selection did not produce a verified exact binding.",
                "result": {
                    "action": action,
                    **selection,
                    "selection_exact_binding_required": True,
                    "selection_exact_binding_present": False,
                    "error_code": error_code,
                },
            }
    if action == "computer.probe_text_control":
        diagnostics = _safe_type_diagnostics(result)
        protocol_complete = bool(
            result_value.get("action") == action
            and result_value.get("executed") is True
            and result_value.get("is_error") is not True
            and diagnostics.get("probe_completed") is True
            and isinstance(diagnostics.get("semantic_control_ready"), bool)
            and diagnostics.get("input_dispatched") is False
            and diagnostics.get("mutation_attempted") is False
            and diagnostics.get("semantic_discovery_stage")
        )
        safe_result = {
            "action": action,
            "executed": protocol_complete,
            "probe_completed": diagnostics.get("probe_completed") is True,
            "semantic_control_ready": diagnostics.get("semantic_control_ready") is True,
            "input_dispatched": False,
            "mutation_attempted": False,
            "background": True,
            "foreground": False,
            "requires_foreground": False,
            "uses_physical_input": False,
            "diagnostics": diagnostics,
        }
        if diagnostics.get("error_code"):
            safe_result["error_code"] = diagnostics["error_code"]
        if protocol_complete:
            return {"ok": True, "result": safe_result}
        reported_code = str(diagnostics.get("error_code") or "")
        error_code = (
            reported_code
            if reported_code in SAFE_TYPE_PREDISPATCH_ERROR_CODES
            else "TYPE_DIAGNOSTICS_INVALID"
        )
        safe_result.update(
            {"executed": False, "probe_completed": False, "is_error": True}
        )
        safe_result["error_code"] = error_code
        return {
            "ok": False,
            "error_code": error_code,
            "error": "Computer semantic probe protocol failed.",
            "result": safe_result,
            "diagnostics": diagnostics,
        }
    if action == "computer.type" and not _verified_type_result(result):
        diagnostics = _safe_type_diagnostics(result)
        delivery = safe_computer_delivery_facts(result)
        ax_candidate = safe_ax_candidate_facts(result)
        input_dispatched = diagnostics.get("input_dispatched")
        native_code = str(diagnostics.get("error_code") or "")
        if input_dispatched is False and native_code in SAFE_TYPE_PREDISPATCH_ERROR_CODES:
            error_code = native_code
            error_message = "Computer text-input precondition failed."
        elif input_dispatched is True:
            error_code = "TYPE_COMPLETION_NOT_VERIFIED"
            error_message = "Computer text input did not report verified full completion."
        else:
            error_code = "TYPE_DIAGNOSTICS_INVALID"
            error_message = "Computer text-input diagnostics were invalid."
        return {
            "ok": False,
            "error_code": error_code,
            "error": error_message,
            "result": {
                "action": action,
                **delivery,
                **({"ax_candidate": ax_candidate} if ax_candidate else {}),
                "diagnostics": diagnostics,
            },
            "diagnostics": diagnostics,
        }
    if action == "computer.key" and not _approval_pending(result):
        delivery = safe_computer_delivery_facts(result)
        if (
            delivery.get("delivered") is True
            and delivery.get("completion_verified") is not True
        ):
            return {
                "ok": False,
                "error_code": "KEY_EFFECT_NOT_VERIFIED",
                "error": "Computer key input was posted but its focus or effect was not verified.",
                "result": {"action": action, **delivery},
            }
    if action == "computer.screenshot" and not _approval_pending(result):
        screenshot_facts = _verified_screenshot_facts(result, artifact_root)
        if not screenshot_facts["screenshot_contract_valid"]:
            return {
                "ok": False,
                "error_code": "SCREENSHOT_COMPLETION_NOT_VERIFIED",
                "error": "Computer screenshot did not produce a verified artifact.",
                "result": {
                    "action": action,
                    **screenshot_facts,
                    "failure_stage": "helper_contract",
                    "error_code": "SCREENSHOT_COMPLETION_NOT_VERIFIED",
                },
            }
    return {"ok": True, "result": result}


def _approval_pending(result: object) -> bool:
    return isinstance(result, dict) and bool(
        result.get("approval_required") or result.get("requires_approval")
    )


_SCREENSHOT_BOOL_FACTS = (
    "screenshot_supported",
    "target_resolved",
    "capture_attempted",
    "capture_succeeded",
    "artifact_path_present",
    "model_path_present",
    "artifact_file_created",
    "model_file_created",
    "artifact_root_match",
)
_SCREENSHOT_CAPTURE_DRIVERS = frozenset(
    {
        "none",
        "mac_swift_host",
        "mac_screencapture_window",
        "mac_screencapture_rect",
        "mac_screencapture_display",
        "windows_native",
        "linux_native",
    }
)
_SCREENSHOT_TARGET_BINDING_SOURCES = frozenset(
    {
        "explicit_window",
        "explicit_identifiers",
        "enumerated_match",
        "persisted_selection",
        "active_window",
        "none",
    }
)


def _verified_screenshot_facts(
    result: object, artifact_root: Path | None
) -> dict[str, object]:
    value = result if isinstance(result, dict) else {}
    facts: dict[str, object] = {
        key: value.get(key) if isinstance(value.get(key), bool) else False
        for key in _SCREENSHOT_BOOL_FACTS
    }
    capture_driver = str(value.get("capture_driver") or "")
    target_binding_source = str(value.get("target_binding_source") or "")
    facts["capture_driver"] = (
        capture_driver if capture_driver in _SCREENSHOT_CAPTURE_DRIVERS else "none"
    )
    facts["target_binding_source"] = (
        target_binding_source
        if target_binding_source in _SCREENSHOT_TARGET_BINDING_SOURCES
        else "none"
    )

    artifact_present, artifact_created, artifact_root_match = _verified_artifact(
        value.get("screenshot_path") or value.get("path"), artifact_root
    )
    model_present, model_created, model_root_match = _verified_artifact(
        value.get("model_image_path"), artifact_root
    )
    facts["artifact_path_present"] = artifact_present
    facts["model_path_present"] = model_present
    facts["artifact_file_created"] = artifact_created
    facts["model_file_created"] = model_created
    facts["artifact_root_match"] = artifact_root_match and model_root_match
    facts["screenshot_contract_valid"] = bool(
        not value.get("is_error")
        and facts["screenshot_supported"]
        and facts["target_resolved"]
        and facts["capture_attempted"]
        and facts["capture_succeeded"]
        and facts["artifact_path_present"]
        and facts["model_path_present"]
        and facts["artifact_file_created"]
        and facts["model_file_created"]
        and facts["artifact_root_match"]
        and value.get("screenshot_contract_valid") is True
    )
    return facts


def _verified_artifact(
    raw_path: object, artifact_root: Path | None
) -> tuple[bool, bool, bool]:
    path_present = isinstance(raw_path, str) and bool(raw_path.strip())
    if not path_present or artifact_root is None:
        return path_present, False, False
    try:
        candidate = Path(str(raw_path)).expanduser()
        root = artifact_root.expanduser().resolve()
        resolved = candidate.resolve()
        details = candidate.lstat()
        root_match = resolved.is_relative_to(root)
        regular_nonempty = (
            stat.S_ISREG(details.st_mode)
            and not candidate.is_symlink()
            and details.st_size > 0
        )
        return True, bool(root_match and regular_nonempty), root_match
    except (OSError, RuntimeError, ValueError):
        return True, False, False


def _verified_type_result(result: object) -> bool:
    return (
        isinstance(result, dict)
        and result.get("executed") is True
        and result.get("completion_verified") is True
        and not bool(result.get("is_error"))
    )


def _safe_type_diagnostics(result: object) -> dict[str, object]:
    return safe_type_diagnostic_facts(result)


def _validated_artifact_root(raw_value: object) -> Path | None:
    if raw_value is None:
        return None
    value = str(raw_value or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser().resolve()
    if candidate.name != "computer":
        raise ValueError("artifact_root must end with tools/computer.")
    tools_dir = candidate.parent
    workspace_dir = tools_dir.parent
    conversation_dir = workspace_dir.parent
    if tools_dir.name != "tools" or workspace_dir.name != "workspace" or not conversation_dir.name:
        raise ValueError("artifact_root must be inside a conversation workspace tools/computer directory.")
    for root in _allowed_conversation_roots():
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) == 4 and relative.parts[1:] == ("workspace", "tools", "computer"):
            return candidate
    raise ValueError("artifact_root is outside the allowed conversation workspace roots.")


def _allowed_conversation_roots() -> list[Path]:
    roots: list[Path] = []
    override = str(os.environ.get("RUMI_DEFAULTSPACK_CHAT_STORE_PATH") or "").strip()
    if override:
        roots.append(Path(override).expanduser().resolve().parent / "conversations")
    base = Path(__file__).resolve().parents[2]
    roots.append(base / "ecosystem" / "defaultspack" / "user_data" / "shared" / "chat" / "conversations")

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = root.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(resolved)
    return deduped


if __name__ == "__main__":
    raise SystemExit(main())
