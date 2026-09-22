from __future__ import annotations

import json
import re
from typing import Any

from domain.ai_client.capability_tokens import (
    missing_model_capabilities,
    model_requirements_from_tokens,
    normalize_capability_tokens,
)
from domain.ai_client.gateway import LLMGateway
from domain.ai_client.model_router import ModelRoutingRequest, route_model_request
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.ai_client.model_search import get_model_capabilities, get_profile_catalog
from domain.temporal_context import add_temporal_context_message, current_datetime_context


_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)


def call_model(
    input_data: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    *,
    call_handler: Any = None,
) -> dict[str, Any]:
    payload = dict(input_data or {})
    runtime_context = dict(context or {})
    depth = int(runtime_context.get("_model_call_depth") or payload.get("_model_call_depth") or 0)
    if depth >= 2:
        return {
            "status": "error",
            "code": "MODEL_CALL_DEPTH_EXCEEDED",
            "error": "model.call depth limit exceeded",
        }
    messages = _normalized_messages(payload)
    if not messages:
        return {"status": "error", "code": "MISSING_INPUT", "error": "question or messages is required"}

    required_capabilities = normalize_capability_tokens(
        payload.get("required_capabilities")
        or payload.get("capability")
    )
    model_requirements = model_requirements_from_tokens(required_capabilities)
    model_settings = ModelRuntimeSettingsService().get_settings()
    profiles = get_profile_catalog()
    preferred_model = str(
        payload.get("model_hint")
        or payload.get("model")
        or model_settings.get("preferred_model")
        or "stub/default"
    ).strip() or "stub/default"
    has_images = _messages_have_images(messages) or model_requirements["image_input"]
    has_audio = _messages_have_audio(messages)
    routing_request = ModelRoutingRequest(
        user_text=_messages_text(messages),
        has_images=has_images,
        has_audio=has_audio,
        requires_tool_calling=model_requirements["tool_calling"],
        requires_fast=model_requirements["fast"],
        requested_thinking_level=_requested_thinking_level(payload, required_capabilities),
        preferred_model=preferred_model,
        preferred_group=str(model_settings.get("preferred_model_group") or "default"),
        auto_route_within_group=bool(model_settings.get("auto_route_within_group", True)),
        task_hints=dict(payload.get("task_hints") if isinstance(payload.get("task_hints"), dict) else {}),
        settings=model_settings,
    )
    try:
        decision = route_model_request(routing_request, profiles=profiles)
    except TypeError as exc:
        # Keep compatibility with injected/legacy routers that still expose
        # the original request-only callable contract.
        if "profiles" not in str(exc):
            raise
        decision = route_model_request(routing_request)
    model = decision.selected_model
    try:
        selected_capabilities = get_model_capabilities(model, profiles=profiles) or {}
    except TypeError as exc:
        # Keep compatibility with injected/legacy capability resolvers that
        # still expose the original single-argument callable contract.
        if "profiles" not in str(exc):
            raise
        selected_capabilities = get_model_capabilities(model) or {}
    missing_capabilities = missing_model_capabilities(required_capabilities, selected_capabilities)
    if missing_capabilities:
        return {
            "status": "error",
            "code": "MODEL_CAPABILITY_UNSATISFIED",
            "error": "selected model does not satisfy required capabilities: {}".format(", ".join(missing_capabilities)),
            "model": model,
            "required_capabilities": required_capabilities,
            "missing_capabilities": missing_capabilities,
            "routing": decision.to_dict(),
        }
    params = {
        "max_tokens": _max_tokens(payload),
        "thinking_level": _requested_thinking_level(payload, required_capabilities),
    }
    if payload.get("output_schema"):
        params["response_format"] = {"type": "json_object"}
    temporal_context = current_datetime_context({**runtime_context, **payload})
    runtime_context["current_datetime_context"] = temporal_context
    runtime_context.setdefault("current_datetime", temporal_context["iso"])
    runtime_context.setdefault("current_date", temporal_context["date"])
    runtime_context.setdefault("current_time_zone", temporal_context["timezone"])
    add_temporal_context_message(messages, runtime_context, temporal_context=temporal_context)
    sanitized_messages = _sanitize_value(messages)
    try:
        if call_handler is not None:
            response = call_handler(
                "defaults.ai.complete",
                {
                    "model": model,
                    "messages": sanitized_messages,
                    "tools": [],
                    "params": params,
                },
            )
            response = response.get("data") if isinstance(response, dict) and response.get("status") == "ok" else response
        else:
            response = LLMGateway().complete(
                {
                    "model": model,
                    "messages": sanitized_messages,
                    "tools": [],
                    "params": params,
                }
            )
    except RuntimeError as exc:
        return {"status": "error", "code": "PROVIDER_ERROR", "error": str(exc), "model": model}
    output = _extract_output(response, expect_json=bool(payload.get("output_schema")))
    return {
        "status": "ok",
        "model": model,
        "output": output,
        "response": response,
        "required_capabilities": required_capabilities,
        "routing": decision.to_dict(),
    }


def _normalized_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("messages")
    if isinstance(raw, list) and raw:
        messages = [dict(item) for item in raw if isinstance(item, dict)]
    else:
        question = str(payload.get("question") or payload.get("prompt") or payload.get("input") or "").strip()
        if not question:
            return []
        messages = [{"role": "user", "content": question}]
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    if attachments and messages:
        last = dict(messages[-1])
        content = last.get("content")
        if isinstance(content, str):
            blocks: list[Any] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = list(content)
        else:
            blocks = [{"type": "text", "text": str(content or "")}]
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            mime = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
            data_url = attachment.get("dataUrl") or attachment.get("data_url")
            if mime.startswith("image/") and isinstance(data_url, str) and data_url.startswith("data:image/"):
                blocks.append({"type": "image_url", "image_url": {"url": data_url}})
            else:
                blocks.append({"type": "text", "text": f"[attachment] {attachment.get('name') or mime or 'file'}"})
        last["content"] = blocks
        messages[-1] = last
    return messages


def _requested_thinking_level(payload: dict[str, Any], required_capabilities: list[str]) -> str:
    explicit = str(payload.get("thinking_level") or payload.get("requested_thinking_level") or "").strip()
    if explicit:
        return explicit
    if "model.thinking" in set(required_capabilities):
        return "medium"
    return "none"


def _max_tokens(payload: dict[str, Any]) -> int:
    try:
        return max(32, min(int(payload.get("max_tokens") or 800), 8_000))
    except (TypeError, ValueError):
        return 800


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                continue
            sanitized[key] = _sanitize_value(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _extract_output(response: Any, *, expect_json: bool) -> Any:
    text = _response_text(response)
    if expect_json and text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_text": text}
    return text or response


def _response_text(response: Any) -> str:
    if not isinstance(response, dict):
        return str(response or "")
    content = response.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part).strip()
    data = response.get("data")
    if isinstance(data, dict):
        return _response_text(data)
    return ""


def _messages_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _messages_have_images(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").casefold()
            image_url = block.get("image_url")
            if block_type in {"image", "image_url", "input_image"}:
                return True
            if isinstance(image_url, dict) and str(image_url.get("url") or "").startswith("data:image/"):
                return True
    return False


def _messages_have_audio(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").casefold()
            if block_type in {"audio", "input_audio"}:
                return True
    return False
