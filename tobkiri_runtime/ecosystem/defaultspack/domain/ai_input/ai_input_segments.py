from __future__ import annotations

import json
from typing import Any

from .ai_input_models import PromptSegment, ToolSchemaSegment
from .ai_input_token_estimator import estimate_json_tokens, estimate_tokens
from core_runtime.pack_trust import is_pack_trusted
from core_runtime.profile_workspace import ProfileWorkspaceManager, profile_workspace_payload

from ..prompt.effective import resolve_effective_prompt
from ..tool.catalog_contract_client import (
    ContractToolCatalog as ToolRegistry,
)
from ..tool.schema_adapter import (
    adapt_tool_definition,
    tool_name_from_definition,
)
from transport.registry import canonical_http_route_specs


def collect_prompt_segments(
    profile: dict[str, Any],
    *,
    workspace_manager: ProfileWorkspaceManager | None = None,
    include_text: bool = True,
) -> list[PromptSegment]:
    profile_id = str(profile.get("profile_id") or "").strip()
    manager = workspace_manager or ProfileWorkspaceManager()
    prompt_ids = _profile_prompt_ids(profile)
    segments: list[PromptSegment] = []
    seen: set[str] = set()
    for index, prompt_id in enumerate(prompt_ids):
        if prompt_id in seen:
            continue
        seen.add(prompt_id)
        text, source, source_type, metadata = _resolve_prompt_text(profile, prompt_id, manager)
        trusted, trust_reason = _prompt_source_trust(source_type, metadata, profile)
        if not trusted:
            text = ""
        segment_id = f"prompt:{prompt_id}"
        if not text and include_text:
            text = ""
        segments.append(
            PromptSegment(
                id=segment_id,
                text=text if include_text else text,
                source=source,
                source_type=source_type,
                tokens=estimate_tokens(text),
                priority=50 + index,
                enabled=trusted,
                reason="" if trusted else "prompt_source_pack_untrusted",
                metadata={
                    "profile_id": profile_id,
                    "prompt_id": prompt_id,
                    "allow_disable": True,
                    **metadata,
                    "source_pack_trusted": trusted,
                    **({"source_pack_trust_reason": trust_reason} if trust_reason else {}),
                },
            )
        )
    return segments


def collect_tool_schema_segments(profile: dict[str, Any], available_tools: list[dict[str, Any]] | None = None) -> list[ToolSchemaSegment]:
    policy = _dict_value(profile.get("policy"))
    allowlist = _tool_allowlist(policy)
    tools = list(available_tools) if isinstance(available_tools, list) else list(ToolRegistry().list_tools())
    segments: list[ToolSchemaSegment] = []
    for tool in sorted(tools, key=lambda item: str(item.get("tool_id") or item.get("name") or "")):
        if not isinstance(tool, dict):
            continue
        tool_id = str(tool.get("tool_id") or tool.get("name") or "").strip()
        name = str(tool.get("name") or tool_id).strip()
        if not tool_id and not name:
            continue
        adapted = adapt_tool_definition(tool)
        schema: dict[str, Any] = {}
        if isinstance(adapted, dict):
            function_def = _dict_value(adapted.get("function"))
            schema = _dict_value(function_def.get("parameters"))
        if not schema:
            schema = _dict_value(tool.get("schema"))
        enabled = not allowlist or tool_id in allowlist or name in allowlist
        metadata = _dict_value(tool.get("metadata"))
        skill_ids = _string_list(tool.get("skills") or metadata.get("skills"))
        skill_triggers = _string_list(metadata.get("skill_triggers"))
        segments.append(
            ToolSchemaSegment(
                id=f"tool_schema:{tool_id or name}",
                tool_id=tool_id or name,
                name=name or tool_id,
                schema=dict(schema),
                tokens=estimate_json_tokens(schema),
                enabled=enabled,
                reason="" if enabled else "not_in_tool_allowlist",
                metadata={
                    "allow_disable": True,
                    "source": _tool_source(tool),
                    "provider_name": tool_name_from_definition(tool),
                    "tool_id": tool_id or name,
                    "tool_name": name or tool_id,
                    "display_name": str(tool.get("display_name") or metadata.get("display_name") or name or tool_id),
                    "description": str(tool.get("description") or metadata.get("description") or ""),
                    "source_pack_id": str(tool.get("source_pack_id") or metadata.get("source_pack_id") or ""),
                    "skills": skill_ids,
                    "skill_triggers": skill_triggers,
                },
            )
        )
    return segments


