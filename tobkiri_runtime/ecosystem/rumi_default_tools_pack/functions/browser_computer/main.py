from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from functions._tool_common import tool_result
from ecosystem.rumi_default_tools_pack import run_host_contract_action

_SEQUENCE_ID_KEYS = (
    "computer_use_haze_sequence_id",
    "computer_use_sequence_id",
    "run_id",
    "request_id",
    "conversation_turn_id",
    "_flow_run_request_id",
    "_flow_execution_id",
)


def run(context, args):
    """Run a browser/computer action through the reviewed host adapter."""

    args = dict(args or {})
    action = str(args.get("action", "browser.session"))
    payload = dict(args.get("payload") or {})
    tool_name = str(args.get("tool_name") or "browser_computer").strip()
    payload = _payload_with_sequence_defaults(payload, context, args)
    tool_arguments = args.get("tool_arguments")
    if not isinstance(tool_arguments, dict) or not tool_arguments:
        tool_arguments = _tool_arguments_from_run_args(args)
    user_requested = bool(isinstance(context, dict) and context.get("user_requested_computer_use"))
    if user_requested and action == "browser.open_url" and not any(
        key in payload for key in ("persistent", "profile_id", "session_id")
    ):
        payload["persistent"] = False
    payload = _normalize_browser_open_url_payload(
        action,
        payload,
        tool_arguments,
    )
    payload = _payload_with_context_defaults(action, payload, context)
    if action == "browser.download.collect" and isinstance(context, dict):
        workspace = str(context.get("conversation_workspace_dir") or "").strip()
        if workspace:
            payload["artifact_root"] = str(Path(workspace) / "tools" / "computer")
    sequence_id = _sequence_id_from_mapping(payload)
    try:
        runner = _run_computer_action()
        result = runner(
            action,
            payload,
            context if isinstance(context, dict) else None,
            tool_name=tool_name or "browser_computer",
            tool_arguments=tool_arguments,
            artifact_root=(
                Path(str(context["conversation_workspace_dir"])) / "tools" / "computer"
                if isinstance(context, dict) and context.get("conversation_workspace_dir")
                else None
            ),
        )
        if not isinstance(result, dict):
            result = {"action": action, "result": result}
        is_error = bool(result.get("is_error")) or result.get("success") is False
        summary = f"{tool_name or 'browser_computer'} {result.get('action', action)}"
        summary += " failed" if is_error else " completed"
        if result.get("reason"):
            summary += f": {result['reason']}"
        if result.get("path"):
            summary += f"; artifact: {result['path']}"
        return tool_result(
            summary,
            widget={"type": tool_name or "browser_computer", **result},
            is_error=is_error,
        )
    finally:
        _end_haze_sequence(sequence_id)


def _run_computer_action():
    """Return the host-contract runner used by the legacy function surface."""

    def run_action(action, payload, context=None, **kwargs):
        source_function_id = str(kwargs.get("tool_name") or "browser_computer")
        del context
        return run_host_contract_action(
            action,
            payload,
            source_function_id=source_function_id,
        )

    return run_action


def _tool_arguments_from_run_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return model arguments when a caller omitted the nested copy."""

    return {
        key: value
        for key, value in args.items()
        if key not in {"tool_name", "tool_arguments"}
    }


def _payload_with_sequence_defaults(
    payload: dict[str, Any] | None,
    context: object,
    args: object,
) -> dict[str, Any]:
    """Attach one stable haze sequence id without overwriting explicit input."""

    normalized = dict(payload or {})
    sequence_id = _sequence_id_from_mapping(normalized) or _sequence_id_from_mapping(args)
    if not sequence_id:
        sequence_id = _sequence_id_from_mapping(context)
    if sequence_id:
        normalized.setdefault("computer_use_haze_sequence_id", sequence_id)
    return normalized


def _sequence_id_from_mapping(value: object) -> str:
    """Read the first supported sequence identifier from a mapping."""

    if not isinstance(value, dict):
        return ""
    for key in _SEQUENCE_ID_KEYS:
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


def _end_haze_sequence(sequence_id: str) -> None:
    """Release a sequence-scoped haze lease after success or interruption."""

    sequence_id = str(sequence_id or "").strip()
    if not sequence_id:
        return
    try:
        try:
            from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import (
                ComputerUseEdgeHazeManager,
            )
        except ImportError:  # pragma: no cover - direct function execution fallback
            from domain.computer.mac.edge_haze import ComputerUseEdgeHazeManager

        pack_root = Path(__file__).resolve().parents[2]
        ComputerUseEdgeHazeManager.from_pack_root(pack_root).end_sequence(sequence_id)
    except Exception:
        # Cleanup is best effort; the helper lease still has a bounded deadline.
        return


def _payload_with_context_defaults(action, payload, context):
    payload = dict(payload or {})
    if not isinstance(context, dict):
        return payload
    if action == "browser.open_url":
        target_app = context.get("computer_use_target_app")
        if isinstance(target_app, str) and target_app.strip() and not any(
            payload.get(key) for key in ("app", "application", "browser", "browser_app")
        ):
            payload["app"] = target_app.strip()
        return payload
    if action.startswith("computer.") and action not in {"computer.windows", "computer.apps"}:
        target_app = context.get("computer_use_target_app")
        target_title = context.get("computer_use_target_title")
        if isinstance(target_app, str) and target_app.strip():
            payload.setdefault("app", target_app.strip())
        if isinstance(target_title, str) and target_title.strip():
            payload.setdefault("title", target_title.strip())
        if context.get("computer_use_physical_clicks") and action in {
            "computer.click",
            "computer.drag",
            "computer.type",
            "computer.key",
            "computer.backspace",
            "computer.scroll",
        }:
            payload.setdefault("physical", True)
    return payload


_URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def _normalize_browser_open_url_payload(action, payload, tool_arguments=None):
    """Promote legacy value/text URL fields without granting extra authority."""

    payload = dict(payload or {})
    if action != "browser.open_url" or payload.get("url"):
        return payload
    candidates = [
        payload.get("value"),
        payload.get("text"),
        payload.get("target"),
        payload.get("href"),
    ]
    if isinstance(tool_arguments, dict):
        candidates.extend(
            [tool_arguments.get("value"), tool_arguments.get("text"), tool_arguments.get("target")]
        )
    for value in candidates:
        match = _URL_PATTERN.search(str(value or ""))
        if match:
            payload["url"] = match.group(0).rstrip(".,);]")
            break
    return payload
