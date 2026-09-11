import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from blocks._common import error, ok

from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.ai_client.model_search import get_model_capabilities
from domain.chat.message_converter import convert_to_standard
from domain.chat.modality_detector import detect_modalities
from domain.chat.store import ChatStore
from domain.vision.image_bridge import conversation_image_context, describe_images


def run(input_data, context, *, settings_owner=None):
    store = ChatStore()
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    updates = input_data.get("updates")
    if not updates or not isinstance(updates, dict):
        return error("updates dict is required", "INVALID_INPUT")
    existing = store.get_conversation(conversation_id)
    if existing is not None and "model" in updates:
        updates = _with_model_switch_compatibility(
            store,
            conversation_id,
            existing,
            updates,
            context or {},
            settings_owner=settings_owner,
        )
    conv = store.update_conversation(conversation_id, updates)
    if conv is None:
        return error("Conversation not found", "NOT_FOUND")
    return ok(conv)


def _with_model_switch_compatibility(
    store,
    conversation_id,
    conversation,
    updates,
    context,
    *,
    settings_owner=None,
):
    target_model = str(updates.get("model") or "").strip()
    if not target_model:
        return updates
    capabilities = get_model_capabilities(target_model) or {}
    if capabilities.get("supports_vision"):
        return updates
    metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    existing_context = metadata.get("conversation_image_context") if isinstance(metadata.get("conversation_image_context"), dict) else None
    if existing_context and existing_context.get("valid_for_models_without_vision"):
        return updates
    messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
    has_images = any(detect_modalities(message.get("content"), message.get("metadata")).get("has_images") for message in messages if isinstance(message, dict))
    if not has_images:
        return updates
    settings = ModelRuntimeSettingsService(
        settings_owner=settings_owner
    ).get_settings()
    policy = str(settings.get("on_switch_to_non_vision_with_images") or "auto_bridge")
    if policy == "block":
        raise ValueError("target model does not support vision and conversation contains images")
    if policy == "ignore":
        return updates
    bridge = describe_images(
        messages=convert_to_standard(messages),
        attachments=_image_attachments(messages),
        conversation_context=str(conversation.get("title") or ""),
        model=str(settings.get("utility_models", {}).get("vision_ocr") or ""),
        call_handler=context.get("call_handler") if isinstance(context, dict) else None,
    )
    new_metadata = {
        **metadata,
        "conversation_image_context": conversation_image_context(bridge),
        "model_switch_vision_bridge_result": bridge,
    }
    updated = dict(updates)
    updated["metadata"] = new_metadata
    return updated


def _image_attachments(messages):
    attachments = []
    for message in messages:
        metadata = message.get("metadata") if isinstance(message, dict) and isinstance(message.get("metadata"), dict) else {}
        for item in metadata.get("attachments", []) if isinstance(metadata.get("attachments"), list) else []:
            if isinstance(item, dict) and str(item.get("type") or "").startswith("image/"):
                attachments.append(item)
    return attachments