def collect_context_segments(
    profile: dict[str, Any],
    *,
    workspace_manager: ProfileWorkspaceManager | None = None,
    request_context: dict[str, Any] | None = None,
) -> list[PromptSegment]:
    del workspace_manager
    profile_id = str(profile.get("profile_id") or "").strip()
    context = request_context if isinstance(request_context, dict) else {}
    segments: list[PromptSegment] = []
    knowledge_text = str(context.get("knowledge_text") or "").strip()
    if knowledge_text:
        segments.append(
            PromptSegment(
                id="retrieval:knowledge.results",
                text=knowledge_text,
                source="knowledge.search_results",
                source_type="retrieval_source",
                tokens=estimate_tokens(knowledge_text),
                priority=20,
                enabled=True,
                metadata={
                    "profile_id": profile_id,
                    "allow_disable": True,
                    "source_kind": "knowledge",
                    "result_count": _result_count(context.get("knowledge_results")),
                },
            )
        )
    memory_text = str(context.get("memory_text") or "").strip()
    if memory_text:
        segments.append(
            PromptSegment(
                id="memory:conversation.recalled_memory",
                text=memory_text,
                source="memory.recall_results",
                source_type="memory_source",
                tokens=estimate_tokens(memory_text),
                priority=30,
                enabled=True,
                metadata={
                    "profile_id": profile_id,
                    "allow_disable": True,
                    "source_kind": "memory",
                    "result_count": _result_count(context.get("memory_results")),
                },
            )
        )
    return segments


