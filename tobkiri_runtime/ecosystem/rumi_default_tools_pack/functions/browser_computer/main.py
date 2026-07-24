from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from domain.tool.host_contract_adapter import run_host_contract_action


def run(context, args):
    action = str(args.get("action", "browser.session"))
    payload = dict(args.get("payload") or {})
    tool_name = str(args.get("tool_name") or "browser_computer").strip()
    user_requested = bool(isinstance(context, dict) and context.get("user_requested_computer_use"))
    if user_requested and action == "browser.open_url" and not any(
        key in payload for key in ("persistent", "profile_id", "session_id")
    ):
        payload["persistent"] = False
    payload = _payload_with_context_defaults(action, payload, context)
    if action == "browser.download.collect" and isinstance(context, dict):
        workspace = str(context.get("conversation_workspace_dir") or "").strip()
        if workspace:
            payload["artifact_root"] = str(Path(workspace) / "tools" / "computer")
    return run_host_contract_action(
        action,
        payload,
        source_function_id=tool_name or "browser_computer",
    )


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
    return payload
