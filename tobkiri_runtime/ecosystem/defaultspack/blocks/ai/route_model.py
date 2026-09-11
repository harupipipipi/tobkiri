import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.ai_client.model_router import ModelRoutingRequest, route_model_request
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService


def _message_text(message):
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            elif isinstance(item, str) and item.strip():
                parts.append(item)
        return "\n".join(parts).strip()
    return str(content or "")


def _requested_tools(value):
    items = value if isinstance(value, list) else []
    result = []
    for item in items:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            name = (
                item.get("name")
                or item.get("tool_name")
                or item.get("tool_id")
                or (item.get("function") or {}).get("name")
            )
            if isinstance(name, str) and name.strip():
                result.append(name.strip())
    return result


def run(input_data, context, *, settings_owner=None):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    settings = (
        data.get("settings")
        if isinstance(data.get("settings"), dict)
        else ModelRuntimeSettingsService(
            settings_owner=settings_owner
        ).get_settings()
    )
    modalities = data.get("modalities") if isinstance(data.get("modalities"), dict) else {}
    tools = _requested_tools(data.get("tools"))
    request = ModelRoutingRequest(
        conversation_id=str(data.get("conversation_id") or ""),
        user_text=_message_text(data.get("message")),
        has_images=bool(modalities.get("has_images")),
        has_files=bool(modalities.get("has_files")),
        requested_tools=tools,
        requires_tool_calling=bool(data.get("requires_tool_calling") or tools),
        requires_fast=bool(data.get("requires_fast")),
        requested_thinking_level=data.get("requested_thinking_level"),
        preferred_model=str(
            data.get("preferred_model")
            or data.get("model")
            or settings.get("preferred_model")
            or "stub/default"
        ).strip() or "stub/default",
        preferred_group=str(
            data.get("preferred_group")
            or settings.get("preferred_model_group")
            or "default"
        ).strip() or "default",
        auto_route_within_group=bool(data.get("auto_route_within_group", settings.get("auto_route_within_group", True))),
        task_hints=dict(data.get("task_hints") if isinstance(data.get("task_hints"), dict) else {}),
        settings=settings,
    )
    return ok(route_model_request(request).to_dict())