def collect_api_route_segments(profile: dict[str, Any]) -> list[PromptSegment]:
    policy = _dict_value(profile.get("policy"))
    allowlist = _string_set(policy.get("api_route_allowlist"))
    if not allowlist:
        return []
    catalog = _api_route_catalog()
    enforce = bool(policy.get("enforce_api_route_allowlist", False))
    segments: list[PromptSegment] = []
    for index, route_id in enumerate(sorted(allowlist)):
        route = catalog.get(route_id, {"id": route_id})
        text = json.dumps(
            {
                "route": route_id,
                "handler": route.get("handler", ""),
                "always_available": bool(route.get("always_available", False)),
                "enforced": enforce,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        segments.append(
            PromptSegment(
                id=f"api_route:{_safe_node_suffix(route_id)}",
                text=text,
                source="profile.policy.api_route_allowlist",
                source_type="api_route",
                tokens=estimate_tokens(text),
                priority=30 + index,
                enabled=True,
                reason="" if enforce else "api_route_allowlist_preview_only",
                metadata={
                    "allow_disable": True,
                    "route_id": route_id,
                    "handler": route.get("handler", ""),
                    "always_available": bool(route.get("always_available", False)),
                    "enforce_api_route_allowlist": enforce,
                },
            )
        )
    return segments


def collect_policy_segment(profile: dict[str, Any]) -> PromptSegment:
    policy = _dict_value(profile.get("policy"))
    text = json.dumps(policy, ensure_ascii=False, sort_keys=True)
    return PromptSegment(
        id="policy:profile",
        text=text,
        source="profile.policy",
        source_type="profile_policy",
        tokens=estimate_json_tokens(policy),
        priority=10,
        enabled=True,
        metadata={"allow_disable": False},
    )


def _profile_prompt_ids(profile: dict[str, Any]) -> list[str]:
    metadata = _dict_value(profile.get("metadata"))
    candidates: list[Any] = [
        profile.get("system_prompt_id"),
        profile.get("default_prompt_id"),
        metadata.get("system_prompt_id"),
        metadata.get("default_prompt_id"),
        "default_chat",
    ]
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        prompt_id = str(item or "").strip()
        if not prompt_id or prompt_id in seen:
            continue
        seen.add(prompt_id)
        result.append(prompt_id)
    return result


def _resolve_prompt_text(
    profile: dict[str, Any],
    prompt_id: str,
    workspace_manager: ProfileWorkspaceManager,
) -> tuple[str, str, str, dict[str, Any]]:
    profile_id = str(profile.get("profile_id") or "").strip()
    workspace = profile_workspace_payload(workspace_manager.paths_for_profile(profile_id)) if profile_id else {}
    payload = {
        "profile_id": profile_id,
        "base_pack": profile.get("base_pack") or "defaultspack",
        "system_prompt_id": prompt_id,
        "default_prompt_id": profile.get("default_prompt_id"),
        "workspace": workspace,
    }
    try:
        effective = resolve_effective_prompt(payload)
    except Exception:
        return "", f"profile.prompt:{prompt_id}", "unresolved", {"prompt_resolution_error": True}
    text = str(effective.get("final_content") or effective.get("content") or "")
    return (
        text,
        str(effective.get("source") or f"profile.prompt:{prompt_id}"),
        str(effective.get("source_type") or "profile_prompt"),
        {
            **_dict_value(effective.get("metadata")),
            "resolved_prompt_id": effective.get("prompt_id"),
            "source_pack_id": effective.get("source_pack_id"),
            "source_pack_trusted": effective.get("source_pack_trusted"),
            "source_pack_trust_reason": effective.get("source_pack_trust_reason"),
            "source_chain": _list_value(effective.get("source_chain")),
        },
    )


def _prompt_source_trust(
    source_type: str,
    metadata: dict[str, Any],
    profile: dict[str, Any],
) -> tuple[bool, str | None]:
    if source_type not in {"pack_default", "component", "extension", "profile_snapshot"}:
        return True, None
    source_pack_id = str(metadata.get("source_pack_id") or "").strip()
    if not source_pack_id and source_type == "profile_snapshot":
        source_pack_id = str(profile.get("base_pack") or "").strip()
    if not source_pack_id:
        return False, "missing_source_pack_id"
    trusted, reason = is_pack_trusted(source_pack_id)
    return trusted, reason


def _tool_allowlist(policy: dict[str, Any]) -> set[str]:
    value = policy.get("tool_allowlist") or policy.get("enabled_tools") or policy.get("allowed_tools")
    return _string_set(value)


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.replace("\n", ",").split(",")]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _result_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _tool_source(tool: dict[str, Any]) -> str:
    metadata = _dict_value(tool.get("metadata"))
    return str(metadata.get("source_pack_id") or tool.get("source_pack_id") or metadata.get("source") or "")


def _dict_value(value: Any) -> dict[str, Any]:
    """Return a JSON object value, or an empty object for another shape."""
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    """Return a JSON array value, or an empty array for another shape."""
    return value if isinstance(value, list) else []


def _api_route_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    base_keys = {
        (spec.method, spec.pattern)
        for spec in canonical_http_route_specs(include_always_available=False)
    }
    for spec in canonical_http_route_specs(include_always_available=True):
        route_id = f"{spec.method} {spec.pattern}"
        catalog[route_id] = {
            "id": route_id,
            "handler": spec.block_module or spec.function_name or spec.handler_name or "",
            "always_available": (spec.method, spec.pattern) not in base_keys,
        }
    return catalog


def _safe_node_suffix(value: str) -> str:
    return (
        str(value or "")
        .strip()
        .replace(" ", "_")
        .replace("/", ".")
        .replace("{", "")
        .replace("}", "")
        .replace(":", "")
    )
