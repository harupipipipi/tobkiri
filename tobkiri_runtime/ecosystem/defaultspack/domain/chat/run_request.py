from __future__ import annotations

import base64
import copy
import importlib
import json
from functools import lru_cache
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import gen_id
from core_runtime.authority.principal import build_principal_id
from domain.ai_client.capabilities.registry import get_model_provider_capabilities
from blocks.chat._context_helpers import enrich_messages, extract_user_text
from domain.capabilities.runtime_snapshot import build_runtime_capability_snapshot
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.ai_client.model_router import ModelRoutingRequest, route_model_request
from domain.ai_client.model_search import get_model_capabilities
from domain.ai_client.request_planner import plan_model_request
from domain.capability.models import stable_revision
from domain.capability.orchestrator import CapabilityOrchestrator
from domain.capability.repository import CapabilityRepository
from domain.chat.ir import RumiChatIR
from domain.chat.ir_blocks import IR_SCHEMA_VERSION
from domain.chat.ir_legacy_adapter import (
    ir_to_legacy_standard_messages,
    legacy_standard_messages_to_ir,
    stored_messages_to_ir,
)
from domain.chat.modality_detector import detect_modalities
from domain.chat.progress_tool import assistant_progress_system_instruction, with_assistant_progress_tool
from domain.chat.public_metadata import compact_tool_filter_entries
from domain.chat.store import ChatStore
from domain.chat.tool_selection_schema import (
    TOOL_SELECTION_MODES,
    TOOL_SELECTION_SCOPES,
    TOOL_SELECTION_STRATEGIES,
    normalize_tool_target,
    normalize_tool_targets,
)
from domain.mention import extract_mention_values, is_mention_start
from domain.chat.tool_selection_service import ToolSelectionService
from domain.chat.tool_selection_preview import (
    ToolSelectionPreviewAccessError,
    ToolSelectionPreviewStore,
    preview_payload_bindings,
)
from domain.frontend_settings import frontend_settings_path
from domain.human_operator.constants import HUMAN_OPERATOR_TOOL_NAME, is_human_operator_model
from domain.vision.image_bridge import (
    apply_vision_bridge_to_messages,
    conversation_image_context,
    describe_images,
)
from domain.chat.tool_recommender import (
    effective_tool_assist_mode,
    recommend_tool_ids,
    tool_assist_limit,
)
from domain.coding.frontend_precision import (
    PRECISION_TOOL_NAMES,
    detect_frontend_request,
    precision_metadata,
)
from domain.prompt.manager import get_manager
from domain.temporal_context import add_temporal_context_message, current_datetime_context
from domain.tool.loading import split_tools_by_loading
from domain.tool.catalog_contract_client import ContractToolCatalog as ToolRegistry
from domain.tool.eligibility import filter_tool_definitions_by_eligibility
from domain.tool.schema_adapter import (
    adapt_tool_definitions,
    build_tool_execution_context,
    connected_tool_names,
    filter_tool_definitions_for_runtime_profile,
    policy_from_context,
    resolve_runtime_profile_context,
    tool_name_from_definition,
)


MAX_ATTACHMENT_TEXT_CHARS = 240_000
MAX_ATTACHMENT_TEXT_CHARS_PER_FILE = 120_000
MAX_ATTACHMENT_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENT_AUDIO_BYTES = 25 * 1024 * 1024
_DATA_IMAGE_PREFIX = "data:image/"
_DATA_AUDIO_PREFIX = "data:audio/"
_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VECTOR_TOOL_ASSIST_PROFILE_IDS = {"defaultspack.mimo_coding_company"}
_COMPUTER_USE_REQUEST_RE = re.compile(
    r"compute[\s_-]*use|compu?ter[\s_-]*use|computer\s+ツール|コンピューター操作|pc操作|"
    r"(google\s*chrome|chrome|chatgpt|atlas|アトラス|vivaldi|vivladi|line|ブラウザ|browser).{0,80}(操作|送信|入力|input|クリック|開いて|開く|open)",
    re.IGNORECASE,
)
_EXPLICIT_BROWSER_NAVIGATION_RE = re.compile(
    r"(https?://|www\.|google\.com|youtube\.com).{0,160}"
    r"(->|→|youtube|google|検索|入力|input|開いて|開く|open|再生|play)",
    re.IGNORECASE,
)
_COMPUTER_USE_CHROME_TARGET_RE = re.compile(
    r"google\s*chrome|chrome|グーグル\s*クローム|クローム", re.IGNORECASE
)
_COMPUTER_USE_CHROME_NEGATED_RE = re.compile(
    r"(google\s*chrome|chrome|グーグル\s*クローム|クローム).{0,16}"
    r"(使わない|使わず|禁止|not\s+use|do\s+not\s+use|don't\s+use)",
    re.IGNORECASE,
)
_COMPUTER_USE_VIVALDI_TARGET_RE = re.compile(
    r"vivaldi|vivladi|ヴィヴァルディ|ビバルディ", re.IGNORECASE
)
_COMPUTER_USE_ATLAS_TARGET_RE = re.compile(r"chat\s*gpt\s*atlas|chatgpt\s*atlas|atlas|ａｔｌａｓ|アトラス", re.IGNORECASE)
_COMPUTER_USE_LINE_TARGET_RE = re.compile(r"(?<![A-Za-z])line(?![A-Za-z])|ライン", re.IGNORECASE)
_COMPUTER_USE_CHATGPT_TARGET_RE = re.compile(r"chat\s*gpt|chatgpt", re.IGNORECASE)
_COMPUTER_USE_PHYSICAL_INPUT_RE = re.compile(
    r"mouse|keyboard|key\s*board|physical|real\s+(?:ui|mouse|keyboard|click)|foreground|"
    r"マウス|キーボード|キー入力|物理|実操作|実際に|クリック|入力",
    re.IGNORECASE,
)
_TOOL_MENTION_RE = re.compile(r"@([A-Za-z0-9_.:-]+)")
_COMPUTER_USE_TOOL_IDS = {"computer_use", "browser_computer", "browser_use"}
_COMPUTER_NAVIGATION_PROVIDER_DISABLED_ACTIONS = {
    "apps",
    "browser.session",
    "computer.apps",
    "context",
    "computer.context",
    "diagnose",
    "computer.diagnose",
    "doctor",
    "computer.doctor",
    "windows",
    "computer.windows",
}
_CODING_PR_REQUEST_RE = re.compile(
    r"pull\s*request|draft\s*pr|github\.com|git\s*hub|プルリク|"
    r"(?<![A-Za-z])pr(?![A-Za-z]).{0,24}(出して|作って|作成|開いて|open|create)|"
    r"(出して|作って|作成|開いて|open|create).{0,24}(?<![A-Za-z])pr(?![A-Za-z])",
    re.IGNORECASE,
)
_CODING_PR_TOOL_IDS = [
    "coding_file_list",
    "coding_file_search",
    "coding_file_read",
    "coding_file_write",
    "coding_terminal_exec",
    "coding_git_status",
    "coding_git_diff",
    "coding_git_commit",
    "coding_git_push",
    "coding_github_pr_create",
]

# Empty conversations are routed through the catalog-owned Rumi profile.  The
# persisted model settings default (``stub/default``) remains available for
# explicit local-provider selection, but is not a user-facing chat default.
DEFAULT_CHAT_MODEL = "rumi/rumi"


def _is_provider_qualified_model(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and "/" in text and not text.startswith("modelpack/"))


def _apply_provider_surface_defaults(
    provider_capabilities: dict[str, Any],
    model_capabilities: dict[str, Any],
) -> dict[str, Any]:
    if model_capabilities:
        return provider_capabilities
    api_surface = (
        provider_capabilities.get("api_surface")
        if isinstance(provider_capabilities.get("api_surface"), dict)
        else {}
    )
    updated = dict(provider_capabilities)
    if api_surface.get("supports_tool_call_shape") and not updated.get("supports_tool_calling"):
        updated["supports_tool_calling"] = True
    if (
        api_surface.get("supports_parallel_tool_call_shape")
        and not updated.get("supports_parallel_tool_calls")
    ):
        updated["supports_parallel_tool_calls"] = True
    return updated


def _runtime_model_capabilities(
    model_capabilities: dict[str, Any],
    provider_capabilities: dict[str, Any],
) -> dict[str, Any]:
    if model_capabilities:
        return model_capabilities
    runtime_caps: dict[str, Any] = {}
    for key in (
        "supports_tool_calling",
        "supports_vision",
        "supports_image_input",
        "supports_thinking",
        "supports_fast",
    ):
        if provider_capabilities.get(key):
            runtime_caps[key] = provider_capabilities.get(key)
    if provider_capabilities.get("supports_reasoning"):
        runtime_caps["supports_thinking"] = True
    return runtime_caps


_AUTHORITY_FOLLOWUP_PERMISSION_IDS = frozenset(
    {"model.invoke", "api_key.use", "network.egress"}
)
_TOOL_SELECTION_MODES = {"auto", "manual", "none"}
_TOOL_SELECTION_SCOPES = {"turn"}
_TOOL_DISCOVERY_FALLBACK_IDS = {"tool_names", "tool_search"}

@dataclass
class NormalizedToolSelection:
    mode: str = "auto"
    strategy: str | None = None
    include: list[Any] = field(default_factory=list)
    exclude: list[Any] = field(default_factory=list)
    scope: str = "turn"
    must_use: bool = False
    review: bool = False
    preview_id: str | None = None
    source: str = "default"


@dataclass
class PreparedChatRun:
    conversation_id: str
    conversation: dict[str, Any]
    input_data: dict[str, Any]
    request_id: str
    content: list[Any]
    metadata: dict[str, Any] | None
    user_message: dict[str, Any]
    model: str
    params: dict[str, Any]
    request_context: dict[str, Any]
    tool_context: dict[str, Any]
    standard_messages: list[dict[str, Any]]
    user_text: str
    system_prompt: str
    enrich_info: dict[str, Any]
    raw_tools: list[dict[str, Any]]
    provider_tools: list[dict[str, Any]]
    tools_called: list[str]
    connected_tool_names: set[str]
    call_handler: Any
    model_routing: dict[str, Any]
    chat_ir: RumiChatIR = field(default_factory=RumiChatIR)
    provider_chat_ir: RumiChatIR = field(default_factory=RumiChatIR)
    ir_schema_version: str = IR_SCHEMA_VERSION
    provider_planning: dict[str, Any] = field(default_factory=dict)
    provider_capabilities: dict[str, Any] = field(default_factory=dict)
    chat_references: dict[str, Any] = field(default_factory=dict)
    matched_skills: list[dict[str, Any]] = field(default_factory=list)


def validate_chat_run_input(input_data: dict[str, Any]) -> str | None:
    if not isinstance(input_data, dict):
        return "input_data dict is required"
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return "conversation_id is required"
    message = input_data.get("message")
    if not message or not isinstance(message, dict):
        return "message dict is required"
    raw_content = message.get("content")
    attachments = message.get("attachments")
    has_attachments = isinstance(attachments, list) and len(attachments) > 0
    if (raw_content is None or raw_content == "") and not has_attachments:
        return "message content must not be empty"
    if isinstance(raw_content, list) and len(raw_content) == 0 and not has_attachments:
        return "message content must not be empty"
    tool_selection_error = _validate_tool_selection_input(input_data)
    if tool_selection_error:
        return tool_selection_error
    return None


def _resolve_template_tool_policy(
    request_policy: dict[str, Any] | None,
    *,
    metadata: dict[str, Any] | None = None,
) -> Any:
    resolver_module = importlib.import_module("domain.templates.tool_policy_resolution")
    return resolver_module.resolve_template_tool_policy(request_policy, metadata=metadata)


