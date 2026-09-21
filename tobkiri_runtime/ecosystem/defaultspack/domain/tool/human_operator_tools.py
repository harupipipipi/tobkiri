from __future__ import annotations

import secrets
import time
from typing import Any

from domain.ai_client.provider_trace import redact_sensitive_value
from domain.human_operator.constants import HUMAN_OPERATOR_MODEL, HUMAN_OPERATOR_TOOL_NAME
from domain.human_operator.session_store import absolute_session_url, save_session
from domain.prompt.studio_client import compact_prompt_via_owner

from ._agent_os_common import err, ok
from .schema_adapter import list_or_empty, mapping_or_empty


def human_operator_canvas_open(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    invoke_context = dict(context or {})
    conversation_id = str(invoke_context.get("conversation_id") or arguments.get("conversation_id") or "").strip()
    if not conversation_id:
        return err("conversation_id is required", "INVALID_INPUT")

    session_id = str(arguments.get("session_id") or "").strip()
    if not session_id:
        return err("session_id is required", "INVALID_INPUT")

    messages = list_or_empty(arguments.get("messages"))
    params = mapping_or_empty(arguments.get("params"))
    tool_names = [
        str(item).strip()
        for item in list_or_empty(arguments.get("tool_names"))
        if str(item or "").strip()
    ]
    system_prompt = _system_prompt_from_messages(messages)
    compacted_prompt = compact_prompt_via_owner(system_prompt) if system_prompt else {}
    payload = {
        "session_id": session_id,
        "conversation_id": conversation_id,
        "created_at": int(time.time() * 1000),
        "title": str(arguments.get("note") or "Human Operator Canvas").strip() or "Human Operator Canvas",
        "command": str(arguments.get("command") or "/start").strip() or "/start",
        "note": str(arguments.get("note") or "").strip(),
        "operator_id": str(arguments.get("operator_id") or invoke_context.get("operator_id") or "").strip(),
        "operator_marker": (
            str(arguments.get("operator_marker") or invoke_context.get("operator_marker") or "").strip()
            or "local_human_operator"
        ),
        "csrf_token": secrets.token_urlsafe(32),
        "launch_snapshot": redact_sensitive_value(
            {
                "model": str(arguments.get("model") or invoke_context.get("model") or HUMAN_OPERATOR_MODEL),
                "system_prompt": system_prompt,
                "prompt_compact": compacted_prompt,
                "messages": messages,
                "params": params,
                "tool_names": tool_names,
                "context": _context_snapshot(invoke_context),
            }
        ),
    }
    save_session(conversation_id, session_id, payload)
    local_url = absolute_session_url(conversation_id, session_id, view="readable", prompt_view="original")
    return ok(
        {
            "status": "ok",
            "tool_name": HUMAN_OPERATOR_TOOL_NAME,
            "human_operator_canvas": True,
            "session_id": session_id,
            "conversation_id": conversation_id,
            "title": payload["title"],
            "local_url": local_url,
            "summary": "Human Operator Canvas session is ready.",
        },
        message="Human Operator Canvas opened",
    )


def _context_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "conversation_id",
        "conversation_workspace_dir",
        "history_json_path",
        "history_path",
        "model",
        "chat_params",
        "provider_capabilities",
        "provider_planning",
        "profile_policy",
        "runtime_profile_key",
        "active_startup_profile_id",
        "profile_graph_selection",
        "knowledge_text",
        "memory_text",
        "matched_skill_instructions",
        "capability_graph",
    }
    return {key: context.get(key) for key in keep if key in context}


def _system_prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            text = "\n".join(part for part in parts if part).strip()
            if text:
                return text
    return ""
