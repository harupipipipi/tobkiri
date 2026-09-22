import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.chat.tool_selection_preview import (
    PREVIEW_TTL_SECONDS,
    ToolSelectionPreviewStore,
    preview_context_metadata,
    preview_payload_bindings,
)
from domain.chat.tool_selection_service import ToolSelectionService
from domain.chat.tool_selection_schema import normalize_tool_targets
from domain.tool.registry import ToolRegistry
from domain.tool.schema_adapter import filter_tool_definitions_for_runtime_profile, resolve_runtime_profile_context


@dataclass
class _PreviewSelection:
    mode: str = "review"
    strategy: str | None = None
    scope: str = "turn"
    include: list[Any] = field(default_factory=list)
    exclude: list[Any] = field(default_factory=list)
    must_use: bool = False
    preview_id: str | None = None


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data dict is required", "INVALID_INPUT")
    raw = input_data.get("tool_selection") if isinstance(input_data.get("tool_selection"), dict) else {}
    user_text = str(input_data.get("user_text") or input_data.get("text") or input_data.get("message") or "")
    selection = _PreviewSelection(
        mode=str(raw.get("mode") or "review").strip().lower() or "review",
        strategy=str(raw.get("strategy") or "").strip().lower() or None,
        scope=str(raw.get("scope") or "turn").strip().lower() or "turn",
        include=[target.to_dict() for target in normalize_tool_targets(raw.get("include"))],
        exclude=[target.to_dict() for target in normalize_tool_targets(raw.get("exclude"))],
        must_use=bool(raw.get("must_use", False)),
    )
    base_context = dict(context) if isinstance(context, dict) else {}
    input_context = input_data.get("context") if isinstance(input_data.get("context"), dict) else {}
    if isinstance(input_context, dict):
        base_context.update(input_context)
    resolved_context = resolve_runtime_profile_context(base_context)
    selection, resolved_context = _apply_inferred_preview_tools(selection, raw, user_text, resolved_context)
    try:
        registry_tools = ToolRegistry().list_tools()
        tools = filter_tool_definitions_for_runtime_profile(
            registry_tools,
            resolved_context.get("runtime_profile"),
            policy_context=resolved_context,
        )
        decision = ToolSelectionService(
            call_handler=resolved_context.get("call_handler"),
        ).select(user_text, tools, selection=selection, context=resolved_context)
    except Exception as exc:
        return error("tool selection preview failed: " + str(exc), "SELECTION_PREVIEW_FAILED")
    trace = decision.to_trace_dict()
    preview_id = trace["selection_id"]
    expires_at_epoch = time.time() + PREVIEW_TTL_SECONDS
    ToolSelectionPreviewStore().save(
        {
            "preview_id": preview_id,
            **preview_context_metadata(
                resolved_context,
                conversation_id=str(input_data.get("conversation_id") or ""),
            ),
            "expires_at_epoch": expires_at_epoch,
            "bindings": preview_payload_bindings(
                input_data,
                resolved_context,
                user_text=user_text,
                model=str(input_data.get("model") or ""),
                catalog_tools=registry_tools,
            ),
            "selection": {
                "mode": selection.mode,
                "strategy": trace.get("strategy") or selection.strategy,
                "scope": selection.scope,
                "include": [
                    {"kind": "tool", "id": tool_id}
                    for tool_id in trace.get("selected_tool_ids", [])
                    if str(tool_id or "").strip()
                ],
                "exclude": selection.exclude,
                "must_use": selection.must_use,
                "review": selection.mode == "review",
                "preview_id": preview_id,
                "source": "tool_selection_preview",
            },
            "decision": trace,
        }
    )
    return ok(
        {
            "preview_id": preview_id,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_epoch)),
            "decision": {
                "selected_tools": trace["selected_tool_ids"],
                "selected_services": trace["selected_services"],
                "recommendations": trace["recommendations"],
                "permission_summary": trace["permission_summary"],
                "fallbacks": trace["fallbacks"],
                "metadata": trace,
            },
        }
    )


def _apply_inferred_preview_tools(selection, raw, user_text, context):
    if _has_explicit_preview_selection(raw):
        return selection, context
    try:
        from domain.chat.run_request import (
            _apply_computer_use_context_preferences,
            _has_computer_use_tool,
            _infer_requested_tools_from_message,
        )
    except Exception:
        return selection, context
    inferred_tool_ids = _infer_requested_tools_from_message(user_text)
    if not _has_computer_use_tool(inferred_tool_ids):
        return selection, context
    include = _merge_preview_includes(
        selection.include,
        [
            {"kind": "tool", "id": tool_id}
            for tool_id in inferred_tool_ids
            if str(tool_id or "").strip()
        ],
    )
    updated_selection = _PreviewSelection(
        mode=selection.mode,
        strategy=selection.strategy,
        scope=selection.scope,
        include=include,
        exclude=selection.exclude,
        must_use=selection.must_use,
        preview_id=selection.preview_id,
    )
    updated_context = _apply_computer_use_context_preferences(
        {**context, "user_requested_computer_use": True},
        user_text,
    )
    return updated_selection, updated_context


def _has_explicit_preview_selection(raw):
    if not isinstance(raw, dict):
        return False
    mode = str(raw.get("mode") or "").strip().lower()
    return mode in {"manual", "none"}


def _merge_preview_includes(left, right):
    merged = []
    seen = set()
    for item in [*(left or []), *(right or [])]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "tool").strip() or "tool"
        target_id = str(item.get("id") or item.get("tool_id") or "").strip()
        if not target_id:
            continue
        key = (kind, target_id)
        if key in seen:
            continue
        seen.add(key)
        merged.append({"kind": kind, "id": target_id})
    return merged