def prepare_chat_run(
    input_data: dict[str, Any], context: dict[str, Any] | None = None
) -> PreparedChatRun:
    validation_error = validate_chat_run_input(input_data if isinstance(input_data, dict) else {})
    if validation_error:
        raise ValueError(validation_error)
    store = ChatStore()
    conversation_id = str(input_data.get("conversation_id") or "")
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise ValueError("Conversation not found")
    conversation_metadata = conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    if conversation_metadata.get("shared_read_only") is True:
        raise ValueError("This imported conversation is read-only. Create a continue copy to send messages.")
    active_startup_profile = _load_active_v4_profile()

    message = input_data.get("message") if isinstance(input_data.get("message"), dict) else {}
    top_level_metadata = input_data.get("metadata") if isinstance(input_data.get("metadata"), dict) else {}
    if top_level_metadata:
        message = dict(message)
        message_metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        message["metadata"] = {**top_level_metadata, **message_metadata}
    content, metadata, runtime_content = _prepared_user_content(store, conversation_id, message)
    chat_references = _chat_references(store, conversation_id, metadata)
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.setdefault("chat_references", chat_references)
    authority_resume_followup = _hidden_authority_followup_from_metadata(
        metadata
    ) is not None
    user_message_dict = {
        "role": message.get("role", "user"),
        "content": content,
        "metadata": metadata or None,
    }
    if "parent_id" in message:
        parent_id = str(message.get("parent_id") or "").strip()
        if parent_id:
            user_message_dict["parent_id"] = parent_id
    user_message = store.add_message(conversation_id, user_message_dict)
    if user_message is None:
        raise RuntimeError("Failed to add user message")
    current_user_message_hidden = _message_hidden_from_model(user_message)

    user_message_for_ir = dict(user_message)
    if runtime_content is not None:
        user_message_for_ir["content"] = runtime_content

    if _current_turn_history_only(context):
        message_chain = [user_message_for_ir]
    else:
        message_chain = store.get_message_chain(conversation_id, user_message["id"])
        if runtime_content is not None:
            message_chain = [
                user_message_for_ir if item.get("id") == user_message.get("id") else item
                for item in message_chain
            ]
    model_message_chain = _model_visible_message_chain(
        message_chain,
        preserve_hidden_tool_progress=authority_resume_followup,
    )
    chat_ir = stored_messages_to_ir(conversation_id, model_message_chain)
    standard_messages = ir_to_legacy_standard_messages(chat_ir)
    runtime_content = _runtime_user_content_override(metadata)
    if runtime_content and not current_user_message_hidden:
        _replace_current_user_content_for_model(
            standard_messages,
            role=str(user_message.get("role") or message.get("role") or "user"),
            runtime_content=runtime_content,
        )
    model = str((conversation or {}).get("model") or DEFAULT_CHAT_MODEL)
    request_id = gen_id()

    manager = get_manager()
    system_prompt = _conversation_system_prompt(conversation, manager)
    raw_user_text = extract_user_text(content)
    user_text = raw_user_text
    if _message_hidden_from_model(user_message):
        visible_user_text = _last_visible_user_text(model_message_chain)
        if visible_user_text:
            user_text = visible_user_text
    inferred_tool_ids = _infer_requested_tools_from_message(user_text)
    effective_inferred_tool_ids = (
        [] if _has_explicit_selected_tools(input_data) else inferred_tool_ids
    )
    prepared_input = _with_inferred_tools(input_data, effective_inferred_tool_ids)

    try:
        enrich_info = enrich_messages(
            standard_messages, system_prompt, conversation_id, user_text, manager
        )
    except Exception:
        enrich_info = {
            "knowledge_text": "",
            "memory_text": "",
            "knowledge_results": [],
            "memory_results": [],
            "enriched_prompt": system_prompt,
        }
        if system_prompt:
            standard_messages.insert(0, {"role": "system", "content": system_prompt})
    if system_prompt and (not standard_messages or standard_messages[0].get("role") != "system"):
        standard_messages.insert(0, {"role": "system", "content": system_prompt})
    chat_reference_prompt = _format_chat_references_for_prompt(chat_references)

    params = dict(prepared_input.get("params") or {})
    tool_selection = _normalize_tool_selection(prepared_input)
    requested_model = str(params.get("model") or params.get("profile_id") or "").strip()
    tool_selection = _apply_tool_selection_preview_snapshot(
        tool_selection,
        context if isinstance(context, dict) else {},
        conversation_id=conversation_id,
        input_data=prepared_input,
        user_text=user_text,
        model=requested_model or model,
    )
    params.pop("tool_selection", None)
    if requested_model:
        model = requested_model
    model_settings_service = ModelRuntimeSettingsService()
    model_settings = model_settings_service.get_settings()
    route_override = _consume_turn_model_route_override(
        store, conversation_id, conversation, metadata
    )
    preferred_group_override = (
        str(route_override.get("preferred_group") or "").strip()
        if isinstance(route_override, dict)
        else ""
    )
    requested_route_model = (
        str(route_override.get("preferred_model") or "").strip()
        if isinstance(route_override, dict)
        else ""
    )
    if requested_route_model and not requested_model:
        model = requested_route_model
    if "thinking_level" not in params:
        params["thinking_level"] = (
            str(route_override.get("requested_thinking_level") or "").strip()
            if isinstance(route_override, dict)
            and str(route_override.get("requested_thinking_level") or "").strip()
            else model_settings_service.get_effective_thinking_level(
                profile_id=model,
                conversation_id=conversation_id,
            )["level"]
        )
    if "deepthink_enabled" not in params:
        params["deepthink_enabled"] = bool(model_settings.get("deepthink_enabled", False))

    request_context = _merge_active_startup_profile_context(context or {}, active_startup_profile)
    requested_tool_ids_for_policy = _requested_tool_ids_from_selection(tool_selection)
    _apply_requested_tool_policy(request_context, requested_tool_ids_for_policy)
    if _has_computer_use_tool(effective_inferred_tool_ids) or _has_computer_use_tool(
        requested_tool_ids_for_policy
    ):
        request_context["user_requested_computer_use"] = True
        request_context = _apply_computer_use_context_preferences(request_context, user_text)
    request_context["conversation_id"] = conversation_id
    request_context["conversation_workspace_dir"] = str(
        store.conversation_workspace_dir(conversation_id)
    )
    request_context["chat_references"] = chat_references
    request_context["history_json_path"] = chat_references["history_json_path"]
    request_context["user_text"] = user_text
    request_context["conversation_user_text"] = _conversation_user_text_for_context(standard_messages)
    request_context["model"] = model
    request_context["chat_params"] = params
    request_context["request_id"] = request_id
    request_context["tool_selection"] = _tool_selection_metadata(tool_selection)
    _copy_enriched_context_into_request_context(request_context, enrich_info)
    if isinstance(metadata, dict) and any(
        key in metadata for key in ("skills", "skill_ids", "selected_skills")
    ):
        # Client metadata is an untrusted rendering hint. The backend resolves
        # explicit Skills again from the canonical message text.
        request_context["ignored_client_skill_metadata"] = True
    conversation_metadata = (
        conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    )
    if isinstance(conversation_metadata.get("tool_preferences"), dict):
        request_context["conversation_tool_preferences"] = conversation_metadata["tool_preferences"]
    resolved_profile_id = str(
        request_context.get("profile_id")
        or ""
    ).strip()
    requested_profile_id = str(
        metadata.get("profile_id") or conversation_metadata.get("profile_id") or ""
    ).strip()
    if requested_profile_id and requested_profile_id != resolved_profile_id:
        request_context["ignored_requested_profile_id"] = requested_profile_id
    if resolved_profile_id:
        request_context["profile_id"] = resolved_profile_id
        _hydrate_profile_policy_from_profile_id(request_context, resolved_profile_id)
    resolved_agent_id = str(
        request_context.get("agent_id")
        or metadata.get("agent_id")
        or conversation.get("agent_id")
        or conversation_metadata.get("agent_id")
        or ""
    ).strip()
    if resolved_agent_id:
        request_context["agent_id"] = resolved_agent_id
    _propagate_conversation_workspace(request_context, metadata, conversation_metadata)
    if authority_resume_followup:
        request_context["authority_resume_followup"] = True
    request_context.update(_approval_followup_tool_context(metadata))
    raw_tool_policy = params.get("tool_policy")
    if isinstance(raw_tool_policy, dict):
        sanitized_tool_policy, ignored_tool_policy_keys = _sanitize_untrusted_chat_tool_policy(
            raw_tool_policy,
            trusted_local_ui=bool(
                request_context.get("_defaultspack_local_ui_authenticated")
            ),
        )
        if ignored_tool_policy_keys:
            params["tool_policy"] = sanitized_tool_policy
            request_context["ignored_client_tool_policy_keys"] = sorted(
                set(ignored_tool_policy_keys)
            )
            if isinstance(metadata, dict):
                metadata["ignored_client_tool_policy_keys"] = sorted(
                    set(ignored_tool_policy_keys)
                )
                store.update_message(conversation_id, user_message["id"], {"metadata": metadata})
    frontend_precision = _frontend_precision_metadata(
        user_text=user_text,
        metadata=metadata,
        input_data=prepared_input,
        conversation_metadata=conversation_metadata,
        request_context=request_context,
    )
    if frontend_precision:
        metadata["frontend_precision"] = frontend_precision
        user_message["metadata"] = metadata
        request_context["frontend_precision"] = frontend_precision
    template_tool_policy_resolution = _resolve_template_tool_policy(
        params.get("tool_policy") if isinstance(params.get("tool_policy"), dict) else {},
        metadata=metadata,
    )
    if template_tool_policy_resolution.id_requested:
        params["tool_policy"] = template_tool_policy_resolution.policy
        request_context["template_tool_policy_resolution"] = (
            template_tool_policy_resolution.to_context()
        )
        if isinstance(metadata, dict):
            metadata["template_tool_policy_resolution"] = (
                template_tool_policy_resolution.to_context()
            )
    tool_policy = params.get("tool_policy")
    if isinstance(tool_policy, dict):
        request_context["profile_policy"] = {
            **(
                request_context.get("profile_policy")
                if isinstance(request_context.get("profile_policy"), dict)
                else {}
            ),
            **tool_policy,
        }
        if (
            str(tool_policy.get("action_approval_mode") or "").strip().lower()
            == "full"
            and tool_policy.get("full_access") is True
        ):
            request_context["full_access"] = True
        policy_profile_id = str(tool_policy.get("profile_id") or "").strip()
        if policy_profile_id and not request_context.get("profile_id"):
            request_context["profile_id"] = policy_profile_id
        tool_choice = tool_policy.get("tool_choice")
        if "tool_choice" not in params and (
            isinstance(tool_choice, dict)
            or str(tool_choice or "").strip().lower() in {"auto", "none", "required"}
        ):
            params["tool_choice"] = tool_choice
        parallel_tool_calls = tool_policy.get("parallel_tool_calls")
        if "parallel_tool_calls" not in params and isinstance(parallel_tool_calls, bool):
            params["parallel_tool_calls"] = parallel_tool_calls
    if tool_selection.must_use:
        params["tool_choice"] = "required"
    prepared_input = {**prepared_input, "params": params}
    tool_resolution_input = {
        **prepared_input,
        "params": {
            **params,
            "tool_selection": {
                "mode": tool_selection.mode,
                "include": list(tool_selection.include),
                "exclude": list(tool_selection.exclude),
                "scope": tool_selection.scope,
                "must_use": tool_selection.must_use,
            },
        },
    }
    if frontend_precision:
        tool_resolution_input = _with_frontend_precision_tool_selection(tool_resolution_input)
        request_context["tool_selection"] = _tool_selection_metadata(
            _normalize_tool_selection(tool_resolution_input)
        )

    request_context, effective_system_prompt = _apply_effective_ai_input_to_request_context(
        request_context,
        active_startup_profile,
        conversation_id=conversation_id,
        request_id=request_id,
        user_text=user_text,
    )
    if effective_system_prompt:
        system_prompt = effective_system_prompt
        _replace_system_prompt_message(standard_messages, effective_system_prompt)
    temporal_context = current_datetime_context(request_context)
    request_context["current_datetime_context"] = temporal_context
    request_context.setdefault("current_datetime", temporal_context["iso"])
    request_context.setdefault("current_date", temporal_context["date"])
    request_context.setdefault("current_time_zone", temporal_context["timezone"])
    add_temporal_context_message(
        standard_messages,
        request_context,
        temporal_context=temporal_context,
    )
    _append_system_context_message(standard_messages, chat_reference_prompt)

    _apply_authority_context(
        request_context,
        metadata,
        conversation_id=conversation_id,
        request_id=request_id,
        active_profile=active_startup_profile,
    )

    raw_tools, provider_tools, tool_context = _available_tools(
        request_context, tool_resolution_input, user_text=user_text
    )
    if frontend_precision:
        tool_context["frontend_precision"] = frontend_precision
        request_context["frontend_precision"] = frontend_precision
    tool_hint_prompt = _tool_selection_hints_prompt(tool_context)
    if tool_hint_prompt:
        _append_system_context_message(standard_messages, tool_hint_prompt)
    computer_use_hint_prompt = _computer_use_runtime_prompt(request_context, raw_tools)
    if computer_use_hint_prompt:
        _append_system_context_message(standard_messages, computer_use_hint_prompt)
    _ensure_must_use_has_eligible_tools(tool_selection, raw_tools)
    modalities = detect_modalities(content, metadata)
    route_preferred_model = model
    route_preferred_capabilities = get_model_capabilities(route_preferred_model) or {}
    routing_decision = route_model_request(
        ModelRoutingRequest(
            conversation_id=conversation_id,
            user_text=user_text,
            has_images=bool(modalities.get("has_images")),
            has_audio=bool(modalities.get("has_audio")),
            has_files=bool(modalities.get("has_files")),
            requested_tools=[
                tool_name_from_definition(tool)
                for tool in raw_tools
                if tool_name_from_definition(tool)
            ],
            requires_tool_calling=bool(provider_tools),
            requested_thinking_level=params.get("thinking_level"),
            preferred_model=model,
            preferred_group=preferred_group_override
            or str(model_settings.get("preferred_model_group") or "default"),
            auto_route_within_group=bool(model_settings.get("auto_route_within_group", True)),
            task_hints={
                **(
                    route_override.get("task_hints")
                    if isinstance(route_override, dict)
                    and isinstance(route_override.get("task_hints"), dict)
                    else {}
                ),
                "modalities": modalities,
            },
            settings=model_settings,
        )
    )
    force_preferred_model = bool(requested_model) or (
        _is_provider_qualified_model(route_preferred_model)
        and not route_preferred_capabilities
    )
    if force_preferred_model:
        model = route_preferred_model
        if routing_decision.selected_model != model:
            routing_decision.selected_model = model
            if "explicit_model_override" not in routing_decision.reason_codes:
                routing_decision.reason_codes.append("explicit_model_override")
            routing_decision.explanation = f"{model} selected because it was explicitly requested."
    else:
        model = routing_decision.selected_model
    selected_capabilities = get_model_capabilities(model) or {}
    provider_model_metadata = None
    if selected_capabilities:
        provider_model_metadata = {
            "id": model,
            "provider_id": model.split("/", 1)[0] if "/" in model else "",
            "capabilities": selected_capabilities,
            "metadata": {"capabilities": selected_capabilities},
            "supports_thinking": bool(selected_capabilities.get("supports_thinking")),
        }
    provider_capabilities = _apply_provider_surface_defaults(
        get_model_provider_capabilities(model, provider_model_metadata),
        selected_capabilities,
    )
    if params.get("thinking_level") not in (None, "", "none") and not selected_capabilities.get(
        "supports_thinking"
    ):
        params["thinking_level"] = "none"
    policy = policy_from_context(request_context)
    runtime_model_capabilities = _runtime_model_capabilities(
        selected_capabilities,
        provider_capabilities,
    )
    runtime_snapshot = build_runtime_capability_snapshot(
        user_text=user_text,
        modalities=modalities,
        model_capabilities=runtime_model_capabilities,
        context=request_context,
        policy=policy,
    )
    eligibility_result = filter_tool_definitions_by_eligibility(
        raw_tools,
        runtime_snapshot,
        policy=policy,
        connected_tool_names=connected_tool_names(
            provider_tools,
            tool_context.get("runtime_profile") if isinstance(tool_context, dict) else None,
            agent_id=tool_context.get("agent_id") if isinstance(tool_context, dict) else None,
        ),
    )
    raw_tools = list(eligibility_result.get("allowed_tools") or [])
    _ensure_must_use_has_eligible_tools(tool_selection, raw_tools)
    capability_plan = CapabilityOrchestrator(
        call_handler=request_context.get("call_handler")
    ).compile_selected(
        user_text=user_text,
        selected_tools=raw_tools,
        eligible_tools=raw_tools,
        settings=(
            tool_context.get("capability_settings_snapshot")
            if isinstance(tool_context.get("capability_settings_snapshot"), dict)
            else _read_frontend_settings()
        ),
        runtime_profile=(
            tool_context.get("runtime_profile")
            if isinstance(tool_context.get("runtime_profile"), dict)
            else None
        ),
        context={
            **request_context,
            **tool_context,
            "policy_generation": CapabilityRepository().policy_generation(),
        },
    )
    compiled_model_input = capability_plan.pop("_compiled_model_input")
    compiled_tool_ids = set(compiled_model_input["tool_ids"])
    raw_tools = [
        tool
        for tool in compiled_model_input["tools"]
        if tool_name_from_definition(tool) in compiled_tool_ids
    ]
    if tool_selection.must_use:
        attached_tool_ids = [
            tool_name_from_definition(tool)
            for tool in raw_tools
            if tool_name_from_definition(tool)
        ]
        explicitly_requested = set(
            _coerce_tool_id_list(list(tool_selection.include))
        )
        required_tool_ids = [
            tool_id
            for tool_id in attached_tool_ids
            if not explicitly_requested or tool_id in explicitly_requested
        ]
        if not required_tool_ids:
            raise ValueError(
                "params.tool_selection.must_use did not resolve to an "
                "attached Tool"
            )
        request_context["required_tool_ids"] = list(required_tool_ids)
        tool_context["required_tool_ids"] = list(required_tool_ids)
        if len(required_tool_ids) == 1 and not isinstance(
            params.get("tool_choice"), dict
        ):
            params["tool_choice"] = {
                "type": "function",
                "function": {"name": required_tool_ids[0]},
            }
    provider_tools = adapt_tool_definitions(raw_tools)
    skill_instructions = str(
        compiled_model_input.get("skill_instructions") or ""
    ).strip()
    matched_skills = list(compiled_model_input.get("matched_skills") or [])
    request_context["capability_plan"] = capability_plan
    tool_context["capability_plan"] = capability_plan
    provider_tools, provider_schema_restrictions = _shape_provider_tools_for_request(
        provider_tools,
        request_context,
        user_text=user_text,
    )
    if provider_schema_restrictions:
        request_context["provider_tool_schema_restrictions"] = provider_schema_restrictions
        tool_context["provider_tool_schema_restrictions"] = provider_schema_restrictions
    filter_entries = list(eligibility_result.get("entries") or [])
    if isinstance(tool_context.get("unselected_requested_tools"), list):
        filter_entries.extend(
            entry
            for entry in tool_context["unselected_requested_tools"]
            if isinstance(entry, dict)
        )
    compact_filter_entries = compact_tool_filter_entries(filter_entries)
    tool_context["tool_filter_result"] = filter_entries
    tool_context["runtime_capability_snapshot"] = runtime_snapshot.as_dict()
    request_context["tool_filter_result"] = filter_entries
    request_context["runtime_capability_snapshot"] = runtime_snapshot.as_dict()
    if isinstance(metadata, dict):
        metadata["tool_filter_result"] = compact_filter_entries
        metadata["runtime_capability_snapshot"] = runtime_snapshot.as_dict()
        store.update_message(conversation_id, user_message["id"], {"metadata": metadata})
    if (
        provider_tools
        and not runtime_model_capabilities.get("supports_tool_calling")
        and not request_context.get("user_requested_computer_use")
    ):
        unavailable_tools = [
            tool_name_from_definition(tool) for tool in raw_tools if tool_name_from_definition(tool)
        ]
        marked_entries = _mark_tool_calling_unavailable(
            request_context.get("tool_filter_result"),
            unavailable_tools,
            runtime_snapshot.as_dict(),
        )
        tool_context["tool_filter_result"] = marked_entries
        request_context["tool_filter_result"] = marked_entries
        if isinstance(metadata, dict):
            metadata["tool_filter_result"] = compact_tool_filter_entries(marked_entries)
            store.update_message(conversation_id, user_message["id"], {"metadata": metadata})
        tool_context["tool_suggestion_context"] = {
            "message": "Selected model does not support provider tool calling; tools were not attached.",
            "suggested_tools": unavailable_tools,
        }
        tool_context["tool_calling_unverified"] = True
        tool_context["tool_calling_unavailable_reason"] = (
            "selected_model_does_not_support_tool_calling"
        )
        tool_context["requested_tools_without_provider_attachment"] = unavailable_tools
        request_context["tool_calling_unverified"] = True
        request_context["tool_calling_unavailable_reason"] = (
            "selected_model_does_not_support_tool_calling"
        )
        request_context["requested_tools_without_provider_attachment"] = unavailable_tools
        provider_tools = []
    tool_discovery_prompt = _tool_discovery_fallback_prompt(raw_tools) if provider_tools else ""
    if tool_discovery_prompt:
        _append_system_context_message(standard_messages, tool_discovery_prompt)
    if routing_decision.bridge_required:
        bridge_result = describe_images(
            messages=standard_messages,
            attachments=(metadata or {}).get("attachments") if isinstance(metadata, dict) else [],
            conversation_context=user_text,
            model=routing_decision.bridge_plan.get("model", ""),
            call_handler=request_context.get("call_handler"),
        )
        standard_messages = apply_vision_bridge_to_messages(standard_messages, bridge_result)
        existing_metadata = (
            conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
        )
        store.update_conversation(
            conversation_id,
            {
                "metadata": {
                    **existing_metadata,
                    "conversation_image_context": conversation_image_context(bridge_result),
                }
            },
        )
        if isinstance(metadata, dict):
            metadata["vision_bridge_result"] = bridge_result
            store.update_message(conversation_id, user_message["id"], {"metadata": metadata})
    request_context["model"] = model
    request_context["chat_params"] = params
    request_context["model_routing"] = routing_decision.to_dict()
    connected_names = connected_tool_names(
        provider_tools,
        tool_context.get("runtime_profile") if isinstance(tool_context, dict) else None,
        agent_id=tool_context.get("agent_id") if isinstance(tool_context, dict) else None,
    )
    provider_compat_tool_ids = {
        str(item).strip()
        for item in (
            tool_context.get("caller_provider_tool_ids")
            if isinstance(tool_context, dict)
            and isinstance(tool_context.get("caller_provider_tool_ids"), list)
            else []
        )
        if str(item or "").strip()
    }
    if provider_compat_tool_ids:
        connected_names = {name for name in connected_names if name not in provider_compat_tool_ids}
    tool_context["chat_references"] = chat_references
    tool_context["history_json_path"] = chat_references["history_json_path"]
    if skill_instructions:
        _append_system_context_message(standard_messages, skill_instructions)
        request_context["matched_skill_instructions"] = matched_skills
        tool_context["matched_skill_instructions"] = matched_skills
        try:
            from ..ai_input.ai_input_token_estimator import estimate_tokens
            from ..ai_input.ai_input_trace_store import AiInputTraceStore
            from domain.prompt.usage import append_runtime_prompt_segment, compact_prompt_usage_for_metadata

            skill_segment = {
                "id": "skill:runtime.matched_instructions",
                "edge_id": "",
                "prompt_id": "runtime.matched_instructions",
                "label": "Matched skill instructions",
                "kind": "skill",
                "port": "system",
                "status": "active",
                "source": "RuntimeSkillTriggerService",
                "source_type": "skill",
                "tokens": int(estimate_tokens(skill_instructions)),
                "reason": "Matched skills were triggered by the current message or selected tools.",
                "allow_disable": False,
                "editable": False,
                "readonly_reason": "Runtime skill instructions are controlled by skill definitions.",
                "preview": "[instruction body redacted from trace]",
                "text_hash": stable_revision(skill_instructions),
                "metadata": {"matched_skills": matched_skills},
            }
            request_context["prompt_usage"] = compact_prompt_usage_for_metadata(
                append_runtime_prompt_segment(request_context.get("prompt_usage"), skill_segment)
            )
            trace_info = request_context.get("ai_input_trace") if isinstance(request_context.get("ai_input_trace"), dict) else {}
            trace_id = str(trace_info.get("trace_id") or "").strip()
            trace_profile_id = str(trace_info.get("profile_id") or request_context.get("profile_id") or "").strip()
            if trace_id and trace_profile_id:
                trace_store = AiInputTraceStore()
                trace = trace_store.get_trace(trace_profile_id, trace_id)
                if isinstance(trace, dict):
                    runtime_segments = trace.get("runtime_prompt_segments") if isinstance(trace.get("runtime_prompt_segments"), list) else []
                    runtime_segments.append(skill_segment)
                    trace["runtime_prompt_segments"] = runtime_segments
                    trace_store.save_trace(trace_profile_id, trace)
        except Exception:
            pass
    provider_input_ir = legacy_standard_messages_to_ir(standard_messages, conversation_id)
    if authority_resume_followup and provider_tools:
        params = dict(params)
        params.setdefault("tool_choice", "required")
    planned_request = plan_model_request(
        provider_input_ir,
        model,
        provider_capabilities,
        provider_tools,
        params,
        request_context,
    )
    provider_planning = planned_request.to_dict()
    provider_tools = planned_request.provider_tools
    params = planned_request.params
    provider_chat_ir = planned_request.ir
    standard_messages = ir_to_legacy_standard_messages(provider_chat_ir)
    if provider_tools and not authority_resume_followup:
        provider_tools = with_assistant_progress_tool(list(provider_tools))
        _append_system_context_message(standard_messages, assistant_progress_system_instruction())
        provider_chat_ir = legacy_standard_messages_to_ir(standard_messages, conversation_id)
        tool_context["assistant_progress_enabled"] = True
    request_context["chat_params"] = params
    request_context["provider_capabilities"] = provider_capabilities
    request_context["provider_planning"] = provider_planning
    tool_context["provider_capabilities"] = provider_capabilities
    tool_context["provider_planning"] = provider_planning

    return PreparedChatRun(
        conversation_id=conversation_id,
        conversation=conversation,
        input_data=prepared_input,
        request_id=request_id,
        content=content,
        metadata=metadata,
        user_message=user_message,
        model=model,
        params=params,
        request_context=request_context,
        tool_context=tool_context,
        standard_messages=standard_messages,
        user_text=user_text,
        system_prompt=system_prompt,
        enrich_info=enrich_info,
        raw_tools=raw_tools,
        provider_tools=provider_tools,
        tools_called=[
            tool_name_from_definition(tool) for tool in raw_tools if tool_name_from_definition(tool)
        ],
        connected_tool_names=connected_names,
        call_handler=request_context.get("call_handler"),
        model_routing=routing_decision.to_dict(),
        chat_ir=chat_ir,
        provider_chat_ir=provider_chat_ir,
        ir_schema_version=IR_SCHEMA_VERSION,
        provider_planning=provider_planning,
        provider_capabilities=provider_capabilities,
        chat_references=chat_references,
        matched_skills=matched_skills,
    )


