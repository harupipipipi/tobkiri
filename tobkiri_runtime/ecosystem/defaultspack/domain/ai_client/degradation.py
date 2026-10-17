from __future__ import annotations

from copy import deepcopy
from typing import Any

from domain.ai_client.bridge_plan import PlannedProviderRequest
from domain.chat.ir import RumiChatIR, RumiIRMessage
from domain.chat.ir_blocks import BridgeAction, DroppedFeature, ProviderWarning, RumiIRBlock
from domain.tool.provider_adapter import adapt_rumi_tools_to_provider_tools


NON_VISION_IMAGE_PLACEHOLDER = (
    "[Image omitted: the selected provider does not support vision input. "
    "Use a vision-capable model or bridge result for image details.]"
)
INTERNAL_RUMI_PARAMS = {
    "rumi_base_model_override",
    "rumi_require_intended_base_model",
}


def degrade_request(
    ir: RumiChatIR,
    *,
    model: str,
    provider_capabilities: dict[str, Any],
    tools: list[dict[str, Any]],
    params: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> PlannedProviderRequest:
    caps = dict(provider_capabilities or {})
    working_ir = deepcopy(ir)
    working_params = dict(params or {})
    _drop_internal_rumi_params(working_params)
    warnings: list[ProviderWarning] = []
    dropped: list[DroppedFeature] = []
    bridges: list[BridgeAction] = []

    _degrade_roles(working_ir, caps, warnings, bridges)
    _degrade_modalities(working_ir, caps, warnings, dropped, bridges)
    _degrade_reasoning(working_ir, caps, working_params, warnings, dropped)

    provider_tools = list(tools or [])
    tool_mapping = None
    provider_tool_defs = []
    if provider_tools and not caps.get("supports_tool_calling"):
        requested = [_tool_name(tool) for tool in provider_tools if _tool_name(tool)]
        warnings.append(
            ProviderWarning(
                code="requested_tools_without_provider_attachment",
                message="Provider does not support tool calling; tools were not attached.",
                metadata={"requested_tools": requested},
            )
        )
        provider_tools = []
    elif provider_tools:
        provider_tools, tool_mapping, provider_tool_defs = adapt_rumi_tools_to_provider_tools(provider_tools, caps)

    if provider_tools and not caps.get("supports_parallel_tool_calls", False):
        working_params["parallel_tool_calls"] = False
        warnings.append(
            ProviderWarning(
                code="parallel_tool_calls_serialized",
                message="Provider does not support parallel tool calls; the tool loop will run sequentially.",
            )
        )

    response_format = working_params.get("response_format")
    if isinstance(response_format, dict):
        json_schema = response_format.get("json_schema")
        strict = response_format.get("strict")
        if isinstance(json_schema, dict):
            strict = json_schema.get("strict", strict)
        quirks = caps.get("quirks") if isinstance(caps.get("quirks"), dict) else {}
        if strict and not quirks.get("supports_strict_json_schema", False):
            working_params["response_format"] = {**response_format, "strict": False}
            warnings.append(
                ProviderWarning(
                    code="strict_json_schema_downgraded",
                    message="Provider does not support strict JSON schema; using best-effort JSON with post-validation.",
                )
            )

    metadata = {
        "context": dict(context or {}),
        "tool_name_mapping": tool_mapping.to_dict() if tool_mapping is not None else {},
        "provider_tool_definitions": [
            {"name": item.name, "provider_alias": item.provider_alias}
            for item in provider_tool_defs
        ],
    }
    return PlannedProviderRequest(
        ir=working_ir,
        model=model,
        provider_capabilities=caps,
        provider_tools=provider_tools,
        params=working_params,
        bridge_actions=bridges,
        dropped_features=dropped,
        warnings=warnings,
        metadata=metadata,
    )


def _degrade_roles(ir: RumiChatIR, caps: dict[str, Any], warnings: list[ProviderWarning], bridges: list[BridgeAction]) -> None:
    supported = {str(role) for role in caps.get("supported_roles", [])}
    if not supported:
        return
    developer_messages = [message for message in ir.messages if message.role == "developer" and "developer" not in supported]
    if developer_messages:
        system_text = "\n\n".join(_message_text(message) for message in developer_messages if _message_text(message))
        ir.messages = [message for message in ir.messages if message.role != "developer"]
        if system_text:
            system = _first_role(ir, "system")
            if system is None:
                ir.messages.insert(
                    0,
                    RumiIRMessage(
                        conversation_id=ir.conversation_id,
                        role="system",
                        content=[RumiIRBlock(type="text", text="[Developer instructions]\n" + system_text)],
                    ),
                )
            else:
                system.content.append(RumiIRBlock(type="text", text="[Developer instructions]\n" + system_text))
        warnings.append(ProviderWarning(code="developer_role_merged", message="Developer role is unsupported and was merged into system."))

    if "system" not in supported:
        system_messages = [message for message in ir.messages if message.role == "system"]
        if system_messages:
            prefix = "\n\n".join(_message_text(message) for message in system_messages if _message_text(message))
            ir.messages = [message for message in ir.messages if message.role != "system"]
            user = _first_role(ir, "user")
            if user is None:
                ir.messages.insert(
                    0,
                    RumiIRMessage(
                        conversation_id=ir.conversation_id,
                        role="user",
                        content=[RumiIRBlock(type="text", text="[System instructions]\n" + prefix)],
                    ),
                )
            else:
                user.content.insert(0, RumiIRBlock(type="text", text="[System instructions]\n" + prefix))
            bridges.append(BridgeAction(action="system_prefix_injected", reason="Provider does not support system role."))


def _degrade_modalities(
    ir: RumiChatIR,
    caps: dict[str, Any],
    warnings: list[ProviderWarning],
    dropped: list[DroppedFeature],
    bridges: list[BridgeAction],
) -> None:
    supported_blocks = {str(block) for block in caps.get("supported_content_blocks", [])}
    supports_vision = bool(caps.get("supports_vision") or caps.get("supports_image_input"))
    vision_bridge_added = False
    image_warning_added = False
    for message in ir.messages:
        degraded_content: list[RumiIRBlock] = []
        for block in message.content:
            block_type = block.type
            if block_type in {"image", "image_url"} and not supports_vision:
                if not vision_bridge_added:
                    bridges.append(BridgeAction(action="vision_bridge_required", reason="Provider does not support images."))
                    vision_bridge_added = True
                if not image_warning_added:
                    warnings.append(ProviderWarning(code="image_bridge_required", message="Image content requires a vision bridge for this provider."))
                    image_warning_added = True
                dropped.append(
                    DroppedFeature(
                        feature=block_type,
                        reason="Provider does not support image input; image content was removed from the provider payload.",
                        source="request_planner",
                    )
                )
                degraded_content.append(RumiIRBlock(type="text", text=NON_VISION_IMAGE_PLACEHOLDER))
                continue
            elif block_type == "pdf" and not caps.get("supports_pdf"):
                bridges.append(BridgeAction(action="extract_pdf_text_and_page_images", reason="Provider does not support native PDF."))
            elif block_type in {"audio", "input_audio"} and not caps.get("supports_audio"):
                bridges.append(BridgeAction(action="transcription_bridge_required", reason="Provider does not support audio."))
            elif block_type == "file" and not caps.get("supports_file_upload"):
                if block.text or block.data.get("text"):
                    bridges.append(BridgeAction(action="inline_file_text", reason="Provider does not support file upload."))
                else:
                    warnings.append(ProviderWarning(code="file_upload_unavailable", message="File upload is unsupported; passing workspace reference only."))
            elif supported_blocks and block_type not in supported_blocks and block_type not in {"unknown", "event", "citation", "refusal"}:
                dropped.append(DroppedFeature(feature=block_type, reason="Provider does not support this content block.", source="request_planner"))
            degraded_content.append(block)
        message.content = degraded_content


def _degrade_reasoning(
    ir: RumiChatIR,
    caps: dict[str, Any],
    params: dict[str, Any],
    warnings: list[ProviderWarning],
    dropped: list[DroppedFeature],
) -> None:
    requested_level = str(params.get("thinking_level") or params.get("reasoning_effort") or "").strip().lower()
    has_reasoning_blocks = any(block.type == "reasoning" for message in ir.messages for block in message.content)
    if caps.get("supports_reasoning"):
        return
    if requested_level and requested_level != "none":
        params["thinking_level"] = "none"
        params.pop("reasoning_effort", None)
        dropped.append(DroppedFeature(feature="reasoning", reason="Provider does not support reasoning controls.", source="request_params"))
        warnings.append(ProviderWarning(code="reasoning_disabled", message="Reasoning controls were disabled for this provider."))
    if has_reasoning_blocks:
        dropped.append(DroppedFeature(feature="reasoning_blocks", reason="Hidden reasoning blocks are not model-visible for this provider.", source="chat_ir"))


def _drop_internal_rumi_params(params: dict[str, Any]) -> None:
    for key in INTERNAL_RUMI_PARAMS:
        params.pop(key, None)


def _first_role(ir: RumiChatIR, role: str) -> RumiIRMessage | None:
    for message in ir.messages:
        if message.role == role:
            return message
    return None


def _message_text(message: RumiIRMessage) -> str:
    return "\n".join(block.text for block in message.content if block.type == "text" and block.text)


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return str(tool or "")
    function_def = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(function_def.get("name") or tool.get("name") or tool.get("tool_id") or "")