def _apply_effective_ai_input_to_request_context(
    request_context: dict[str, Any],
    active_profile: dict[str, Any] | None,
    *,
    conversation_id: str,
    request_id: str,
    user_text: str,
) -> tuple[dict[str, Any], str]:
    if (
        not isinstance(active_profile, dict)
        or not str(active_profile.get("profile_id") or "").strip()
    ):
        return request_context, ""
    try:
        from ..ai_input.ai_input_graph_builder import build_runtime_ai_input_trace
        from ..ai_input.ai_input_trace_store import AiInputTraceStore
        from domain.prompt.usage import compact_prompt_usage_for_metadata, prompt_usage_from_trace
    except Exception:
        return request_context, ""

    updated = dict(request_context or {})
    trace = build_runtime_ai_input_trace(
        active_profile,
        conversation_id=conversation_id,
        run_id=request_id,
        user_message=user_text,
        request_context=updated,
        include_text=True,
    )
    allowed_tool_ids = [
        str(item).strip() for item in trace.get("allowed_tool_ids", []) if str(item or "").strip()
    ]
    if allowed_tool_ids and _active_profile_enforces_tool_allowlist(active_profile):
        updated["effective_tool_allowlist"] = allowed_tool_ids
        profile_policy = dict(
            updated.get("profile_policy") if isinstance(updated.get("profile_policy"), dict) else {}
        )
        profile_policy["tool_allowlist"] = allowed_tool_ids
        updated["profile_policy"] = profile_policy
    updated["ai_input_trace"] = {
        "trace_id": trace.get("trace_id"),
        "profile_id": trace.get("profile_id"),
        "token_estimate": trace.get("token_estimate"),
        "allowed_tool_ids": allowed_tool_ids,
        "gate_decisions": trace.get("gate_decisions", []),
    }
    try:
        updated["prompt_usage"] = compact_prompt_usage_for_metadata(prompt_usage_from_trace(trace, include_text=False))
    except Exception:
        pass
    try:
        AiInputTraceStore().save_trace(str(active_profile.get("profile_id") or ""), trace)
    except Exception:
        pass

    effective = (
        trace.get("effective_input") if isinstance(trace.get("effective_input"), dict) else {}
    )
    segments = (
        effective.get("system_segments")
        if isinstance(effective.get("system_segments"), list)
        else []
    )
    context_segments = (
        effective.get("context_segments")
        if isinstance(effective.get("context_segments"), list)
        else []
    )
    system_text = "\n\n".join(
        str(segment.get("text") or segment.get("preview") or "").strip()
        for segment in [*segments, *context_segments]
        if isinstance(segment, dict)
        and str(segment.get("text") or segment.get("preview") or "").strip()
    )
    if not _active_profile_provides_system_prompt(active_profile):
        system_text = ""
    return updated, system_text


def _copy_enriched_context_into_request_context(
    request_context: dict[str, Any],
    enrich_info: dict[str, Any],
) -> None:
    for key in ("knowledge_text", "memory_text"):
        value = str(enrich_info.get(key) or "").strip()
        if value:
            request_context[key] = value
    for key in ("knowledge_results", "memory_results"):
        value = enrich_info.get(key)
        if isinstance(value, list):
            request_context[key] = list(value)


def _conversation_user_text_for_context(messages: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for message in list(messages or [])[-12:]:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_content_text_for_context(message.get("content"))
        if text:
            texts.append(text)
    return "\n".join(texts[-6:])


def _message_content_text_for_context(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part.strip() for part in parts if part and part.strip())
    return ""


def _replace_system_prompt_message(messages: list[dict[str, Any]], system_prompt: str) -> None:
    if not system_prompt:
        return
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = system_prompt
        return
    messages.insert(0, {"role": "system", "content": system_prompt})


def _append_system_context_message(messages: list[dict[str, Any]], content: str) -> None:
    text = str(content or "").strip()
    if not text:
        return
    if messages and messages[0].get("role") == "system":
        existing = str(messages[0].get("content") or "").strip()
        messages[0]["content"] = "{}\n\n{}".format(existing, text) if existing else text
        return
    messages.insert(0, {"role": "system", "content": text})


def _tool_selection_hints_prompt(context: dict[str, Any]) -> str:
    metadata = context.get("tool_selection") if isinstance(context, dict) else {}
    if not isinstance(metadata, dict) or metadata.get("strategy") != "all_with_hints":
        return ""
    metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
    recommendations = metrics.get("recommended_tools")
    if not isinstance(recommendations, list):
        recommendations = metadata.get("recommendations") if isinstance(metadata.get("recommendations"), list) else []
    lines = []
    for item in recommendations[:16]:
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get("tool_id") or item.get("id") or "").strip()
        if not tool_id:
            continue
        reason = str(item.get("reason") or "").strip()
        confidence = item.get("confidence")
        suffix = " reason={}".format(reason) if reason else ""
        if confidence not in (None, ""):
            suffix += " confidence={}".format(confidence)
        lines.append("- {}{}".format(tool_id, suffix))
    if not lines:
        order = metrics.get("recommendation_order") if isinstance(metrics.get("recommendation_order"), list) else []
        lines = ["- {}".format(str(item)) for item in order[:16] if str(item or "").strip()]
    if not lines:
        return ""
    return (
        "Tool selection hints for all_with_hints strategy.\n"
        "All eligible tool schemas are attached, but prefer this order when it fits the user request:\n"
        + "\n".join(lines)
    )


def _explicit_browser_navigation_requested(context: dict[str, Any], user_text: str = "") -> bool:
    text_parts = [user_text]
    if isinstance(context, dict):
        text_parts.extend(
            [
                str(context.get("user_text") or ""),
                str(context.get("conversation_user_text") or ""),
            ]
        )
    text = "\n".join(part for part in text_parts if isinstance(part, str) and part)
    return bool(_EXPLICIT_BROWSER_NAVIGATION_RE.search(text))


def _shape_provider_tools_for_request(
    provider_tools: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    user_text: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if (
        not provider_tools
        or not isinstance(context, dict)
        or not context.get("user_requested_computer_use")
        or not _explicit_browser_navigation_requested(context, user_text)
    ):
        return provider_tools, []

    shaped_tools: list[dict[str, Any]] = []
    restrictions: list[dict[str, Any]] = []
    for tool in provider_tools:
        shaped_tool, restriction = _restrict_computer_navigation_provider_tool(tool)
        shaped_tools.append(shaped_tool)
        if restriction:
            restrictions.append(restriction)
    return shaped_tools, restrictions


def _restrict_computer_navigation_provider_tool(
    tool: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    tool_name = tool_name_from_definition(tool)
    if tool_name not in _COMPUTER_USE_TOOL_IDS:
        return tool, None
    if not isinstance(tool, dict):
        return tool, None
    function_def = tool.get("function")
    if not isinstance(function_def, dict):
        return tool, None
    parameters = function_def.get("parameters")
    if not isinstance(parameters, dict):
        return tool, None
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return tool, None
    action_schema = properties.get("action")
    if not isinstance(action_schema, dict) or not isinstance(action_schema.get("enum"), list):
        return tool, None

    original_actions = [str(action) for action in action_schema.get("enum") or []]
    allowed_actions = [
        action
        for action in original_actions
        if action not in _COMPUTER_NAVIGATION_PROVIDER_DISABLED_ACTIONS
    ]
    if allowed_actions == original_actions:
        return tool, None

    shaped_tool = copy.deepcopy(tool)
    shaped_function = shaped_tool.setdefault("function", {})
    shaped_parameters = shaped_function.setdefault("parameters", {})
    shaped_properties = shaped_parameters.setdefault("properties", {})
    shaped_action = shaped_properties.setdefault("action", {})
    shaped_action["enum"] = allowed_actions
    existing_description = str(shaped_action.get("description") or "").strip()
    shaped_action["description"] = (
        "For this explicit browser-navigation request, choose browser.open_url as the "
        "first external action when a URL is named. Diagnostic discovery actions "
        "such as computer.context, computer.apps, and computer.doctor are hidden for "
        "this request unless a later tool result asks for diagnostics."
        + (f" {existing_description}" if existing_description else "")
    )
    return shaped_tool, {
        "tool_name": tool_name,
        "reason": "explicit_browser_navigation",
        "removed_actions": [
            action
            for action in original_actions
            if action in _COMPUTER_NAVIGATION_PROVIDER_DISABLED_ACTIONS
        ],
        "allowed_actions": allowed_actions,
    }


def _computer_use_runtime_prompt(context: dict[str, Any], tools: list[dict[str, Any]]) -> str:
    if not isinstance(context, dict) or not context.get("user_requested_computer_use"):
        return ""
    tool_names = {
        tool_name_from_definition(tool)
        for tool in tools or []
        if tool_name_from_definition(tool)
    }
    if not (tool_names & _COMPUTER_USE_TOOL_IDS):
        return ""
    lines = ["Computer-use execution contract:"]
    target_app = str(context.get("computer_use_target_app") or "").strip()
    target_title = str(context.get("computer_use_target_title") or "").strip()
    if target_app:
        target = target_app + (f" / {target_title}" if target_title else "")
        lines.append(f"- Target the visible app/window: {target}.")
    foreground_preferred = _computer_use_foreground_preferred_for_prompt(context)
    user_text = str(context.get("user_text") or context.get("conversation_user_text") or "")
    if _explicit_browser_navigation_requested(context, user_text):
        lines.append(
            "- When the user gives an explicit browser URL or ordered browser navigation, use browser.open_url "
            "with the requested URL as the first external action. Do not spend the first actions on "
            "computer.context, computer.apps, or computer.doctor unless browser.open_url fails or the target app is unknown."
        )
    lines.append(
        "- Do not treat browser.open_url or app launch as task completion when the user asked to operate the app; "
        "continue with visible-screen actions until the requested UI result is verified."
    )
    if foreground_preferred:
        lines.append(
            "- This is a visible foreground computer-use run. For computer.type, computer.key, and "
            "computer.scroll, omit background=true and use fallback=foreground when you need on-screen input."
        )
    else:
        lines.append(
            "- Supported safe input actions (computer.type, computer.key, computer.scroll) run in background by default "
            "when a trusted non-physical background driver is available. If the tool reports "
            "foreground_confirmation_required, ask the user `foregroundで作業しますか？` and only retry with "
            "fallback=foreground after explicit user confirmation and approval."
        )
    lines.append(
        "- Use computer.show_app/select_window, then computer.screenshot or computer.observe before coordinate-based actions; "
        "continue with computer.type, computer.key, computer.click, or computer.scroll as needed."
    )
    lines.append(
        "- Enter any text or URL explicitly requested by the user exactly and completely, character-for-character. "
        "Before typing into an editable field or address bar, focus it and clear or replace existing content, then type "
        "the full literal. Never abbreviate, truncate, paraphrase, or rely on autocomplete/search suggestions as proof "
        "that the requested input was entered."
    )
    lines.append(
        "- For multi-step navigation, use computer.screenshot or computer.observe after every consequential action, "
        "including typing, Enter/submission, navigation, and clicking a result or media item. Confirm the expected "
        "text, page, or state before continuing; if it failed, recover and retry from the last visually verified state."
    )
    lines.append(
        "- Do not declare completion until the user's requested final state is visually verified. For media playback, "
        "verify actual playback state rather than merely opening the page or media item."
    )
    if context.get("computer_use_mouse_keyboard_requested") or context.get("computer_use_physical_clicks"):
        lines.append(
            "- The user explicitly asked for mouse/keyboard app operation. Prefer foreground UI navigation "
            "(for browsers: command+l, type the URL/search text, return) over a direct browser.open_url-only answer."
        )
        lines.append(
            "- For real visible pointer clicks, include physical=true; approval still gates execution before the host performs it."
        )
    return "\n".join(lines)


def _computer_use_foreground_preferred_for_prompt(context: dict[str, Any]) -> bool:
    if not isinstance(context, dict):
        return False
    value = context.get("computer_use_foreground_preferred")
    if isinstance(value, bool):
        if value:
            return True
    elif str(value or "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    target_alias = re.sub(
        r"[^a-z0-9]+",
        "",
        str(context.get("computer_use_target_app") or "").strip().casefold(),
    )
    if target_alias in {"atlas", "chatgptatlas"}:
        return True
    env_value = str(os.environ.get("RUMI_COMPUTER_USE_DEBUG_FOREGROUND") or "").strip().lower()
    return env_value in {"1", "true", "yes", "on"}


def _tool_discovery_fallback_prompt(tools: list[dict[str, Any]]) -> str:
    tool_names = {
        tool_name_from_definition(tool)
        for tool in tools
        if tool_name_from_definition(tool)
    }
    if not (_TOOL_DISCOVERY_FALLBACK_IDS & tool_names):
        return ""
    lines = ["Tool discovery fallback:"]
    if "tool_names" in tool_names:
        lines.append("- To inspect the currently attached/registered tool names, call tool_names with {}.")
    if "tool_search" in tool_names:
        lines.append(
            '- To find tools related to a word or task, call tool_search with {"query":"coding","phase":"overview"}; use phase="schema" or include_schema=true only after choosing a concrete tool.'
        )
    lines.append(
        "Use these discovery tools when semantic/vector tool selection seems incomplete before deciding that no suitable tool exists."
    )
    return "\n".join(lines)


def _active_profile_selected(active_profile: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(active_profile, dict):
        return {}
    metadata = (
        active_profile.get("metadata") if isinstance(active_profile.get("metadata"), dict) else {}
    )
    selected = metadata.get("selected") if isinstance(metadata.get("selected"), dict) else {}
    return selected if isinstance(selected, dict) else {}


def _active_profile_enforces_tool_allowlist(active_profile: dict[str, Any] | None) -> bool:
    if not isinstance(active_profile, dict):
        return False
    policy = active_profile.get("policy") if isinstance(active_profile.get("policy"), dict) else {}
    allowlist = policy.get("tool_allowlist")
    if isinstance(allowlist, list) and any(str(item or "").strip() for item in allowlist):
        return True
    selected = _active_profile_selected(active_profile)
    tools = selected.get("tools") if isinstance(selected.get("tools"), list) else []
    return any(str(item or "").strip() for item in tools)


def _active_profile_provides_system_prompt(active_profile: dict[str, Any] | None) -> bool:
    if not isinstance(active_profile, dict):
        return False
    for key in ("system_prompt_id", "default_prompt_id", "prompt_id"):
        if str(active_profile.get(key) or "").strip():
            return True
    selected = _active_profile_selected(active_profile)
    for key in ("prompts", "ai_input_nodes", "gates"):
        values = selected.get(key) if isinstance(selected.get(key), list) else []
        if any(str(item or "").strip() for item in values):
            return True
    metadata = (
        active_profile.get("metadata") if isinstance(active_profile.get("metadata"), dict) else {}
    )
    ai_input = metadata.get("ai_input") if isinstance(metadata.get("ai_input"), dict) else {}
    return bool(ai_input)


def _mark_tool_calling_unavailable(
    entries: Any, tool_names: list[str], actual: dict[str, Any]
) -> list[dict[str, Any]]:
    blocked_names = {str(name) for name in tool_names if str(name or "").strip()}
    output: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        tool_name = str(entry.get("tool_name") or "")
        if tool_name not in blocked_names:
            output.append(dict(entry))
            continue
        required = dict(entry.get("required") if isinstance(entry.get("required"), dict) else {})
        model_caps = [
            str(item).strip()
            for item in (
                required.get("model_capabilities")
                if isinstance(required.get("model_capabilities"), list)
                else []
            )
            if str(item or "").strip()
        ]
        if "model.tool_calling" not in model_caps:
            model_caps.append("model.tool_calling")
        required["model_capabilities"] = model_caps
        output.append(
            {
                **entry,
                "status": "blocked",
                "reason_code": "model_unsupported",
                "reason": "selected model does not support provider tool calling",
                "required": required,
                "actual": actual,
                "repair_suggestions": [
                    "Switch to a tool-calling model or disable tools for this turn."
                ],
            }
        )
    return output


def _current_turn_history_only(context: dict[str, Any] | None) -> bool:
    if not isinstance(context, dict):
        return False
    mode = (
        str(
            context.get("chat_history_mode")
            or context.get("external_chat_history_mode")
            or context.get("history_mode")
            or ""
        )
        .strip()
        .lower()
    )
    return mode in {"current_turn", "current_message", "stateless", "none"}


def _model_visible_message_chain(
    messages: list[dict[str, Any]],
    *,
    preserve_hidden_tool_progress: bool = False,
) -> list[dict[str, Any]]:
    """Return model-visible history, optionally retaining safe resume progress.

    Authority/approval followups intentionally hide their UI-only messages from
    the provider.  A hidden assistant message can nevertheless contain tool
    logs for successful actions that happened before the approval pause.  On a
    hidden authority resume, retain only each tool's compact ``model_context``
    as a linked assistant tool-call/tool-result pair.  The executable arguments,
    approval payload, and one-shot token are deliberately not reconstructed.
    """
    visible: list[dict[str, Any]] = []
    linked_tool_call_ids = _stored_tool_call_ids(
        [
            message
            for message in list(messages or [])
            if isinstance(message, dict) and not _message_hidden_from_model(message)
        ]
    )
    for message_index, message in enumerate(list(messages or [])):
        if not isinstance(message, dict):
            continue
        if not _message_hidden_from_model(message):
            visible.append(message)
            continue
        if not preserve_hidden_tool_progress or str(message.get("role") or "").lower() != "assistant":
            continue
        for progress_index, progress in enumerate(_hidden_tool_progress(message)):
            source_tool_call_id = str(progress.get("tool_call_id") or "").strip()
            if source_tool_call_id and source_tool_call_id in linked_tool_call_ids:
                continue
            # Reconstructed history is evidence, not an executable replay.  A
            # local ID avoids copying an opaque provider ID (or malformed
            # secret-bearing log value) into a new provider request.
            tool_call_id = "resume-progress-{}-{}".format(message_index, progress_index)
            while tool_call_id in linked_tool_call_ids:
                tool_call_id += "-next"
            linked_tool_call_ids.add(tool_call_id)
            tool_name = str(progress.get("tool_name") or "tool").strip() or "tool"
            model_context = progress.get("model_context")
            visible.extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_call",
                                "id": tool_call_id,
                                "name": tool_name,
                                "arguments": {},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_call_id": tool_call_id,
                                "name": tool_name,
                                "content": json.dumps(
                                    {"model_context": model_context},
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            }
                        ],
                    },
                ]
            )
    return visible


def _stored_tool_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tool_call_id = str(block.get("id") or block.get("tool_call_id") or "").strip()
            if tool_call_id and block.get("type") in {"tool_call", "tool_result"}:
                found.add(tool_call_id)
    return found


def _hidden_tool_progress(message: dict[str, Any]) -> list[dict[str, Any]]:
    progress: list[dict[str, Any]] = []
    for log in list(message.get("tool_logs") or []):
        if not isinstance(log, dict):
            continue
        model_context = _find_model_context(log.get("result"))
        safe_context = _safe_resume_model_context(model_context)
        outcome = safe_context.get("outcome") if isinstance(safe_context, dict) else None
        if not isinstance(outcome, dict) or outcome.get("succeeded") is not True:
            continue
        if outcome.get("approval_pending") is True:
            continue
        tool_name = str(log.get("tool_name") or "").strip()
        if tool_name not in _SAFE_RESUME_TOOL_NAMES:
            continue
        progress.append(
            {
                "tool_name": tool_name,
                "tool_call_id": str(log.get("tool_call_id") or ""),
                "model_context": safe_context,
            }
        )
    return progress


def _find_model_context(value: Any, *, depth: int = 0) -> dict[str, Any] | None:
    if depth > 6:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "[{":
            return None
        try:
            return _find_model_context(json.loads(text), depth=depth + 1)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if isinstance(value, dict):
        context = value.get("model_context")
        if isinstance(context, dict):
            return context
        for key in ("data", "result"):
            found = _find_model_context(value.get(key), depth=depth + 1)
            if found is not None:
                return found
    return None


def _safe_resume_model_context(value: Any) -> dict[str, Any]:
    """Allowlist progress facts; never carry URLs, args, payloads, or tokens."""
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    action = str(value.get("action") or "").strip()
    if action in _SAFE_RESUME_ACTIONS:
        safe["action"] = action
    for section, allowed in {
        "outcome": {"succeeded", "approval_pending", "effect_observed", "postcondition_verified"},
        "target": {
            "app",
            "pid",
            "window_id",
            "window_selected",
            "window_foreground",
            "binding_valid",
        },
        "navigation": {"open_succeeded", "same_url_reopen_needed"},
    }.items():
        raw = value.get(section)
        if not isinstance(raw, dict):
            continue
        compact: dict[str, Any] = {}
        for key in allowed:
            item = raw.get(key)
            if isinstance(item, bool) or isinstance(item, int):
                compact[key] = item
            elif key == "app" and isinstance(item, str) and _safe_resume_app_name(item):
                compact[key] = item.strip()[:160]
        if compact:
            safe[section] = compact
    transition = value.get("task_transition")
    if isinstance(transition, dict):
        compact_transition: dict[str, Any] = {}
        for key in ("completed_phase", "next_phase"):
            item = transition.get(key)
            if isinstance(item, str) and item.strip() in _SAFE_RESUME_PHASES:
                compact_transition[key] = item.strip()
        for key in ("recommended_actions", "avoid_actions"):
            items = transition.get(key)
            if isinstance(items, list):
                allowed_actions = (
                    _SAFE_RESUME_RECOMMENDED_ACTIONS
                    if key == "recommended_actions"
                    else _SAFE_RESUME_AVOID_ACTIONS
                )
                compact_transition[key] = [
                    str(item).strip()
                    for item in items[:8]
                    if isinstance(item, str) and item.strip() in allowed_actions
                ]
        if compact_transition:
            safe["task_transition"] = compact_transition
    return safe


_SAFE_RESUME_TOOL_NAMES = {"browser_computer", "browser_use", "computer_use"}
_SAFE_RESUME_ACTIONS = {
    "browser.open_url",
    "computer.click",
    "computer.drag",
    "computer.key",
    "computer.observe",
    "computer.screenshot",
    "computer.scroll",
    "computer.select_app",
    "computer.select_window",
    "computer.show_app",
    "computer.type",
}
_SAFE_RESUME_PHASES = {
    "action_completed",
    "action_failed",
    "approval_requested",
    "await_approval",
    "bind_foreground_target",
    "continue_unresolved_task",
    "enter_text",
    "interact_with_visible_target",
    "navigate_to_requested_page",
    "observe_action_effect",
    "observe_current_target",
    "observe_opened_page",
    "recover_from_failure",
    "submit_or_verify_typed_text",
}
_SAFE_RESUME_RECOMMENDED_ACTIONS = _SAFE_RESUME_ACTIONS | {
    "approve_request",
    "inspect_failure_and_recover",
}
_SAFE_RESUME_AVOID_ACTIONS = {
    "assume_action_succeeded",
    "browser.open_url:same_setup_page",
    "browser.open_url:same_url",
    "repeat_action_before_observing_effect",
    "repeat_completed_action_without_state_change",
    "repeat_observation_without_state_change",
    "repeat_same_text",
    "repeat_target_selection_when_binding_is_valid",
    "repeat_target_selection_without_binding_failure",
    "repeat_unapproved_action",
    "send_input_before_target_binding",
}


def _safe_resume_app_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 160 or any(ord(char) < 32 for char in text):
        return False
    lowered = text.casefold()
    return not any(marker in lowered for marker in ("://", "?", "=", "approval_token", "bearer "))


def _message_hidden_from_model(message: dict[str, Any]) -> bool:
    if not isinstance(message, dict):
        return False
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if metadata.get("hidden") is True:
        return True
    chat_display = metadata.get("chat_display")
    if not isinstance(chat_display, dict):
        chat_display = metadata.get("chatDisplay")
    if isinstance(chat_display, dict) and chat_display.get("hidden") is True:
        return True
    if isinstance(metadata.get("approval_followup"), dict) or isinstance(metadata.get("approvalFollowup"), dict):
        return True
    if _hidden_authority_followup_from_metadata(metadata) is not None:
        return True
    if any(
        isinstance(metadata.get(key), dict)
        for key in ("pending_approval", "pendingAuthorityApproval", "pending_authority_approval")
    ):
        return True
    finish_reason = str(message.get("finish_reason") or "").strip()
    return finish_reason in {"approval_required", "authority_approval_required"}


def _last_visible_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        text = extract_user_text(message.get("content"))
        if text.strip():
            return text
    return ""


def prefocus_computer_use_target_window(prepared: PreparedChatRun) -> Any:
    if not isinstance(prepared.request_context, dict) or not prepared.request_context.get(
        "user_requested_computer_use"
    ):
        return None
    if not _computer_use_prefocus_is_preapproved(prepared.tool_context):
        return None
    target_app = str(prepared.request_context.get("computer_use_target_app") or "").strip()
    target_title = str(prepared.request_context.get("computer_use_target_title") or "").strip()
    if not (target_app or target_title):
        return None
    tool_name = next(
        (
            candidate
            for candidate in ("browser_computer", "computer_use", "browser_use")
            if candidate in prepared.connected_tool_names
        ),
        "",
    )
    if not tool_name:
        return None

    payload: dict[str, Any] = {}
    if target_app:
        payload["app"] = target_app
    if target_title:
        payload["title"] = target_title
    arguments = {"action": "computer.select_window", "payload": payload}
    invoke_context = build_tool_execution_context(
        prepared.tool_context, tool_name, prepared.connected_tool_names
    )
    if prepared.call_handler is not None:
        result = prepared.call_handler(
            "defaults.tool.invoke",
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "context": invoke_context,
            },
        )
        if isinstance(result, dict) and result.get("status") == "ok":
            return result.get("data", {})
        return result

    from domain.tool.executor import ToolExecutor

    return ToolExecutor().execute(tool_name, arguments, invoke_context)


def _computer_use_prefocus_is_preapproved(context: dict[str, Any] | None) -> bool:
    """Only run automatic focus changes in an internally approved tool context.

    Prefocus wraps ``computer.select_window``. Running it without a preapproved
    context creates hidden pending approvals before the model's explicit
    computer-use action, which makes live debugging look stuck.
    """
    if not isinstance(context, dict):
        return False
    try:
        internal_context = importlib.import_module("domain.tool_policy.internal_context")
    except Exception:
        return False
    internal_tool_decision_allows = getattr(internal_context, "internal_tool_decision_allows", None)
    tool_server_approval_context_is_internal = getattr(
        internal_context, "tool_server_approval_context_is_internal", None
    )
    if not callable(internal_tool_decision_allows) or not callable(tool_server_approval_context_is_internal):
        return False
    return bool(tool_server_approval_context_is_internal(context) or internal_tool_decision_allows(context))


def _prepared_user_content(
    store: ChatStore,
    conversation_id: str,
    message: dict[str, Any],
) -> tuple[list[Any], dict[str, Any] | None, list[Any] | None]:
    content = message.get("content", [])
    attachments = message.get("attachments")
    has_attachments = isinstance(attachments, list) and len(attachments) > 0
    if (content is None or content == "" or content == []) and has_attachments:
        content = "添付ファイルを確認してください。"
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if isinstance(content, list):
        content = list(content)
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    runtime_content: list[Any] | None = None
    if isinstance(attachments, list):
        metadata = dict(metadata)
        persisted_attachments = store.persist_attachments(
            conversation_id,
            [attachment for attachment in attachments if not _attachment_is_ephemeral(attachment)],
        )
        metadata["attachments"] = _sanitize_attachment_metadata(attachments)
        if persisted_attachments:
            metadata["workspace_attachments"] = persisted_attachments
        if isinstance(content, list):
            content.extend(_attachment_text_blocks(attachments))
            content.extend(_attachment_image_blocks(attachments))
            content.extend(
                _attachment_audio_transcript_blocks(
                    attachments,
                    existing_text=_content_text_for_transcript_dedupe(content),
                )
            )
            audio_blocks = _attachment_audio_blocks(attachments)
            audio_placeholders = _attachment_audio_placeholders(attachments)
            if audio_blocks:
                runtime_content = list(content)
                runtime_content.extend(audio_blocks)
                content.extend(audio_placeholders)
            elif audio_placeholders:
                content.extend(audio_placeholders)
    return content if isinstance(content, list) else [{"type": "text", "text": str(content)}], metadata or None, runtime_content


def _chat_references(
    store: ChatStore, conversation_id: str, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    conversation_dir = store.conversation_dir(conversation_id)
    workspace_dir = store.conversation_workspace_dir(conversation_id)
    history_path = conversation_dir / "history.json"
    dropped_widgets = (
        metadata.get("dropped_widgets")
        if isinstance(metadata, dict) and isinstance(metadata.get("dropped_widgets"), list)
        else []
    )
    references = []
    for widget in dropped_widgets:
        if not isinstance(widget, dict):
            continue
        widget_meta = widget.get("metadata") if isinstance(widget.get("metadata"), dict) else {}
        ref_id = str(widget_meta.get("conversation_id") or widget.get("sourceItemId") or "").strip()
        if not ref_id or ref_id == conversation_id:
            continue
        ref_conv = store.get_conversation(ref_id) or {}
        ref_dir = store.conversation_dir(ref_id)
        references.append(
            {
                "conversation_id": ref_id,
                "title": str(
                    widget_meta.get("title")
                    or ref_conv.get("title")
                    or widget.get("label")
                    or ref_id
                ),
                "summary": _summarize_referenced_conversation(ref_conv),
                "history_json_path": str(ref_dir / "history.json"),
            }
        )
    return {
        "conversation_id": conversation_id,
        "conversation_dir": str(conversation_dir),
        "workspace_dir": str(workspace_dir),
        "history_json_path": str(history_path),
        "history_path": str(history_path),
        "references": references,
    }


def _format_chat_references_for_prompt(chat_references: dict[str, Any]) -> str:
    refs = chat_references.get("references") if isinstance(chat_references, dict) else []
    if not isinstance(refs, list) or not refs:
        return ""
    lines = ["--- Dropped Chat References ---"]
    for index, ref in enumerate(refs, 1):
        if not isinstance(ref, dict):
            continue
        lines.append(
            "[{}] chat_id={} title={} history_json={}\nsummary: {}".format(
                index,
                ref.get("conversation_id") or "",
                ref.get("title") or "",
                ref.get("history_json_path") or "",
                ref.get("summary") or "",
            )
        )
    return "\n".join(lines).strip()


def _summarize_referenced_conversation(conversation: dict[str, Any]) -> str:
    messages = (
        conversation.get("messages")
        if isinstance(conversation, dict) and isinstance(conversation.get("messages"), list)
        else []
    )
    snippets = []
    for message in messages[-8:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "message")
        text = _content_text(message.get("content"))
        if text:
            snippets.append(f"{role}: {text[:240]}")
    return "\n".join(snippets)[-1600:]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") in {"image", "image_url"}:
                parts.append("[image]")
            elif block.get("type"):
                parts.append(f"[{block.get('type')}]")
    return " ".join(part.strip() for part in parts if str(part).strip())


def _runtime_user_content_override(metadata: dict[str, Any] | None) -> str:
    if not isinstance(metadata, dict):
        return ""
    external = metadata.get("external") if isinstance(metadata.get("external"), dict) else {}
    value = external.get("runtime_content") or metadata.get("runtime_content")
    if not isinstance(value, str):
        return ""
    return value.strip()


_WORKSPACE_ID_KEYS = ("workspace_id", "workspaceId")
_WORKSPACE_ROOT_KEYS = ("workspace_root", "workspaceRoot", "rootPath")
_CLIENT_TOOL_POLICY_PRIVILEGED_TRUE_KEYS = {
    "allow_browser",
    "allow_client_supplied_approved",
    "allow_direct_tool",
    "allow_file_write",
    "allow_git",
    "allow_network",
    "allow_shell",
    "allow_terminal",
    "direct_tool_execution",
    "full_access",
    "yolo_mode",
}
_CLIENT_TOOL_POLICY_APPROVAL_BYPASS_KEYS = {
    "approval_token",
    "approval_bypass",
    "approval_granted",
    "approved",
    "bypass_approval",
    "grant_approval",
    "is_approved",
    "server_approved",
}
_CLIENT_TOOL_POLICY_APPROVAL_WEAKENING_FALSE_KEYS = {
    "delete_actions_require_approval",
    "destructive_actions_require_approval",
    "git_push_requires_approval",
    "terminal_actions_require_approval",
    "write_actions_require_approval",
}
_CLIENT_TOOL_POLICY_UNTRUSTED_STRUCTURAL_KEYS = {
    "tool_permission_policy",
}


def _client_policy_value_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}


def _client_policy_value_false(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if value is None:
        return False
    return str(value).strip().lower() in {"0", "false", "no", "off"}


def _sanitize_untrusted_chat_tool_policy(
    policy: dict[str, Any],
    *,
    trusted_local_ui: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    sanitized: dict[str, Any] = {}
    ignored: list[str] = []
    requested_mode = str(policy.get("action_approval_mode") or "").strip().lower()
    for key, value in policy.items():
        key_text = str(key or "").strip()
        lower_key = key_text.lower()
        if not key_text:
            continue
        if lower_key in _CLIENT_TOOL_POLICY_UNTRUSTED_STRUCTURAL_KEYS:
            ignored.append(key_text)
            continue
        if lower_key in _CLIENT_TOOL_POLICY_APPROVAL_BYPASS_KEYS and _client_policy_value_truthy(value):
            ignored.append(key_text)
            continue
        if (
            lower_key in _CLIENT_TOOL_POLICY_PRIVILEGED_TRUE_KEYS
            and _client_policy_value_truthy(value)
            and not (trusted_local_ui and requested_mode == "full")
        ):
            ignored.append(key_text)
            continue
        if (
            lower_key in _CLIENT_TOOL_POLICY_APPROVAL_WEAKENING_FALSE_KEYS
            and _client_policy_value_false(value)
            and not (trusted_local_ui and requested_mode == "full")
        ):
            ignored.append(key_text)
            continue
        if (
            lower_key == "action_approval_mode"
            and str(value or "").strip().lower() == "full"
            and not trusted_local_ui
        ):
            ignored.append(key_text)
            continue
        sanitized[key] = value
    if trusted_local_ui and requested_mode == "full":
        sanitized.update(
            {
                "action_approval_mode": "full",
                "allow_file_write": True,
                "allow_network": True,
                "allow_shell": True,
                "full_access": True,
                "write_actions_require_approval": False,
                "yolo_mode": True,
            }
        )
    elif requested_mode == "agent":
        # Delegated approval must never inherit the legacy blanket-yolo bypass.
        for key in (
            "allow_file_write",
            "allow_network",
            "allow_shell",
            "full_access",
            "yolo_mode",
        ):
            sanitized.pop(key, None)
    return sanitized, ignored


@lru_cache(maxsize=64)
def _profile_snapshot(profile_id: str) -> dict[str, Any]:
    candidate = str(profile_id or "").strip()
    if not candidate:
        return {}
    try:
        from core_runtime.resolved_profile_scope import persisted_resolved_profile

        plan = persisted_resolved_profile()
    except Exception:
        return {}
    if plan is None or str(plan.profile_id) != candidate:
        return {}
    return {
        "version": 4,
        "profile_id": str(plan.profile_id),
        "profile_revision": str(plan.profile_revision),
        "plan_hash": str(plan.plan_hash),
        "packs": list(plan.effective_pack_set),
        "providers": [
            {
                "contract_id": provider.contract_id,
                "provider_instance_id": provider.provider_instance_id,
                "source_pack_id": provider.source_pack_id,
            }
            for provider in plan.providers
        ],
    }


def _first_non_empty_str(*sources: dict[str, Any] | None, keys: tuple[str, ...]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value not in (None, "") and not isinstance(value, str):
                return str(value)
    return ""


def _hydrate_profile_policy_from_profile_id(
    request_context: dict[str, Any], profile_id: str
) -> None:
    if not isinstance(request_context, dict):
        return
    snapshot = _profile_snapshot(profile_id)
    policy = snapshot.get("policy") if isinstance(snapshot, dict) else None
    if not isinstance(policy, dict) or not policy:
        return
    existing = (
        request_context.get("profile_policy")
        if isinstance(request_context.get("profile_policy"), dict)
        else {}
    )
    request_context["profile_policy"] = {
        **policy,
        **existing,
    }


def _profile_policy_tool_ids(policy: dict[str, Any] | None) -> list[str]:
    if not isinstance(policy, dict):
        return []
    for key in ("tool_allowlist", "enabled_tools", "allowed_tools"):
        if key not in policy:
            continue
        value = policy.get(key)
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",")]
        if not isinstance(value, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            tool_id = str(item or "").strip()
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            result.append(tool_id)
        return result
    return []


def _profile_node_allowed_actions(profile: dict[str, Any] | None) -> list[str]:
    if not isinstance(profile, dict):
        return []
    node_settings = profile.get("node_settings") if isinstance(profile.get("node_settings"), dict) else {}
    tool_settings = (
        node_settings.get("defaultspack.tool")
        if isinstance(node_settings.get("defaultspack.tool"), dict)
        else {}
    )
    actions = tool_settings.get("allowed_actions")
    if not isinstance(actions, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in actions:
        tool_id = str(item or "").strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        result.append(tool_id)
    return result


def _profile_client_agent_id(profile: dict[str, Any] | None, fallback: Any = None) -> str:
    fallback_id = str(fallback or "").strip()
    if not isinstance(profile, dict):
        return fallback_id
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    node_settings = profile.get("node_settings") if isinstance(profile.get("node_settings"), dict) else {}
    agent_settings = (
        node_settings.get("defaultspack.agent")
        if isinstance(node_settings.get("defaultspack.agent"), dict)
        else {}
    )
    for value in (
        fallback_id,
        metadata.get("client_manager_agent_id"),
        agent_settings.get("client_facing_role"),
    ):
        candidate = str(value or "").strip()
        if candidate:
            return candidate
    return "agent"


def _runtime_profile_agent_tool_refs(
    runtime_profile: dict[str, Any] | None,
    agent_id: Any = None,
) -> list[str]:
    if not isinstance(runtime_profile, dict):
        return []
    defaultspack = runtime_profile.get("defaultspack")
    if not isinstance(defaultspack, dict):
        return []
    agents = defaultspack.get("agents")
    if not isinstance(agents, dict):
        return []
    selected = agents.get(str(agent_id or "").strip()) if str(agent_id or "").strip() else None
    if not isinstance(selected, dict) and len(agents) == 1:
        selected = next(iter(agents.values()))
    if not isinstance(selected, dict):
        return []
    tools = selected.get("tools")
    if not isinstance(tools, list):
        return []
    return [str(tool).strip() for tool in tools if str(tool or "").strip()]


def _merge_profile_tool_ids(*sources: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            tool_id = str(item or "").strip()
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            merged.append(tool_id)
    return merged


def _runtime_profile_with_policy_connected_tools(
    runtime_profile: dict[str, Any] | None,
    *,
    profile_id: Any = None,
    agent_id: Any = None,
) -> tuple[dict[str, Any] | None, str]:
    base_profile = dict(runtime_profile) if isinstance(runtime_profile, dict) else {}
    snapshot_profile_id = str(
        profile_id
        or base_profile.get("profile_id")
        or (
            base_profile.get("profile", {}).get("profile_id")
            if isinstance(base_profile.get("profile"), dict)
            else ""
        )
        or ""
    ).strip()
    base_profile_id = str(
        base_profile.get("profile_id")
        or (
            base_profile.get("profile", {}).get("profile_id")
            if isinstance(base_profile.get("profile"), dict)
            else ""
        )
        or ""
    ).strip()
    if (
        base_profile
        and snapshot_profile_id
        and base_profile_id
        and base_profile_id != snapshot_profile_id
    ):
        snapshot = _profile_snapshot(snapshot_profile_id)
        if isinstance(snapshot, dict) and snapshot:
            base_profile = snapshot
    if snapshot_profile_id:
        snapshot = _profile_snapshot(snapshot_profile_id)
        if isinstance(snapshot, dict) and snapshot:
            # Runtime graph projections intentionally omit some canonical
            # profile fields. Preserve graph/agent authority while hydrating
            # those non-authoritative fields from the saved snapshot.
            base_profile = {
                **snapshot,
                **base_profile,
                "metadata": {
                    **(
                        snapshot.get("metadata")
                        if isinstance(snapshot.get("metadata"), dict)
                        else {}
                    ),
                    **(
                        base_profile.get("metadata")
                        if isinstance(base_profile.get("metadata"), dict)
                        else {}
                    ),
                },
                "policy": {
                    **(
                        snapshot.get("policy")
                        if isinstance(snapshot.get("policy"), dict)
                        else {}
                    ),
                    **(
                        base_profile.get("policy")
                        if isinstance(base_profile.get("policy"), dict)
                        else {}
                    ),
                },
            }
        # The request-selected profile is authoritative even when its saved
        # snapshot is temporarily unavailable.  Keeping the prior graph's ID
        # here makes downstream tool-policy evaluation run under the wrong
        # profile while retaining stale graph connections.
        base_profile["profile_id"] = snapshot_profile_id
    if not isinstance(base_profile, dict) or not base_profile:
        return runtime_profile, str(agent_id or "").strip()

    policy = base_profile.get("policy") if isinstance(base_profile.get("policy"), dict) else {}
    policy_tool_ids = _profile_policy_tool_ids(policy) or _profile_node_allowed_actions(base_profile)
    resolved_agent_id = _profile_client_agent_id(base_profile, fallback=agent_id)
    current_tool_ids = _runtime_profile_agent_tool_refs(base_profile, resolved_agent_id)
    tool_ids = _merge_profile_tool_ids(
        current_tool_ids,
        policy_tool_ids,
    )
    if current_tool_ids and tool_ids == current_tool_ids:
        return base_profile, resolved_agent_id
    if not tool_ids:
        return base_profile, resolved_agent_id

    patched = dict(base_profile)
    defaultspack = (
        dict(patched.get("defaultspack"))
        if isinstance(patched.get("defaultspack"), dict)
        else {}
    )
    agents = (
        dict(defaultspack.get("agents"))
        if isinstance(defaultspack.get("agents"), dict)
        else {}
    )
    selected_agent = (
        dict(agents.get(resolved_agent_id))
        if isinstance(agents.get(resolved_agent_id), dict)
        else {}
    )
    selected_agent.setdefault("node_instance_id", resolved_agent_id)
    selected_agent.setdefault("node_id", "defaultspack.agent")
    selected_agent["tools"] = list(tool_ids)
    agents[resolved_agent_id] = selected_agent
    defaultspack["agents"] = agents
    patched["defaultspack"] = defaultspack
    if snapshot_profile_id:
        patched["profile_id"] = snapshot_profile_id
    return patched, resolved_agent_id


def _propagate_conversation_workspace(
    request_context: dict[str, Any],
    message_metadata: dict[str, Any] | None,
    conversation_metadata: dict[str, Any] | None,
) -> None:
    if not isinstance(request_context, dict):
        return
    workspace_id = _first_non_empty_str(
        request_context, message_metadata, conversation_metadata, keys=_WORKSPACE_ID_KEYS
    )
    if workspace_id:
        request_context["workspace_id"] = workspace_id
    workspace_root = _first_non_empty_str(
        request_context,
        message_metadata,
        conversation_metadata,
        keys=_WORKSPACE_ROOT_KEYS,
    )
    if workspace_root:
        request_context["workspace_root"] = workspace_root
    if request_context.get("workspace_id") and request_context.get("workspace_root"):
        return
    try:
        from domain.coding.workspace_store import WorkspaceStore

        store = WorkspaceStore()
        selected_workspace_id = store.selected_workspace_id()
        if not selected_workspace_id:
            return
        selected = store.get(selected_workspace_id)
        if not isinstance(selected, dict):
            return
        request_context.setdefault("workspace_id", selected_workspace_id)
        root_path = selected.get("root_path")
        if isinstance(root_path, str) and root_path.strip():
            request_context.setdefault("workspace_root", root_path.strip())
    except Exception:
        return


def _approval_followup_tool_context(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    followup = metadata.get("approval_followup")
    if not isinstance(followup, dict):
        return {}
    token = str(followup.get("approval_token") or followup.get("token") or "").strip()
    tool_name = str(followup.get("tool_name") or "").strip()
    if not token or not tool_name:
        return {}
    action = str(followup.get("action") or "").strip()
    operation = str(followup.get("operation") or action or "").strip()
    request_id = str(
        followup.get("request_id") or followup.get("approval_request_id") or ""
    ).strip()
    token_map = {tool_name: token}
    if action:
        token_map[action] = token
    if operation:
        token_map[operation] = token
    if request_id:
        token_map[request_id] = token
    if tool_name in {"computer_use", "browser_use", "browser_computer"} and any(
        candidate.startswith(("computer.", "browser."))
        for candidate in (action, operation)
        if candidate
    ):
        for alias in ("computer_use", "browser_use", "browser_computer"):
            token_map[alias] = token
    return {"tool_approval_tokens": token_map}


def _apply_authority_context(
    request_context: dict[str, Any],
    metadata: dict[str, Any] | None,
    *,
    conversation_id: str,
    request_id: str,
    active_profile: dict[str, Any] | None,
) -> None:
    profile_id = str(
        request_context.get("profile_id")
        or request_context.get("active_startup_profile_id")
        or ((active_profile or {}).get("profile_id") if isinstance(active_profile, dict) else "")
        or ""
    ).strip()
    graph_id = str(
        request_context.get("graph_id")
        or request_context.get("capability_graph_id")
        or request_context.get("default_graph")
        or ((active_profile or {}).get("default_graph") if isinstance(active_profile, dict) else "")
        or ""
    ).strip()
    node_id = str(
        request_context.get("node_id") or request_context.get("runtime_node_id") or ""
    ).strip()
    authority_principal_id = build_principal_id(
        profile_id=profile_id,
        graph_id=graph_id,
        node_id=node_id,
        conversation_id=conversation_id,
    )
    if profile_id:
        request_context.setdefault("profile_id", profile_id)
    if graph_id:
        request_context.setdefault("graph_id", graph_id)
    if node_id:
        request_context.setdefault("node_id", node_id)

    followup: dict[str, str] = {}
    approval_tokens: dict[str, dict[str, str]] = {}
    allow_consumed_one_shot_tokens_for_run = False

    def add_authority_followup(raw: Any, *, prefer_primary: bool = True, require_issued: bool = False) -> None:
        nonlocal followup, allow_consumed_one_shot_tokens_for_run
        if not isinstance(raw, dict):
            return
        permission_id = str(raw.get("permission_id") or "").strip()
        if permission_id not in _AUTHORITY_FOLLOWUP_PERMISSION_IDS and not (
            permission_id.startswith("host.") or permission_id.startswith("authority.")
        ):
            return
        token = str(raw.get("approval_token") or raw.get("token") or "").strip()
        authority_request_id = str(raw.get("request_id") or raw.get("approval_request_id") or "").strip()
        if require_issued:
            issued = _authority_followup_was_issued(
                raw,
                conversation_id=conversation_id,
                principal_id=authority_principal_id,
            )
            if not issued:
                issued = _authority_followup_was_issued(
                    raw,
                    conversation_id=conversation_id,
                    principal_id=authority_principal_id,
                    include_consumed=True,
                )
                if issued:
                    allow_consumed_one_shot_tokens_for_run = True
            if not issued:
                return
        if token and authority_request_id:
            approval_tokens[permission_id] = {
                "approval_token": token,
                "request_id": authority_request_id,
                "permission_id": permission_id,
            }
        if prefer_primary or not followup:
            followup = {
                "approval_token": token,
                "request_id": authority_request_id,
                "permission_id": permission_id,
            }

    for raw_followup in _trusted_authority_followups_from_current_chain(conversation_id):
        for item in _authority_followup_approval_items(raw_followup):
            add_authority_followup(item, prefer_primary=False, require_issued=True)
        add_authority_followup(raw_followup, prefer_primary=False, require_issued=True)

    if isinstance(metadata, dict):
        raw_followup = metadata.get("authority_followup")
        if not isinstance(raw_followup, dict):
            raw_followup = metadata.get("approval_followup")
        if isinstance(raw_followup, dict):
            for item in _authority_followup_approval_items(raw_followup):
                add_authority_followup(item, prefer_primary=False)
            add_authority_followup(raw_followup, prefer_primary=True)

    authority_context = {
        "principal_id": authority_principal_id,
        "profile_id": profile_id or None,
        "graph_id": graph_id or None,
        "node_id": node_id or None,
        "conversation_id": conversation_id,
        "request_id": followup.get("request_id") or request_id,
        "run_request_id": request_id,
    }
    if followup.get("approval_token"):
        authority_context["approval_token"] = followup["approval_token"]
    if followup.get("permission_id"):
        authority_context["permission_id"] = followup["permission_id"]
    if approval_tokens:
        authority_context["approval_tokens"] = approval_tokens
    if allow_consumed_one_shot_tokens_for_run:
        authority_context["allow_consumed_one_shot_tokens_for_run"] = True
    request_context["authority_principal_id"] = authority_principal_id
    request_context["authority"] = authority_context


def _authority_followup_approval_items(raw_followup: Any) -> list[Any]:
    if not isinstance(raw_followup, dict):
        return []
    items: list[Any] = []
    for key in ("approvals", "related_approvals", "relatedApprovals"):
        raw_items = raw_followup.get(key)
        if isinstance(raw_items, list):
            items.extend(raw_items)
    return items


def _trusted_authority_followups_from_current_chain(conversation_id: str) -> list[dict[str, Any]]:
    """Return hidden authority followups since the last visible user turn."""
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return []
    try:
        conversation = ChatStore().get_conversation(conversation_id)
    except Exception:
        return []
    messages = conversation.get("messages") if isinstance(conversation, dict) else None
    if not isinstance(messages, list):
        return []

    followups: list[dict[str, Any]] = []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        raw_followup = _hidden_authority_followup_from_metadata(metadata)
        if raw_followup is not None:
            followups.append(raw_followup)
            continue
        if str(message.get("role") or "").strip().lower() == "user":
            if _approval_followup_from_metadata(metadata) is not None:
                continue
            break
    followups.reverse()
    return followups


def _hidden_authority_followup_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw_followup = metadata.get("authority_followup")
    if not isinstance(raw_followup, dict):
        raw_followup = metadata.get("authorityFollowup")
    if not isinstance(raw_followup, dict):
        return None
    chat_display = metadata.get("chat_display")
    if not isinstance(chat_display, dict):
        chat_display = metadata.get("chatDisplay")
    hidden = bool(raw_followup.get("hidden"))
    if isinstance(chat_display, dict):
        hidden = hidden or (
            bool(chat_display.get("hidden"))
            and str(chat_display.get("reason") or "").strip() == "authority_followup"
        )
    return raw_followup if hidden else None


def _approval_followup_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw_followup = metadata.get("approval_followup")
    if not isinstance(raw_followup, dict):
        raw_followup = metadata.get("approvalFollowup")
    return raw_followup if isinstance(raw_followup, dict) else None


def _authority_followup_was_issued(
    raw: dict[str, Any],
    *,
    conversation_id: str,
    principal_id: str,
    include_consumed: bool = False,
) -> bool:
    permission_id = str(raw.get("permission_id") or "").strip()
    authority_request_id = str(raw.get("request_id") or raw.get("approval_request_id") or "").strip()
    token = str(raw.get("approval_token") or raw.get("token") or "").strip()
    if not permission_id or not authority_request_id or not token:
        return False
    try:
        from core_runtime.legacy_runtime_removed import removed_authority_service

        service = removed_authority_service()
        issued = getattr(service, "one_shot_approval_issued", None)
        if not callable(issued):
            return False
        return bool(
            issued(
                request_id=authority_request_id,
                permission_id=permission_id,
                token=token,
                conversation_id=conversation_id,
                principal_id=principal_id,
                include_consumed=include_consumed,
            )
        )
    except Exception:
        return False


def _consume_turn_model_route_override(
    store: ChatStore,
    conversation_id: str,
    conversation: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    message_override = (
        metadata.get("model_route_override")
        if isinstance(metadata, dict) and isinstance(metadata.get("model_route_override"), dict)
        else {}
    )
    conversation_metadata = (
        conversation.get("metadata") if isinstance(conversation.get("metadata"), dict) else {}
    )
    conversation_override = (
        conversation_metadata.get("turn_model_route_override")
        if isinstance(conversation_metadata.get("turn_model_route_override"), dict)
        else {}
    )
    if conversation_override:
        updated_metadata = dict(conversation_metadata)
        updated_metadata.pop("turn_model_route_override", None)
        store.update_conversation(conversation_id, {"metadata": updated_metadata})
    return {
        **conversation_override,
        **message_override,
    }


def _replace_current_user_content_for_model(
    standard_messages: list[dict[str, Any]],
    *,
    role: str,
    runtime_content: str,
) -> None:
    if not runtime_content:
        return
    target_role = str(role or "user").strip() or "user"
    for message in reversed(standard_messages):
        if isinstance(message, dict) and str(message.get("role") or "user") == target_role:
            message["content"] = runtime_content
            return


def _conversation_system_prompt(conv: dict[str, Any], manager: Any) -> str:
    from blocks.chat._prompt_helpers import resolve_conversation_system_prompt
    from domain.kanban.prompt_note import append_kanban_system_prompt_note

    return append_kanban_system_prompt_note(resolve_conversation_system_prompt(conv, manager), conv)


def _load_active_v4_profile() -> dict[str, Any]:
    """Return a non-authoritative view of the verified v4 activation only."""
    try:
        from core_runtime.resolved_profile_scope import persisted_resolved_profile
    except Exception:
        return {}
    try:
        plan = persisted_resolved_profile()
    except Exception:
        return {}
    if plan is None:
        return {}
    return {
        "profile_id": str(plan.profile_id),
        "profile_revision": str(plan.profile_revision),
        "plan_hash": str(plan.plan_hash),
        "effective_pack_set": list(plan.effective_pack_set),
    }


def _merge_active_startup_profile_context(
    context: dict[str, Any],
    active_profile: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(context or {})
    if not isinstance(active_profile, dict):
        return merged

    profile_id = str(active_profile.get("profile_id") or "").strip()
    if not profile_id:
        return merged

    merged["profile_id"] = profile_id
    merged.setdefault(
        "resolved_profile",
        {
            "profile_id": profile_id,
            "profile_revision": active_profile.get("profile_revision"),
            "plan_hash": active_profile.get("plan_hash"),
            "effective_pack_set": list(active_profile.get("effective_pack_set") or []),
        },
    )
    return merged


def _attachment_text_blocks(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    remaining = MAX_ATTACHMENT_TEXT_CHARS
    for attachment in attachments:
        if remaining <= 0 or not isinstance(attachment, dict):
            break
        text = attachment.get("content")
        if not isinstance(text, str) or not text:
            continue
        limit = min(MAX_ATTACHMENT_TEXT_CHARS_PER_FILE, remaining)
        clipped = text[:limit]
        was_truncated = len(text) > limit or attachment.get("truncated") is True
        remaining -= len(clipped)
        name = str(attachment.get("name") or "unnamed").strip()[:200] or "unnamed"
        suffix = "\n..." if was_truncated else ""
        blocks.append(
            {
                "type": "text",
                "text": "\n\n添付ファイル: {}\n```\n{}{}\n```".format(name, clipped, suffix),
            }
        )
    return blocks


def _attachment_image_blocks(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        mime = str(attachment.get("type") or "").lower()
        data_url = attachment.get("dataUrl") or attachment.get("data_url")
        byte_length = _image_data_url_byte_length(data_url)
        if not mime.startswith("image/") or byte_length is None:
            continue
        size = attachment.get("size")
        if isinstance(size, int) and size > MAX_ATTACHMENT_IMAGE_BYTES:
            continue
        if byte_length > MAX_ATTACHMENT_IMAGE_BYTES:
            continue
        blocks.append({"type": "image_url", "image_url": {"url": data_url}})
    return blocks


def _attachment_audio_blocks(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        if _attachment_audio_transcript(attachment) and not _attachment_include_audio_with_transcript(attachment):
            continue
        mime = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
        data_url = attachment.get("dataUrl") or attachment.get("data_url")
        if not mime.startswith("audio/") or not isinstance(data_url, str) or not data_url.startswith("data:"):
            continue
        size = attachment.get("size")
        if isinstance(size, int) and size > MAX_ATTACHMENT_AUDIO_BYTES:
            continue
        byte_length = _audio_data_url_byte_length(data_url)
        if byte_length is None or byte_length > MAX_ATTACHMENT_AUDIO_BYTES:
            continue
        header, encoded = data_url.split(",", 1) if "," in data_url else ("", "")
        if not encoded:
            continue
        audio_format = _audio_format_from_mime(header or mime)
        blocks.append({"type": "input_audio", "input_audio": {"data": encoded, "format": audio_format}})
    return blocks


def _attachment_audio_placeholders(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        if _attachment_audio_transcript(attachment):
            continue
        mime = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
        if not mime.startswith("audio/"):
            continue
        name = str(attachment.get("name") or "ambient-recording").strip()[:200] or "ambient-recording"
        duration_ms = attachment.get("duration_ms") or attachment.get("durationMs")
        suffix = f" ({int(duration_ms)}ms)" if isinstance(duration_ms, (int, float)) else ""
        blocks.append({"type": "text", "text": f"\n\n音声入力: {name}{suffix}"})
    return blocks


def _attachment_audio_transcript_blocks(
    attachments: list[dict[str, Any]],
    *,
    existing_text: str = "",
) -> list[dict[str, Any]]:
    from blocks.chat.materialize_context import (
        materialized_audio_transcript,
        materialized_audio_transcript_blocks,
    )

    existing_normalized = _normalize_transcript_for_dedupe(existing_text)
    blocks: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        transcript = materialized_audio_transcript(attachment)
        if transcript and existing_normalized:
            transcript_normalized = _normalize_transcript_for_dedupe(transcript)
            if transcript_normalized and transcript_normalized in existing_normalized:
                continue
        blocks.extend(materialized_audio_transcript_blocks([attachment]))
    return blocks


def _content_text_for_transcript_dedupe(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "text") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _normalize_transcript_for_dedupe(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"音声入力の文字起こし\s*[:：][^\n]*(?:\n|$)", "\n", text)
    text = re.sub(r"^\s*文字起こし\s*[:：]\s*", "", text)
    return re.sub(r"\s+", "", text)


def _attachment_audio_transcript(attachment: dict[str, Any]) -> str:
    for key in ("transcript", "transcription", "text_transcript"):
        value = attachment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = attachment.get("metadata") if isinstance(attachment.get("metadata"), dict) else {}
    for key in ("transcript", "transcription", "text_transcript"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _attachment_include_audio_with_transcript(attachment: dict[str, Any]) -> bool:
    if bool(attachment.get("include_audio_with_transcript")):
        return True
    metadata = attachment.get("metadata") if isinstance(attachment.get("metadata"), dict) else {}
    return bool(metadata.get("include_audio_with_transcript"))


def _audio_format_from_mime(value: str) -> str:
    lowered = str(value or "").lower()
    if "audio/webm" in lowered:
        return "webm"
    if "audio/wav" in lowered or "audio/x-wav" in lowered:
        return "wav"
    if "audio/mp4" in lowered or "audio/m4a" in lowered:
        return "mp4"
    if "audio/mpeg" in lowered or "audio/mp3" in lowered:
        return "mp3"
    if "audio/ogg" in lowered:
        return "ogg"
    return "webm"


def _attachment_is_ephemeral(attachment: Any) -> bool:
    return isinstance(attachment, dict) and (
        bool(attachment.get("ephemeral"))
        or bool(attachment.get("do_not_persist"))
        or bool(attachment.get("no_persist"))
    )


def _sanitize_attachment_metadata(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        item = {
            key: attachment.get(key)
            for key in ("id", "name", "size", "type", "truncated", "source", "sourcePath")
            if key in attachment
        }
        if _attachment_audio_transcript(attachment):
            item["transcribed"] = True
            item["transcript_length"] = len(_attachment_audio_transcript(attachment))
            source = attachment.get("transcript_source") or attachment.get("transcription_source")
            metadata = attachment.get("metadata") if isinstance(attachment.get("metadata"), dict) else {}
            if not source:
                source = metadata.get("transcript_source") or metadata.get("transcription_source")
            if isinstance(source, str) and source.strip():
                item["transcript_source"] = source.strip()[:80]
            status = attachment.get("transcription_status") or metadata.get("transcription_status")
            if isinstance(status, str) and status.strip():
                item["transcription_status"] = status.strip()[:80]
            model = attachment.get("transcription_model") or metadata.get("transcription_model")
            if isinstance(model, str) and model.strip():
                item["transcription_model"] = model.strip()[:120]
            if _attachment_include_audio_with_transcript(attachment):
                item["audio_included_with_transcript"] = True
        sanitized.append(item)
    return sanitized


def _image_data_url_byte_length(data_url: Any) -> int | None:
    if not isinstance(data_url, str) or not data_url.startswith(_DATA_IMAGE_PREFIX):
        return None
    header, separator, encoded = data_url.partition(",")
    if not separator or ";base64" not in header.lower():
        return None
    try:
        return len(base64.b64decode(encoded, validate=True))
    except Exception:
        return None


def _audio_data_url_byte_length(data_url: Any) -> int | None:
    if not isinstance(data_url, str) or not data_url.startswith(_DATA_AUDIO_PREFIX):
        return None
    header, separator, encoded = data_url.partition(",")
    if not separator or ";base64" not in header.lower():
        return None
    try:
        return len(base64.b64decode(encoded, validate=True))
    except Exception:
        return None


def _normalize_tool_selection(input_data: dict[str, Any]) -> NormalizedToolSelection:
    if not isinstance(input_data, dict):
        return NormalizedToolSelection()
    params = input_data.get("params") if isinstance(input_data.get("params"), dict) else {}
    raw_selection = params.get("tool_selection")
    if isinstance(raw_selection, dict):
        include = _coerce_tool_items(raw_selection.get("include"))
        top_level_tools = _coerce_tool_items(input_data.get("tools"))
        exclude = _coerce_tool_items(raw_selection.get("exclude"))
        raw_mode = str(raw_selection.get("mode") or "").strip().lower()
        if raw_mode not in TOOL_SELECTION_MODES:
            raw_mode = "manual" if include else "auto"
        if raw_mode == "auto" and top_level_tools:
            include = _merge_tool_items(include, top_level_tools)
            raw_mode = "manual"
        if raw_mode == "manual" and not include:
            raw_mode = "none"
        scope = str(raw_selection.get("scope") or "turn").strip().lower() or "turn"
        return NormalizedToolSelection(
            mode=raw_mode,
            strategy=_normalize_tool_selection_strategy(raw_selection.get("strategy")),
            include=include,
            exclude=exclude,
            scope=scope,
            must_use=_coerce_optional_bool(raw_selection.get("must_use"), default=False),
            review=raw_mode == "review" or _coerce_optional_bool(raw_selection.get("review"), default=False),
            preview_id=str(raw_selection.get("preview_id") or "").strip() or None,
            source="tool_selection",
        )

    raw_tools = input_data.get("tools")
    if isinstance(raw_tools, list):
        include = _coerce_tool_items(raw_tools)
        return NormalizedToolSelection(
            mode="manual" if raw_tools else "none",
            include=include,
            source="tools",
        )

    tool_policy = params.get("tool_policy") if isinstance(params.get("tool_policy"), dict) else {}
    if "selected_tools" in tool_policy:
        selected = _coerce_tool_items(tool_policy.get("selected_tools"))
        return NormalizedToolSelection(
            mode="manual" if selected else "none",
            include=selected,
            source="tool_policy.selected_tools",
        )

    message = input_data.get("message") if isinstance(input_data.get("message"), dict) else {}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if "selected_tools" in metadata:
        selected = _coerce_tool_items(metadata.get("selected_tools"))
        return NormalizedToolSelection(
            mode="manual" if selected else "none",
            include=selected,
            source="message.metadata.selected_tools",
        )

    return NormalizedToolSelection()


def _apply_tool_selection_preview_snapshot(
    selection: NormalizedToolSelection,
    context: dict[str, Any],
    *,
    conversation_id: str,
    input_data: dict[str, Any] | None = None,
    user_text: str = "",
    model: str = "",
) -> NormalizedToolSelection:
    preview_id = str(selection.preview_id or "").strip()
    if not preview_id:
        return selection
    lookup_context = dict(context or {})
    if conversation_id:
        lookup_context["conversation_id"] = conversation_id
    try:
        snapshot = ToolSelectionPreviewStore().consume_authorized(
            preview_id,
            lookup_context,
            expected_bindings=preview_payload_bindings(
                input_data or {},
                lookup_context,
                user_text=user_text,
                model=model,
                catalog_tools=ToolRegistry().list_tools(),
            ),
        )
    except ToolSelectionPreviewAccessError as exc:
        raise ValueError(f"params.tool_selection.preview_id is invalid: {exc.code}") from exc
    raw_selection = snapshot.get("selection") if isinstance(snapshot.get("selection"), dict) else {}
    mode = str(raw_selection.get("mode") or selection.mode or "review").strip().lower()
    if mode not in TOOL_SELECTION_MODES:
        mode = "review"
    scope = str(raw_selection.get("scope") or selection.scope or "turn").strip().lower()
    if scope not in TOOL_SELECTION_SCOPES:
        scope = "turn"
    return NormalizedToolSelection(
        mode=mode,
        strategy=_normalize_tool_selection_strategy(raw_selection.get("strategy") or selection.strategy),
        include=_coerce_tool_items(raw_selection.get("include")),
        exclude=_coerce_tool_items(raw_selection.get("exclude")),
        scope=scope,
        must_use=_coerce_optional_bool(raw_selection.get("must_use"), default=selection.must_use),
        review=_coerce_optional_bool(
            raw_selection.get("review"),
            default=(mode == "review" or selection.review),
        ),
        preview_id=preview_id,
        source="tool_selection_preview",
    )


def _validate_tool_selection_input(input_data: dict[str, Any]) -> str | None:
    params = input_data.get("params") if isinstance(input_data.get("params"), dict) else {}
    if "tool_selection" not in params:
        return None
    raw_selection = params.get("tool_selection")
    if raw_selection is None:
        return None
    if not isinstance(raw_selection, dict):
        return "params.tool_selection must be an object"
    raw_mode = str(raw_selection.get("mode") or "auto").strip().lower()
    if raw_mode not in TOOL_SELECTION_MODES:
        return "params.tool_selection.mode must be one of auto, review, manual, none"
    raw_scope = str(raw_selection.get("scope") or "turn").strip().lower()
    if raw_scope not in TOOL_SELECTION_SCOPES:
        return "params.tool_selection.scope must be one of turn, conversation"
    raw_strategy = str(raw_selection.get("strategy") or "").strip().lower()
    if raw_strategy and raw_strategy not in TOOL_SELECTION_STRATEGIES:
        return "params.tool_selection.strategy must be one of hybrid, semantic, catalog_ai, all_with_hints, all_schemas, lexical"
    for field_name in ("include", "exclude"):
        invalid_reason = _invalid_tool_selection_items_reason(raw_selection.get(field_name))
        if invalid_reason:
            return f"params.tool_selection.{field_name} {invalid_reason}"
    must_use = _coerce_optional_bool(raw_selection.get("must_use"), default=False)
    include = _coerce_tool_items(raw_selection.get("include"))
    if raw_mode == "none" and must_use:
        return "params.tool_selection cannot combine mode=none with must_use=true"
    if raw_mode == "none" and include:
        return "params.tool_selection mode=none cannot include tools"
    normalized = _normalize_tool_selection(input_data)
    if normalized.mode == "none" and normalized.must_use:
        return "params.tool_selection cannot combine mode=none with must_use=true"
    return None


def _coerce_tool_items(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value:
        if isinstance(item, dict):
            target = normalize_tool_target(item)
            if target is not None:
                result.append(target.to_dict())
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                result.append(stripped)
    return result


def _caller_provider_tool_definitions(input_data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tools = input_data.get("tools") if isinstance(input_data, dict) else None
    if not isinstance(raw_tools, list):
        return []
    provider_tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    target_only_keys = {"kind", "id", "tool_id", "service_id"}
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        if normalize_tool_target(item) is not None and set(item).issubset(target_only_keys):
            continue
        name = tool_name_from_definition(item)
        if not name:
            continue
        key = _tool_definition_id(item) or name
        if key in seen:
            continue
        seen.add(key)
        provider_tools.append(dict(item))
    return provider_tools


def _invalid_tool_selection_items_reason(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return "must be an array"
    for item in value:
        if isinstance(item, str):
            if item.strip():
                continue
            return "must contain non-empty target IDs"
        if isinstance(item, dict):
            target = normalize_tool_target(item)
            allowed_keys = {"kind", "id", "tool_id", "service_id"}
            if target is not None and set(item).issubset(allowed_keys):
                continue
            return "must contain only string IDs or {kind, id} targets"
        return "must contain only string IDs or {kind, id} targets"
    return None


def _coerce_tool_id_list(value: Any) -> list[str]:
    return [
        str(item).strip()
        for item in _coerce_tool_items(value)
        if not isinstance(item, dict) and str(item).strip()
    ]


def _normalize_tool_selection_strategy(value: Any) -> str | None:
    strategy = str(value or "").strip().lower()
    return strategy if strategy in TOOL_SELECTION_STRATEGIES else None


def _merge_tool_items(*groups: list[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = _tool_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _tool_item_key(item: Any) -> str:
    if isinstance(item, dict):
        return f"{item.get('kind') or 'tool'}:{item.get('id') or item.get('tool_id') or item.get('service_id') or id(item)}"
    return str(item)


def _coerce_optional_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def _tool_selection_metadata(selection: NormalizedToolSelection) -> dict[str, Any]:
    include = [target.to_dict() for target in normalize_tool_targets(selection.include)]
    exclude = [target.to_dict() for target in normalize_tool_targets(selection.exclude)]
    return {
        "mode": selection.mode,
        "strategy": selection.strategy,
        "include": include,
        "exclude": exclude,
        "scope": selection.scope,
        "must_use": selection.must_use,
        "review": selection.review,
        "preview_id": selection.preview_id,
        "source": selection.source,
    }


def _resolve_selected_tools(
    raw_tools: Any,
    *,
    user_text: str = "",
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    registry = ToolRegistry()
    if not isinstance(raw_tools, list):
        tools = registry.list_tools()
        pack_root = Path(__file__).resolve().parents[2]
        mode = effective_tool_assist_mode(pack_root=pack_root)
        prefers_vector = _profile_prefers_vector_tool_assist(context)
        if mode == "all" and prefers_vector:
            mode = "vector"
        if mode == "off":
            return [], []
        if mode == "all_schemas":
            return tools, []
        candidate_tools = tools
        if prefers_vector:
            candidate_tools = filter_tool_definitions_for_runtime_profile(
                tools,
                None,
                policy_context=context or {},
            )
        always_tools, vector_tools = split_tools_by_loading(candidate_tools)
        recommended_ids = recommend_tool_ids(
            user_text,
            vector_tools,
            limit=tool_assist_limit(pack_root=pack_root),
        )
        recommended = set(recommended_ids)
        resolved = [
            *always_tools,
            *[tool for tool in vector_tools if str(tool.get("tool_id") or "") in recommended],
        ]
        if isinstance(context, dict):
            context["tool_assist"] = {
                "mode": mode,
                "recommended_tools": recommended_ids,
                "always_tools": [
                    str(tool.get("tool_id") or tool.get("name") or "") for tool in always_tools
                ],
                "available_tool_count": len(candidate_tools),
                "vector_candidate_count": len(vector_tools),
            }
        return resolved, []
    resolved = []
    unknown = []
    for item in raw_tools:
        if isinstance(item, dict):
            target = normalize_tool_target(item)
            target_only_keys = {"kind", "id", "tool_id", "service_id"}
            if target is not None and set(item).issubset(target_only_keys):
                if target.kind != "tool":
                    unknown.append(f"{target.kind}:{target.id}")
                    continue
                tool_def = registry.get(target.id)
                if tool_def is None:
                    unknown.append(f"tool:{target.id}")
                    continue
                resolved.append(tool_def)
                continue
            resolved.append(item)
            continue
        if not isinstance(item, str):
            continue
        tool_id = item.strip()
        if not tool_id:
            continue
        tool_def = registry.get(tool_id)
        if tool_def is None:
            unknown.append(tool_id)
            continue
        resolved.append(tool_def)
    return resolved, unknown


def _profile_prefers_vector_tool_assist(context: dict[str, Any] | None) -> bool:
    if not isinstance(context, dict):
        return False
    candidate = str(
        context.get("profile_id")
        or (
            context.get("profile_policy").get("profile_id")
            if isinstance(context.get("profile_policy"), dict)
            else ""
        )
        or ""
    ).strip()
    return candidate in _VECTOR_TOOL_ASSIST_PROFILE_IDS


def _infer_requested_tools_from_message(user_text: str) -> list[str]:
    inferred: list[str] = []
    seen: set[str] = set()

    def add(tool_id: str) -> None:
        value = str(tool_id or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        inferred.append(value)

    if isinstance(user_text, str) and (
        _COMPUTER_USE_REQUEST_RE.search(user_text)
        or _explicit_browser_navigation_requested({}, user_text)
    ):
        add("computer_use")
        add("browser_computer")

    if isinstance(user_text, str) and _CODING_PR_REQUEST_RE.search(user_text):
        for tool_id in _CODING_PR_TOOL_IDS:
            add(tool_id)

    for tool_id in _tool_mention_ids_from_text(user_text):
        add(tool_id)

    return inferred


def _tool_mention_ids_from_text(user_text: str) -> list[str]:
    if not isinstance(user_text, str) or "@" not in user_text:
        return []
    try:
        registry = ToolRegistry()
    except Exception:
        return []
    registered_tools: list[dict[str, Any]] = []
    try:
        registered_tools = [
            tool
            for tool in registry.list_tools()
            if isinstance(tool, dict)
            and str(tool.get("tool_id") or "").strip()
        ]
        known_tool_ids = [
            str(tool.get("tool_id") or "").strip()
            for tool in registered_tools
        ]
    except (AttributeError, TypeError):
        known_tool_ids = []
    tool_ids: list[str] = []
    seen: set[str] = set()
    for value in extract_mention_values(user_text, known_tool_ids):
        tool_id = str(value or "").strip()
        if not tool_id or tool_id in seen:
            continue
        try:
            tool_def = registry.get(tool_id)
        except Exception:
            tool_def = None
        if tool_def is None:
            continue
        seen.add(tool_id)
        tool_ids.append(tool_id)
    labels: dict[str, list[str]] = {}
    for tool in registered_tools:
        tool_id = str(tool.get("tool_id") or "").strip()
        display_name = str(tool.get("display_name") or "").strip()
        if not tool_id or not display_name:
            continue
        labels.setdefault(display_name.casefold(), []).append(tool_id)
    for label, matching_ids in sorted(
        labels.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if len(matching_ids) != 1:
            continue
        pattern = re.compile(
            rf"@{re.escape(label)}(?![\w./:-])",
            re.IGNORECASE,
        )
        if not any(
            is_mention_start(user_text, match.start(), known_tool_ids)
            for match in pattern.finditer(user_text)
        ):
            continue
        tool_id = matching_ids[0]
        if tool_id not in seen:
            seen.add(tool_id)
            tool_ids.append(tool_id)
    return tool_ids


def _verified_approval_followup_tool_ids(
    input_data: dict[str, Any],
) -> list[str]:
    """Recover the exact Tool authorized by a signed approval follow-up.

    The visible approval-continuation text no longer contains the original
    ``@Tool`` mention. Client-provided ``selected_tools`` is not sufficient
    authority for a low-level Tool, so validate the server-side approval and
    its one-shot token before treating this one Tool as explicitly selected.
    Token consumption remains the executor's responsibility.
    """

    message = (
        input_data.get("message")
        if isinstance(input_data, dict)
        and isinstance(input_data.get("message"), dict)
        else {}
    )
    metadata = (
        message.get("metadata")
        if isinstance(message.get("metadata"), dict)
        else {}
    )
    followup = (
        metadata.get("approval_followup")
        if isinstance(metadata.get("approval_followup"), dict)
        else {}
    )
    tool_id = str(followup.get("tool_name") or "").strip()
    request_id = str(
        followup.get("request_id")
        or followup.get("approval_request_id")
        or ""
    ).strip()
    token = str(
        followup.get("approval_token") or followup.get("token") or ""
    ).strip()
    if not (tool_id and request_id and token):
        return []

    try:
        from domain.safety import approval

        request = approval.get_approval_request(request_id)
    except Exception:
        return []
    if not isinstance(request, dict):
        return []
    if str(request.get("status") or "") != "approved":
        return []

    operation = str(request.get("operation") or "").strip()
    if operation != f"tool.{tool_id}":
        return []
    details = (
        request.get("details")
        if isinstance(request.get("details"), dict)
        else {}
    )
    request_tool_id = str(details.get("tool_name") or "").strip()
    if request_tool_id and request_tool_id != tool_id:
        return []
    args_hash = str(request.get("args_hash") or "").strip()
    if not args_hash:
        return []
    try:
        verification = approval.verify_execution_token(
            token,
            operation,
            args_hash,
            consume=False,
        )
    except Exception:
        return []
    if not getattr(verification, "valid", False):
        return []
    if str(getattr(verification, "request_id", "") or "").strip() != request_id:
        return []
    return [tool_id]


def _has_computer_use_tool(tool_ids: list[str]) -> bool:
    return any(str(tool_id or "").strip() in _COMPUTER_USE_TOOL_IDS for tool_id in tool_ids)


def _apply_requested_tool_policy(context: dict[str, Any], requested_tool_ids: list[str]) -> None:
    if not isinstance(context, dict) or not requested_tool_ids:
        return
    profile_policy = context.get("profile_policy")
    if isinstance(profile_policy, dict) and profile_policy.get("allow_shell") is False:
        return
    if not _requested_tool_ids_include_shell(requested_tool_ids):
        return
    context["user_requested_shell_tool"] = True


def _requested_tool_ids_include_shell(tool_ids: list[str]) -> bool:
    try:
        registry = ToolRegistry()
    except Exception:
        return False
    for tool_id in tool_ids:
        candidate = str(tool_id or "").strip()
        if not candidate:
            continue
        try:
            tool_def = registry.get(candidate)
        except Exception:
            tool_def = None
        if not isinstance(tool_def, dict):
            continue
        metadata = tool_def.get("metadata") if isinstance(tool_def.get("metadata"), dict) else {}
        category = str(tool_def.get("category") or metadata.get("category") or "").strip()
        action_type = str(tool_def.get("action_type") or metadata.get("action_type") or "").strip()
        capability_grants = tool_def.get("capability_grants")
        capability_grants = capability_grants if isinstance(capability_grants, list) else []
        tags = tool_def.get("tags")
        tags = tags if isinstance(tags, list) else []
        if (
            category == "shell"
            or action_type == "shell"
            or any(
                str(grant).strip().startswith(("shell.", "terminal."))
                for grant in capability_grants
            )
            or any(
                str(tag).strip().lower() in {"shell", "terminal", "command"}
                for tag in tags
            )
        ):
            return True
    return False


def _with_inferred_tools(
    input_data: dict[str, Any], inferred_tool_ids: list[str]
) -> dict[str, Any]:
    if not inferred_tool_ids:
        return input_data
    if _has_explicit_selected_tools(input_data):
        return input_data
    raw_tools = input_data.get("tools")
    existing_tools = list(raw_tools) if isinstance(raw_tools, list) else []
    merged = []
    seen = set()
    for item in existing_tools + list(inferred_tool_ids):
        key = item if isinstance(item, str) else id(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    updated = dict(input_data)
    updated["tools"] = merged
    return updated


def _with_frontend_precision_tool_selection(input_data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(input_data or {})
    params = dict(updated.get("params") if isinstance(updated.get("params"), dict) else {})
    selection = dict(params.get("tool_selection") if isinstance(params.get("tool_selection"), dict) else {})
    include = _merge_tool_items(_coerce_tool_items(selection.get("include")), list(PRECISION_TOOL_NAMES))
    exclude = [item for item in _coerce_tool_id_list(selection.get("exclude")) if item not in PRECISION_TOOL_NAMES]
    selection.update(
        {
            "mode": "auto",
            "include": include,
            "exclude": exclude,
            "scope": "turn",
            "must_use": True,
        }
    )
    params["tool_selection"] = selection
    updated["params"] = params
    return updated


def _frontend_precision_metadata(
    *,
    user_text: str,
    metadata: dict[str, Any],
    input_data: dict[str, Any],
    conversation_metadata: dict[str, Any],
    request_context: dict[str, Any],
) -> dict[str, Any] | None:
    files = _frontend_precision_file_hints(input_data, metadata, conversation_metadata)
    explicit = metadata.get("frontend_precision") if isinstance(metadata.get("frontend_precision"), dict) else {}
    command_hint = ""
    if explicit:
        mode = str(explicit.get("mode") or explicit.get("command") or "strict").strip()
        command_hint = f"/frontend {mode}" if mode else "/frontend strict"
    detection = detect_frontend_request(user_text, files=files, command=command_hint)
    if not detection.enabled:
        return None
    return precision_metadata(
        detection=detection,
        task=user_text,
        files=files,
        context=request_context,
    )


def _frontend_precision_file_hints(
    input_data: dict[str, Any],
    metadata: dict[str, Any],
    conversation_metadata: dict[str, Any],
) -> list[str]:
    values: list[Any] = []
    for source in (input_data, metadata, conversation_metadata):
        if not isinstance(source, dict):
            continue
        for key in ("files", "paths", "target_paths", "targetPaths", "changed_files", "changedFiles"):
            raw = source.get(key)
            if isinstance(raw, list):
                values.extend(raw)
            elif isinstance(raw, str):
                values.append(raw)
        workspace_root = source.get("workspace_root") or source.get("workspaceRoot")
        if workspace_root:
            values.append(workspace_root)
    message = input_data.get("message") if isinstance(input_data.get("message"), dict) else {}
    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict):
                for key in ("path", "name", "filename"):
                    if item.get(key):
                        values.append(item[key])
    return [str(item) for item in values if str(item or "").strip()]


def _has_explicit_selected_tools(input_data: dict[str, Any]) -> bool:
    selection = _normalize_tool_selection(input_data)
    return selection.mode in {"manual", "none"}


def _computer_use_preferences_from_text(user_text: str) -> dict[str, Any]:
    text = user_text if isinstance(user_text, str) else ""
    preferences = {}
    atlas_target = _COMPUTER_USE_ATLAS_TARGET_RE.search(text)
    if atlas_target:
        preferences["computer_use_target_app"] = "ChatGPT Atlas"
        preferences["computer_use_foreground_preferred"] = True
    elif _COMPUTER_USE_VIVALDI_TARGET_RE.search(text):
        preferences["computer_use_target_app"] = "Vivaldi"
    elif _COMPUTER_USE_CHROME_TARGET_RE.search(text) and not _COMPUTER_USE_CHROME_NEGATED_RE.search(
        text
    ):
        preferences["computer_use_target_app"] = "Google Chrome"
    if _COMPUTER_USE_LINE_TARGET_RE.search(text):
        preferences["computer_use_target_title"] = "LINE"
    elif not atlas_target and _COMPUTER_USE_CHATGPT_TARGET_RE.search(text):
        preferences["computer_use_target_title"] = "ChatGPT"
    if _COMPUTER_USE_PHYSICAL_INPUT_RE.search(text):
        preferences["computer_use_mouse_keyboard_requested"] = True
        preferences["computer_use_physical_clicks"] = True
    return preferences


def _apply_computer_use_context_preferences(
    context: dict[str, Any], user_text: str
) -> dict[str, Any]:
    updated = dict(context or {})
    preferences = _computer_use_preferences_from_text(user_text)
    for key, value in preferences.items():
        if value not in (None, "", False):
            updated[key] = value
    return updated


def _append_special_model_tools(
    tools: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    agent_id: Any = None,
) -> list[dict[str, Any]]:
    if not is_human_operator_model(str(context.get("model") or "").strip()):
        return tools
    if any(tool_name_from_definition(tool) == HUMAN_OPERATOR_TOOL_NAME for tool in tools):
        return tools
    tool_def = ToolRegistry().get(HUMAN_OPERATOR_TOOL_NAME)
    if not isinstance(tool_def, dict):
        return tools
    runtime_profile = context.get("runtime_profile")
    helper_tools = filter_tool_definitions_for_runtime_profile(
        [tool_def],
        runtime_profile,
        agent_id=agent_id,
        policy_context=context,
    )
    if not helper_tools:
        return tools
    return [*tools, *helper_tools]


def _requested_tool_ids_from_selection(selection: NormalizedToolSelection) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for target in normalize_tool_targets(selection.include):
        if target.kind != "tool":
            continue
        if target.id in seen:
            continue
        seen.add(target.id)
        ids.append(target.id)
    return ids


def _tool_name_set(tools: list[dict[str, Any]]) -> set[str]:
    return {
        name
        for name in (tool_name_from_definition(tool) for tool in tools)
        if name
    }


def _unselected_requested_tool_entries(
    requested_tool_ids: list[str],
    selected_tool_ids: list[str],
    registry_tools: list[dict[str, Any]],
    profile_filtered_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected = {str(tool_id).strip() for tool_id in selected_tool_ids if str(tool_id or "").strip()}
    registry_names = _tool_name_set(registry_tools)
    profile_names = _tool_name_set(profile_filtered_tools)
    entries: list[dict[str, Any]] = []
    for tool_id in requested_tool_ids:
        if tool_id in selected:
            continue
        if tool_id not in registry_names:
            reason_code = "unknown_selected_tool"
            reason = "selected tool is not registered in the tool catalog"
            suggestions = ["Remove the stale tool selection or install the missing tool."]
        elif tool_id not in profile_names:
            reason_code = "not_connected_to_profile"
            reason = "selected tool is not connected to the active runtime profile"
            suggestions = ["Connect the tool in the active runtime profile or choose a team workspace profile that includes it."]
        else:
            reason_code = "not_attached_to_turn"
            reason = "selected tool was eligible in the catalog but was not attached for this turn"
            suggestions = ["Check tool selection settings and this turn's disabled tools."]
        entries.append(
            {
                "tool_name": tool_id,
                "status": "blocked",
                "reason_code": reason_code,
                "reason": reason,
                "required": {"selected_tools": [tool_id]},
                "actual": {
                    "selected_tool_ids": sorted(selected),
                    "runtime_profile_connected_tools": sorted(profile_names),
                },
                "repair_suggestions": suggestions,
            }
        )
    return entries


def _available_tools(
    context: dict[str, Any],
    input_data: dict[str, Any],
    *,
    user_text: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    selection = _normalize_tool_selection(input_data)
    caller_provider_tools = _caller_provider_tool_definitions(input_data)
    resolved_context = resolve_runtime_profile_context(context or {})
    resolved_context["tool_selection"] = _tool_selection_metadata(selection)
    verified_explicit_tool_ids = list(
        dict.fromkeys(
            [
                *_tool_mention_ids_from_text(user_text),
                *_verified_approval_followup_tool_ids(input_data),
            ]
        )
    )
    if verified_explicit_tool_ids:
        resolved_context["verified_explicit_tool_ids"] = (
            verified_explicit_tool_ids
        )
    caller_provider_tool_ids = [
        tool_name_from_definition(tool)
        for tool in caller_provider_tools
        if tool_name_from_definition(tool)
    ]
    if caller_provider_tool_ids:
        resolved_context["caller_provider_tool_ids"] = caller_provider_tool_ids
    agent_id = input_data.get("agent_id") or resolved_context.get("agent_id")
    requested_tool_ids = _requested_tool_ids_from_selection(selection)
    if caller_provider_tool_ids:
        requested_tool_ids = list(
            dict.fromkeys([*requested_tool_ids, *caller_provider_tool_ids])
        )
    runtime_profile, agent_id = _runtime_profile_with_policy_connected_tools(
        resolved_context.get("runtime_profile"),
        profile_id=resolved_context.get("profile_id"),
        agent_id=agent_id,
    )
    if isinstance(runtime_profile, dict):
        resolved_context["runtime_profile"] = runtime_profile
    if agent_id:
        resolved_context["agent_id"] = agent_id
    settings: dict[str, Any] = {}
    profile_filtered: list[dict[str, Any]] = []
    try:
        settings = _read_frontend_settings()
        registry_tools = ToolRegistry().list_tools()
        profile_filtered = filter_tool_definitions_for_runtime_profile(
            registry_tools,
            runtime_profile,
            agent_id=agent_id,
            policy_context=resolved_context,
        )
        if _should_include_requested_computer_tools_for_turn(
            resolved_context,
            runtime_profile,
            agent_id,
        ):
            profile_filtered = _include_requested_computer_tools_for_turn(
                profile_filtered,
                registry_tools,
                requested_tool_ids,
                resolved_context,
            )
        service = ToolSelectionService(
            call_handler=resolved_context.get("call_handler"),
            settings=settings,
        )
        decision = service.select(
            user_text,
            profile_filtered,
            selection=selection,
            context=resolved_context,
        )
        filtered = list(decision.selected_tools)
        resolved_context["capability_settings_snapshot"] = settings
        selection_trace = decision.to_trace_dict()
        selected_tool_ids = [
            tool_name_from_definition(tool)
            for tool in filtered
            if tool_name_from_definition(tool)
        ]
        if requested_tool_ids:
            unselected_requested_tools = _unselected_requested_tool_entries(
                requested_tool_ids,
                selected_tool_ids,
                registry_tools,
                profile_filtered,
            )
            resolved_context["requested_tool_ids"] = requested_tool_ids
            if unselected_requested_tools:
                resolved_context["unselected_requested_tools"] = unselected_requested_tools
        resolved_context["tool_selection"] = {
            **resolved_context["tool_selection"],
            **selection_trace,
        }
        _persist_tool_selection_trace(
            resolved_context,
            settings,
            decision,
            user_text=user_text,
            trace=selection_trace,
        )
        if decision.unknown_targets:
            resolved_context["unknown_selected_tools"] = list(decision.unknown_targets)
    except Exception as exc:
        filtered = []
        resolved_context["tool_selection"] = {
            **resolved_context["tool_selection"],
            "selection_id": gen_id(),
            "stage": "selection_failed",
            "fallbacks": [{"stage": "tool_selection_service", "reason": str(exc)}],
            "selected_tool_ids": [],
            "eligible_count": 0,
            "candidate_count": 0,
            "duration_ms": 0,
        }
        if isinstance(exc, PermissionError):
            filtered = []
            resolved_context["tool_selection"]["stage"] = "selection_forbidden"
        elif selection.mode == "manual":
            try:
                filtered, unknown_tools = _resolve_selected_tools(selection.include, user_text=user_text, context=resolved_context)
                if unknown_tools:
                    resolved_context["unknown_selected_tools"] = unknown_tools
            except Exception:
                filtered = []
        elif selection.mode != "none" and profile_filtered:
            try:
                fallback_settings = dict(settings)
                fallback_tools_settings = (
                    dict(fallback_settings.get("tools"))
                    if isinstance(fallback_settings.get("tools"), dict)
                    else {}
                )
                fallback_tools_settings["selection_strategy"] = "lexical"
                fallback_settings["tools"] = fallback_tools_settings
                fallback_selection = NormalizedToolSelection(
                    mode=selection.mode if selection.mode in {"auto", "review"} else "auto",
                    strategy="lexical",
                    include=selection.include,
                    exclude=selection.exclude,
                    scope=selection.scope,
                    must_use=selection.must_use,
                    review=selection.review,
                    preview_id=selection.preview_id,
                    source=selection.source,
                )
                fallback_decision = ToolSelectionService(
                    call_handler=resolved_context.get("call_handler"),
                    settings=fallback_settings,
                ).select(
                    user_text,
                    profile_filtered,
                    selection=fallback_selection,
                    context=resolved_context,
                )
                filtered = list(fallback_decision.selected_tools)
                fallback_trace = fallback_decision.to_trace_dict()
                resolved_context["tool_selection"] = {
                    **resolved_context["tool_selection"],
                    **fallback_trace,
                    "stage": "selection_failed_lexical_fallback",
                    "fallbacks": [
                        {"stage": "tool_selection_service", "reason": str(exc)},
                        *list(fallback_trace.get("fallbacks") or []),
                    ],
                }
            except Exception:
                filtered = []
    filtered = _merge_tool_definitions(filtered, caller_provider_tools)
    if caller_provider_tool_ids:
        selection_metadata = resolved_context.get("tool_selection")
        if isinstance(selection_metadata, dict):
            selection_metadata["provider_compat_tool_ids"] = caller_provider_tool_ids
    filtered = _append_special_model_tools(filtered, resolved_context, agent_id=agent_id)
    return filtered, adapt_tool_definitions(filtered), resolved_context


def _include_requested_computer_tools_for_turn(
    profile_filtered: list[dict[str, Any]],
    registry_tools: list[dict[str, Any]],
    requested_tool_ids: list[str],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    if not context.get("user_requested_computer_use") or not requested_tool_ids:
        return profile_filtered
    requested = {
        str(tool_id or "").strip()
        for tool_id in requested_tool_ids
        if str(tool_id or "").strip() in _COMPUTER_USE_TOOL_IDS
    }
    if not requested:
        return profile_filtered
    existing = _tool_name_set(profile_filtered)
    additions: list[dict[str, Any]] = []
    for tool in registry_tools:
        name = tool_name_from_definition(tool)
        if not name or name not in requested or name in existing:
            continue
        allowed = filter_tool_definitions_for_runtime_profile(
            [tool],
            None,
            policy_context=context,
        )
        if not allowed:
            continue
        additions.extend(allowed)
        existing.add(name)
    if not additions:
        return profile_filtered
    return [*profile_filtered, *additions]


def _should_include_requested_computer_tools_for_turn(
    context: dict[str, Any],
    runtime_profile: Any,
    agent_id: Any,
) -> bool:
    if not context.get("user_requested_computer_use"):
        return False
    if not isinstance(runtime_profile, dict):
        return True
    defaultspack = runtime_profile.get("defaultspack")
    if not isinstance(defaultspack, dict):
        return True
    agents = defaultspack.get("agents")
    if not isinstance(agents, dict) or not agents:
        return True
    if agent_id:
        agent = agents.get(str(agent_id))
        if isinstance(agent, dict):
            return not bool(agent.get("tools"))
        return True
    return False


def _persist_tool_selection_trace(
    resolved_context: dict[str, Any],
    settings: dict[str, Any],
    decision: Any,
    *,
    user_text: str,
    trace: dict[str, Any],
) -> None:
    tool_settings = settings.get("tools") if isinstance(settings.get("tools"), dict) else {}
    trace_mode = str(tool_settings.get("selector_trace") or "summary").strip().lower()
    if trace_mode not in {"none", "summary", "full"}:
        trace_mode = "summary"
    selection_metadata = resolved_context.get("tool_selection") if isinstance(resolved_context.get("tool_selection"), dict) else {}
    selection_metadata["trace_mode"] = trace_mode
    _attach_tool_selection_trace_authority(selection_metadata, resolved_context)
    if trace_mode == "none":
        selection_metadata.pop("selection_id", None)
        return
    if trace_mode == "summary":
        selection_metadata.pop("trace_conversation_id", None)
        return
    child_id = _create_hidden_tool_selection_conversation(
        resolved_context,
        decision,
        user_text=user_text,
        trace=trace,
    )
    if child_id:
        selection_metadata["trace_conversation_id"] = child_id


def _create_hidden_tool_selection_conversation(
    resolved_context: dict[str, Any],
    decision: Any,
    *,
    user_text: str,
    trace: dict[str, Any],
) -> str:
    conversation_id = str(resolved_context.get("conversation_id") or "").strip()
    if not conversation_id:
        return ""
    try:
        store = ChatStore()
        selector_model = _tool_selection_selector_model(resolved_context, decision, trace)
        trace_model = selector_model or str(resolved_context.get("model") or "").strip()
        trace_metadata = _tool_selection_trace_metadata(resolved_context, trace, trace_mode="full")
        child = store.create_conversation(
            model=trace_model,
            parent_conversation_id=conversation_id,
            conversation_kind="tool_selection_trace",
            metadata={
                "hidden": True,
                "tool_selection_trace": True,
                "selection_id": trace.get("selection_id"),
                "trace_mode": "full",
                **trace_metadata,
                **({"selector_model": selector_model} if selector_model else {}),
            },
        )
        child_id = str(child.get("id") or "") if isinstance(child, dict) else ""
        if not child_id:
            return ""
        child_metadata = child.get("metadata") if isinstance(child.get("metadata"), dict) else {}
        store.update_conversation(
            child_id,
            {
                "title": "Tool補助の記録 {}".format(str(trace.get("selection_id") or "")[:8]),
                "is_archived": True,
                "metadata": {
                    **child_metadata,
                    "hidden": True,
                    "tool_selection_trace": True,
                    "selection_id": trace.get("selection_id"),
                    "trace_mode": "full",
                    **trace_metadata,
                    **({"selector_model": selector_model} if selector_model else {}),
                },
            },
        )
        payload = {
            "user_text": user_text,
            "trace": trace,
            "decision": decision.to_trace_dict() if hasattr(decision, "to_trace_dict") else trace,
        }
        raw_text = json.dumps(payload, ensure_ascii=False, indent=2)
        store.add_message(
            child_id,
            {
                "id": gen_id(),
                "role": "system",
                "content": [{"type": "text", "text": raw_text}],
                "raw_text": raw_text,
                "metadata": {"hidden": True, "tool_selection_trace": True},
            },
        )
        return child_id
    except Exception:
        return ""


def _attach_tool_selection_trace_authority(
    selection_metadata: dict[str, Any],
    resolved_context: dict[str, Any],
) -> None:
    metadata = _tool_selection_trace_metadata(
        resolved_context,
        selection_metadata,
        trace_mode=str(selection_metadata.get("trace_mode") or "summary"),
    )
    for key, value in metadata.items():
        if value not in ("", None):
            selection_metadata[key] = value


def _tool_selection_trace_metadata(
    resolved_context: dict[str, Any],
    trace: dict[str, Any],
    *,
    trace_mode: str,
) -> dict[str, Any]:
    now = time.time()
    source_message_id = str(
        resolved_context.get("source_message_id")
        or resolved_context.get("message_id")
        or resolved_context.get("request_id")
        or ""
    ).strip()
    return {
        "owner_profile_id": _tool_selection_trace_profile_id(resolved_context),
        "conversation_id": str(resolved_context.get("conversation_id") or "").strip(),
        "source_message_id": source_message_id,
        "trace_mode": trace_mode,
        "created_at_epoch": trace.get("created_at_epoch") or now,
        "expires_at_epoch": trace.get("expires_at_epoch") or now + 7 * 24 * 60 * 60,
        "ephemeral": True,
        "purpose": "tool_selection_trace",
    }


def _tool_selection_trace_profile_id(resolved_context: dict[str, Any]) -> str:
    principal = resolved_context.get("_authenticated_principal") if isinstance(resolved_context, dict) else None
    if isinstance(principal, dict):
        candidate = str(principal.get("profile_id") or "").strip()
        if candidate:
            return candidate
    subject = resolved_context.get("_authority_subject") if isinstance(resolved_context, dict) else None
    if isinstance(subject, dict):
        candidate = str(subject.get("profile_id") or "").strip()
        if candidate:
            return candidate
    for key in ("profile_id", "input_profile_id", "active_profile_id"):
        candidate = str(resolved_context.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


def _tool_selection_selector_model(
    resolved_context: dict[str, Any],
    decision: Any,
    trace: dict[str, Any],
) -> str:
    for source in (
        trace,
        decision.to_trace_dict() if hasattr(decision, "to_trace_dict") else {},
        resolved_context.get("tool_selection") if isinstance(resolved_context.get("tool_selection"), dict) else {},
    ):
        metrics = source.get("metrics") if isinstance(source, dict) and isinstance(source.get("metrics"), dict) else {}
        selector_model = (
            str(metrics.get("selector_model") or source.get("selector_model") or "").strip()
            if isinstance(source, dict)
            else ""
        )
        if selector_model:
            return selector_model
    return ""


def _read_frontend_settings() -> dict[str, Any]:
    path = frontend_settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _ensure_must_use_has_eligible_tools(
    selection: NormalizedToolSelection, tools: list[dict[str, Any]]
) -> None:
    if selection.must_use and not tools:
        raise ValueError("params.tool_selection.must_use requires at least one eligible tool")


def _merge_tool_definitions(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for tool in group:
            key = _tool_definition_id(tool) or str(id(tool))
            if key in seen:
                continue
            seen.add(key)
            merged.append(tool)
    return merged


def _tool_definition_id(tool: dict[str, Any]) -> str:
    if not isinstance(tool, dict):
        return ""
    return str(
        tool.get("tool_id") or tool_name_from_definition(tool) or tool.get("name") or ""
    ).strip()
