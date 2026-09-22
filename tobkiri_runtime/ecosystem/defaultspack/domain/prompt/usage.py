from __future__ import annotations

import copy
from typing import Any

from core_runtime.ai_input_graph_builder import MODEL_INPUT_NODE_ID, build_ai_input_graph_response
from core_runtime.ai_input_models import normalize_ai_input_config
from core_runtime.ai_input_tokenizer import apply_tokenizer_to_ai_input_response
from core_runtime.ai_input_trace_store import AiInputTraceStore
from core_runtime.resolved_profile_scope import persisted_resolved_profile
from core_runtime.runtime_audit_helpers import redact_sensitive
from domain.prompt.studio_client import (
    authored_edge_states,
    prompt_owner_available,
    write_authored_edge_state,
)


def active_prompt_summary(input_data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    profile_id = _resolve_profile_id(data.get("profile_id"))
    profile = _load_profile(profile_id)
    profile = _profile_with_owner_edge_states(profile_id, profile)
    request_context = _request_context(data)
    model_profile_id = _model_profile_id(data, request_context)
    model = _model_name(data, request_context, model_profile_id)
    model_profiles = _model_profiles(data)
    if model_profile_id:
        request_context.setdefault("model_profile_id", model_profile_id)
    if model:
        request_context.setdefault("model", model)
    include_text = _truthy(data.get("include_text"), default=False)
    response = build_ai_input_graph_response(
        profile,
        include_text=include_text or bool(model_profile_id or model),
        request_context=request_context,
    )
    response = apply_tokenizer_to_ai_input_response(
        response,
        model_profile_id=model_profile_id,
        model=model,
        profiles=model_profiles,
    )
    usage = prompt_usage_from_graph_response(
        response,
        conversation_id=str(data.get("conversation_id") or request_context.get("conversation_id") or ""),
        run_id=str(data.get("run_id") or "active"),
        trace_id=str(data.get("trace_id") or "active"),
        include_text=include_text,
    )
    return {
        "profile_id": profile_id,
        "conversation_id": usage.get("conversation_id", ""),
        "summary": usage,
        "segments": usage.get("segments", []),
        "active_segments": usage.get("active_segments", []),
        "disabled_segments": usage.get("disabled_segments", []),
        "token_estimate": usage.get("token_estimate", {}),
        "graph": response.get("graph", {}),
        "gate_decisions": response.get("gate_decisions", []),
        "diagnostics": response.get("diagnostics", []),
        "ai_input": response.get("ai_input", {}),
    }


def list_prompt_traces(input_data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    profile_id = _resolve_profile_id(data.get("profile_id"))
    try:
        limit = int(data.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))
    traces = AiInputTraceStore().list_traces(profile_id, limit=limit)
    conversation_id = str(data.get("conversation_id") or "").strip()
    if conversation_id:
        traces = [trace for trace in traces if str(trace.get("conversation_id") or "") == conversation_id]
    return {"profile_id": profile_id, "traces": traces, "count": len(traces)}


def get_prompt_trace(input_data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    data = input_data if isinstance(input_data, dict) else {}
    profile_id = _resolve_profile_id(data.get("profile_id"))
    trace_id = str(data.get("trace_id") or data.get("id") or "").strip()
    if not trace_id:
        raise ValueError("trace_id is required")
    trace = AiInputTraceStore().get_trace(profile_id, trace_id)
    if trace is None:
        return None
    return {
        "profile_id": profile_id,
        "trace": _redacted_trace_detail(trace),
        "prompt_usage": prompt_usage_from_trace(
            trace,
            include_text=_truthy(data.get("include_text"), default=False),
        ),
        "redaction": {
            "default_redacted": not _truthy(data.get("include_text"), default=False),
            "raw_trace_returned": False,
        },
    }


def toggle_prompt_edge(input_data: dict[str, Any] | None = None, *, preview: bool = False) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    profile_id = _resolve_profile_id(data.get("profile_id"))
    edge_id = str(data.get("edge_id") or "").strip()
    if not edge_id:
        raise ValueError("edge_id is required")
    enabled = bool(data.get("enabled", True))
    profile = _load_profile(profile_id)
    profile = _profile_with_owner_edge_states(profile_id, profile)
    request_context = _request_context(data)
    model_profile_id = _model_profile_id(data, request_context)
    model = _model_name(data, request_context, model_profile_id)
    model_profiles = _model_profiles(data)
    if model_profile_id:
        request_context.setdefault("model_profile_id", model_profile_id)
    if model:
        request_context.setdefault("model", model)
    current = build_ai_input_graph_response(profile, include_text=False, request_context=request_context)
    edge = _find_edge(current.get("graph"), edge_id)
    if edge is None:
        raise ValueError(f"edge not found: {edge_id}")
    source_node = _find_node(current.get("graph"), str(edge.get("from_id") or ""))
    source_metadata = source_node.get("metadata") if isinstance(source_node, dict) and isinstance(source_node.get("metadata"), dict) else {}
    allow_disable = bool(source_metadata.get("metadata", source_metadata).get("allow_disable", True))
    if not enabled and not allow_disable:
        raise PermissionError("This prompt edge cannot be disabled.")

    patched_profile = _profile_with_edge_state(profile, edge_id=edge_id, enabled=enabled)
    response = build_ai_input_graph_response(
        patched_profile,
        include_text=_truthy(data.get("include_text"), default=False) or bool(model_profile_id or model),
        request_context=request_context,
    )
    response = apply_tokenizer_to_ai_input_response(
        response,
        model_profile_id=model_profile_id,
        model=model,
        profiles=model_profiles,
    )
    if not preview:
        if not prompt_owner_available():
            raise RuntimeError("prompt composition owner is unavailable")
        write_authored_edge_state(profile_id, edge_id, enabled)
    prompt_summary = prompt_usage_from_graph_response(
        response,
        conversation_id=str(data.get("conversation_id") or ""),
        run_id=str(data.get("run_id") or "toggle"),
        trace_id="preview_toggle" if preview else "active",
        include_text=_truthy(data.get("include_text"), default=False),
    )
    return {
        "profile_id": profile_id,
        "edge_id": edge_id,
        "enabled": enabled,
        "preview": preview,
        "ai_input": _compact_toggle_ai_input(response.get("ai_input", {})),
        "summary": compact_prompt_usage_for_metadata(prompt_summary),
    }


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _redacted_trace_detail(trace: dict[str, Any]) -> dict[str, Any]:
    token_estimate = trace.get("token_estimate") if isinstance(trace.get("token_estimate"), dict) else {}
    provider_summary = (
        trace.get("provider_payload_summary")
        if isinstance(trace.get("provider_payload_summary"), dict)
        else {}
    )
    return {
        "trace_id": trace.get("trace_id"),
        "created_at": trace.get("created_at"),
        "conversation_id": trace.get("conversation_id"),
        "run_id": trace.get("run_id"),
        "profile_id": trace.get("profile_id"),
        "token_estimate": redact_sensitive(token_estimate),
        "provider_payload_summary": redact_sensitive(provider_summary),
        "gate_decisions": redact_sensitive(trace.get("gate_decisions") if isinstance(trace.get("gate_decisions"), list) else []),
        "diagnostics": redact_sensitive(trace.get("diagnostics") if isinstance(trace.get("diagnostics"), list) else []),
        "blocked": redact_sensitive(trace.get("blocked") if isinstance(trace.get("blocked"), list) else []),
        "blocked_count": len(trace.get("blocked")) if isinstance(trace.get("blocked"), list) else 0,
        "effective_input_redacted": True,
    }


def prompt_usage_from_trace(trace: dict[str, Any], *, include_text: bool = True) -> dict[str, Any]:
    effective = trace.get("effective_input") if isinstance(trace.get("effective_input"), dict) else {}
    usage = _usage_payload(
        profile_id=str(trace.get("profile_id") or effective.get("profile_id") or ""),
        conversation_id=str(trace.get("conversation_id") or ""),
        run_id=str(trace.get("run_id") or ""),
        trace_id=str(trace.get("trace_id") or ""),
        effective=effective,
        graph=trace.get("graph") if isinstance(trace.get("graph"), dict) else {},
        token_estimate=trace.get("token_estimate") if isinstance(trace.get("token_estimate"), dict) else {},
        gate_decisions=trace.get("gate_decisions") if isinstance(trace.get("gate_decisions"), list) else [],
        diagnostics=trace.get("diagnostics") if isinstance(trace.get("diagnostics"), list) else [],
        blocked=trace.get("blocked") if isinstance(trace.get("blocked"), list) else [],
        include_text=include_text,
    )
    for segment in trace.get("runtime_prompt_segments", []) if isinstance(trace.get("runtime_prompt_segments"), list) else []:
        if isinstance(segment, dict):
            if not include_text:
                segment = {key: value for key, value in segment.items() if key not in {"text", "schema"}}
            usage = append_runtime_prompt_segment(usage, segment)
    return usage


def prompt_usage_from_graph_response(
    response: dict[str, Any],
    *,
    conversation_id: str = "",
    run_id: str = "",
    trace_id: str = "",
    include_text: bool = True,
) -> dict[str, Any]:
    effective = response.get("effective_input") if isinstance(response.get("effective_input"), dict) else {}
    return _usage_payload(
        profile_id=str(response.get("profile_id") or effective.get("profile_id") or ""),
        conversation_id=conversation_id,
        run_id=run_id,
        trace_id=trace_id,
        effective=effective,
        graph=response.get("graph") if isinstance(response.get("graph"), dict) else {},
        token_estimate=response.get("token_estimate") if isinstance(response.get("token_estimate"), dict) else {},
        gate_decisions=response.get("gate_decisions") if isinstance(response.get("gate_decisions"), list) else [],
        diagnostics=response.get("diagnostics") if isinstance(response.get("diagnostics"), list) else [],
        blocked=[],
        include_text=include_text,
    )


def compact_prompt_usage_for_metadata(usage: dict[str, Any]) -> dict[str, Any]:
    raw_segments = usage.get("segments") if isinstance(usage.get("segments"), list) else []
    segments = [
        _compact_prompt_usage_segment(segment)
        for segment in raw_segments
        if isinstance(segment, dict)
    ]
    return {
        "trace_id": usage.get("trace_id"),
        "profile_id": usage.get("profile_id"),
        "conversation_id": usage.get("conversation_id"),
        "run_id": usage.get("run_id"),
        "active_count": usage.get("active_count", 0),
        "disabled_count": usage.get("disabled_count", 0),
        "token_estimate": _compact_token_estimate(usage.get("token_estimate", {})),
        "segments": segments,
    }


def compact_active_prompt_summary_response(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    compact_usage = compact_prompt_usage_for_metadata(usage)
    return {
        "profile_id": payload.get("profile_id") or compact_usage.get("profile_id"),
        "conversation_id": payload.get("conversation_id") or compact_usage.get("conversation_id", ""),
        "summary": compact_usage,
        "token_estimate": compact_usage.get("token_estimate", {}),
        "gate_decisions": _compact_list(payload.get("gate_decisions"), limit=20),
        "diagnostics": _compact_list(payload.get("diagnostics"), limit=20),
    }


def _compact_token_estimate(value: Any) -> dict[str, Any]:
    estimate = value if isinstance(value, dict) else {}
    compact: dict[str, Any] = {}
    total = estimate.get("total")
    if isinstance(total, (int, float)):
        compact["total"] = int(total)
    by_port = estimate.get("by_port")
    if isinstance(by_port, dict):
        compact["by_port"] = {
            str(key): int(amount)
            for key, amount in by_port.items()
            if isinstance(amount, (int, float))
        }
    tokenizer = _compact_tokenizer(estimate.get("tokenizer"))
    if tokenizer:
        compact["tokenizer"] = tokenizer
    return compact


def _compact_toggle_ai_input(value: Any) -> dict[str, Any]:
    ai_input = value if isinstance(value, dict) else {}
    return {"disabled_edges": _compact_string_list(ai_input.get("disabled_edges"), limit=500)}


def _compact_tokenizer(value: Any) -> dict[str, Any]:
    tokenizer = value if isinstance(value, dict) else {}
    compact: dict[str, Any] = {}
    for key in ("available", "fallback"):
        if key in tokenizer:
            compact[key] = bool(tokenizer.get(key))
    for key in (
        "status",
        "source",
        "warning",
        "warning_code",
        "tokenizer_id",
        "tokenizer_profile_id",
        "tokenizer_provider_id",
        "tokenizer_model",
        "provider_id",
        "model_profile_id",
        "model",
    ):
        text = _compact_text(tokenizer.get(key), limit=220)
        if text:
            compact[key] = text
    return compact


def _compact_prompt_usage_segment(segment: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in (
        "id",
        "edge_id",
        "prompt_id",
        "label",
        "kind",
        "port",
        "status",
        "enabled",
        "source_type",
        "tokens",
        "allow_disable",
        "editable",
        "readonly_reason",
        "input_role",
        "source_priority",
    ):
        if key in segment:
            compact[key] = copy.deepcopy(segment.get(key))
    tokenizer = _compact_tokenizer(segment.get("tokenizer"))
    if tokenizer:
        compact["tokenizer"] = tokenizer
    source = _compact_source(segment.get("source"))
    if source:
        compact["source"] = source
    for key, limit in (("reason", 320), ("explanation", 320), ("preview", 280)):
        text = _compact_text(segment.get(key), limit=limit)
        if text:
            compact[key] = text
    text = str(segment.get("text") or "")
    if text:
        compact.setdefault("preview", _compact_text(text, limit=280))
        compact["has_full_text"] = True
    activation_detail = _compact_activation_detail(segment.get("activation_detail"))
    if activation_detail:
        compact["activation_detail"] = activation_detail
    safety_boundary = _compact_safety_boundary(segment.get("safety_boundary"))
    if safety_boundary:
        compact["safety_boundary"] = safety_boundary
    source_chain = _compact_source_chain(segment.get("source_chain"))
    if source_chain:
        compact["source_chain"] = source_chain
    tool_signal = _compact_tool_signal(segment.get("tool_signal"))
    if tool_signal:
        compact["tool_signal"] = tool_signal
    skill_signal = _compact_skill_signal(segment.get("skill_signal"))
    if skill_signal:
        compact["skill_signal"] = skill_signal
    return compact


def _compact_activation_detail(value: Any) -> dict[str, Any]:
    detail = value if isinstance(value, dict) else {}
    compact: dict[str, Any] = {}
    for key in ("state", "port", "edge_id", "edge_kind"):
        text = _compact_text(detail.get(key), limit=160)
        if text:
            compact[key] = text
    for key in ("effect", "control", "reason", "trigger"):
        text = _compact_text(detail.get(key), limit=260)
        if text:
            compact[key] = text
    return compact


def _compact_safety_boundary(value: Any) -> dict[str, Any]:
    boundary = value if isinstance(value, dict) else {}
    compact: dict[str, Any] = {}
    for key in ("passive_text_only", "can_grant_permissions", "can_call_tools", "can_mutate_chat_state"):
        if key in boundary:
            compact[key] = bool(boundary.get(key))
    summary = _compact_text(boundary.get("summary"), limit=260)
    if summary:
        compact["summary"] = summary
    return compact


def _compact_source_chain(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in value[:6]:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        for key in ("source_type", "layer", "selected", "prompt_id", "id", "reason"):
            if key in item:
                entry[key] = copy.deepcopy(item.get(key))
        source = _compact_source(item.get("source") or item.get("path"))
        if source:
            entry["source"] = source
        if entry:
            compact.append(entry)
    return compact


def _compact_tool_signal(value: Any) -> dict[str, Any]:
    signal = value if isinstance(value, dict) else {}
    compact: dict[str, Any] = {}
    for key in (
        "tool_id",
        "tool_name",
        "display_name",
        "provider_name",
        "source_pack_id",
        "selection_source",
    ):
        text = _compact_text(signal.get(key), limit=180)
        if text:
            compact[key] = text
    for key in ("available_to_model", "prompt_can_call_tool"):
        if key in signal:
            compact[key] = bool(signal.get(key))
    execution_boundary = _compact_text(signal.get("execution_boundary"), limit=260)
    if execution_boundary:
        compact["execution_boundary"] = execution_boundary
    skills = _compact_string_list(signal.get("skills"), limit=8)
    if skills:
        compact["skills"] = skills
    triggers = _compact_string_list(signal.get("skill_triggers"), limit=8)
    if triggers:
        compact["skill_triggers"] = triggers
    return compact


def _compact_skill_signal(value: Any) -> dict[str, Any]:
    signal = value if isinstance(value, dict) else {}
    compact: dict[str, Any] = {}
    triggered_by = _compact_text(signal.get("triggered_by"), limit=260)
    if triggered_by:
        compact["triggered_by"] = triggered_by
    if "prompt_can_call_tool" in signal:
        compact["prompt_can_call_tool"] = bool(signal.get("prompt_can_call_tool"))
    matched = signal.get("matched")
    if isinstance(matched, list):
        compact["matched"] = [
            {
                key: value
                for key, value in {
                    "id": _compact_text(item.get("id"), limit=160) if isinstance(item, dict) else "",
                    "display_name": _compact_text(item.get("display_name"), limit=160) if isinstance(item, dict) else "",
                    "triggers": _compact_string_list(item.get("triggers"), limit=8) if isinstance(item, dict) else [],
                    "applies_to_tools": _compact_string_list(item.get("applies_to_tools"), limit=8) if isinstance(item, dict) else [],
                }.items()
                if value
            }
            for item in matched[:8]
            if isinstance(item, dict)
        ]
    return compact


def _compact_list(value: Any, *, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return copy.deepcopy(value[:limit])


def _compact_string_list(value: Any, *, limit: int) -> list[str]:
    return [_compact_text(item, limit=160) for item in _list_strings(value)[:limit] if _compact_text(item, limit=160)]


def _compact_source(value: Any) -> str:
    text = _compact_text(value, limit=180)
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    if "/" in normalized:
        return normalized.rsplit("/", 1)[-1] or normalized
    return text


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def append_runtime_prompt_segment(usage: dict[str, Any] | None, segment: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(usage) if isinstance(usage, dict) else {}
    segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
    item = dict(segment)
    item.setdefault("status", "active")
    item.setdefault("enabled", True)
    item.setdefault("allow_disable", False)
    item.setdefault("editable", False)
    item.setdefault("readonly_reason", "runtime generated")
    item.setdefault("source_chain", [])
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    source_type = str(item.get("source_type") or metadata.get("source_type") or "")
    port = str(item.get("port") or "system")
    kind = str(item.get("kind") or _segment_kind(str(item.get("id") or ""), source_type, port))
    item["kind"] = kind
    item["reason"] = str(item.get("reason") or _reason_for_segment(item, metadata, {}))
    item.update(_segment_extras(item, item, metadata, {}))
    segments.append(item)
    payload["segments"] = segments
    payload["active_segments"] = [entry for entry in segments if entry.get("status") == "active"]
    payload["disabled_segments"] = [entry for entry in segments if entry.get("status") != "active"]
    payload["active_count"] = len(payload["active_segments"])
    payload["disabled_count"] = len(payload["disabled_segments"])
    token_estimate = payload.get("token_estimate") if isinstance(payload.get("token_estimate"), dict) else {}
    by_port = token_estimate.get("by_port") if isinstance(token_estimate.get("by_port"), dict) else {}
    port = str(item.get("port") or "system")
    tokens = int(item.get("tokens") or 0)
    by_port[port] = int(by_port.get(port) or 0) + tokens
    token_estimate["by_port"] = by_port
    token_estimate["total"] = int(token_estimate.get("total") or 0) + tokens
    payload["token_estimate"] = token_estimate
    return payload


def _usage_payload(
    *,
    profile_id: str,
    conversation_id: str,
    run_id: str,
    trace_id: str,
    effective: dict[str, Any],
    graph: dict[str, Any],
    token_estimate: dict[str, Any],
    gate_decisions: list[Any],
    diagnostics: list[Any],
    blocked: list[Any],
    include_text: bool,
) -> dict[str, Any]:
    active: list[dict[str, Any]] = []
    for port, key in (
        ("system", "system_segments"),
        ("developer", "developer_segments"),
        ("context", "context_segments"),
        ("tools", "tool_schemas"),
    ):
        for segment in effective.get(key) if isinstance(effective.get(key), list) else []:
            if isinstance(segment, dict):
                active.append(_segment_payload(segment, port=port, status="active", graph=graph, include_text=include_text))
    policy = effective.get("policy") if isinstance(effective.get("policy"), dict) else {}
    for segment in policy.get("segments") if isinstance(policy.get("segments"), list) else []:
        if isinstance(segment, dict):
            active.append(_segment_payload(segment, port="policy", status="active", graph=graph, include_text=include_text))

    disabled = [
        _disabled_payload(segment, graph=graph, include_text=include_text)
        for segment in effective.get("disabled_segments", [])
        if isinstance(segment, dict)
    ]
    segments = [*active, *disabled]
    return {
        "trace_id": trace_id,
        "profile_id": profile_id,
        "conversation_id": conversation_id,
        "run_id": run_id,
        "segments": segments,
        "active_segments": active,
        "disabled_segments": disabled,
        "active_count": len(active),
        "disabled_count": len(disabled),
        "token_estimate": token_estimate,
        "gate_decisions": gate_decisions,
        "diagnostics": diagnostics,
        "blocked": blocked,
        "source_counts": _source_counts(segments),
    }


def _segment_payload(
    segment: dict[str, Any],
    *,
    port: str,
    status: str,
    graph: dict[str, Any],
    include_text: bool,
) -> dict[str, Any]:
    metadata = segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {}
    segment_id = str(segment.get("id") or "")
    edge_id = _edge_id_for(segment_id, port)
    graph_edge = _find_edge(graph, edge_id) or {}
    allow_disable = bool(metadata.get("allow_disable", True))
    source_type = str(segment.get("source_type") or metadata.get("source_type") or ("tool_schema" if port == "tools" else "prompt"))
    kind = _segment_kind(segment_id, source_type, port)
    payload = {
        "id": segment_id,
        "edge_id": edge_id,
        "prompt_id": str(metadata.get("prompt_id") or metadata.get("resolved_prompt_id") or segment.get("tool_id") or segment.get("name") or segment_id.removeprefix("prompt:")),
        "label": _segment_label(segment, metadata),
        "kind": kind,
        "port": port,
        "status": status,
        "enabled": status == "active",
        "source": str(segment.get("source") or metadata.get("source") or ""),
        "source_type": source_type,
        "source_chain": metadata.get("source_chain") if isinstance(metadata.get("source_chain"), list) else [],
        "tokens": int(segment.get("tokens") or 0),
        "reason": str(segment.get("reason") or _reason_for_segment({"kind": kind, "port": port, "status": status, "source_type": source_type, "edge_id": edge_id, "source": segment.get("source")}, metadata, graph_edge)),
        "allow_disable": allow_disable,
        "editable": _is_editable(source_type, metadata),
        "readonly_reason": _readonly_reason(source_type, metadata),
        "preview": str(segment.get("preview") or ""),
        "metadata": copy.deepcopy(metadata),
        "edge": graph_edge,
    }
    tokenizer = _compact_tokenizer(segment.get("tokenizer"))
    if tokenizer:
        payload["tokenizer"] = tokenizer
    payload.update(_segment_extras(payload, segment, metadata, graph_edge))
    if include_text and "text" in segment:
        payload["text"] = str(segment.get("text") or "")
    if include_text and "schema" in segment:
        payload["schema"] = copy.deepcopy(segment.get("schema"))
    return payload


def _disabled_payload(segment: dict[str, Any], *, graph: dict[str, Any], include_text: bool) -> dict[str, Any]:
    metadata = segment.get("metadata") if isinstance(segment.get("metadata"), dict) else {}
    source_type = str(segment.get("source_type") or metadata.get("source_type") or ("tool_schema" if str(segment.get("id") or "").startswith("tool_schema:") else "prompt"))
    segment_id = str(segment.get("id") or "")
    port = _port_for_segment(segment_id, source_type)
    edge_id = _edge_id_for(segment_id, port)
    graph_edge = _find_edge(graph, edge_id) or {}
    reason = str(segment.get("reason") or "")
    status = "budget-dropped" if reason == "budget_exceeded" else "gated" if reason == "edge_disabled_or_gate_blocked" and not _edge_disabled_by_user(graph_edge) else "disabled"
    payload = _segment_payload(
        {**segment, "source_type": source_type},
        port=port,
        status=status,
        graph=graph,
        include_text=include_text,
    )
    payload["enabled"] = False
    payload["reason"] = _disabled_reason_label(reason, graph_edge)
    return payload


def _resolve_profile_id(value: Any = None) -> str:
    plan = persisted_resolved_profile()
    if plan is None:
        raise RuntimeError("Pack v4 resolved profile is not active")
    candidate = str(value or plan.profile_id).strip()
    if candidate != str(plan.profile_id):
        raise PermissionError("requested Profile is not the verified v4 activation")
    return candidate


def _load_profile(profile_id: str) -> dict[str, Any]:
    return _load_raw_profile(profile_id)


def _load_raw_profile(profile_id: str) -> dict[str, Any]:
    plan = persisted_resolved_profile()
    if plan is None or str(plan.profile_id) != profile_id:
        raise PermissionError("Profile is not the verified v4 activation")
    return {
        "version": 4,
        "profile_id": str(plan.profile_id),
        "profile_revision": str(plan.profile_revision),
        "plan_hash": str(plan.plan_hash),
        "packs": list(plan.effective_pack_set),
        "metadata": {"authority": "verified-v4-activation"},
        "policy": {},
    }


def _request_context(data: dict[str, Any]) -> dict[str, Any]:
    context = data.get("request_context") if isinstance(data.get("request_context"), dict) else {}
    merged = dict(context)
    for key in ("conversation_id", "run_id", "message", "user_text", "knowledge_text", "memory_text", "model_profile_id", "model"):
        if key in data and data.get(key) is not None:
            merged[key] = data.get(key)
    return merged


def _model_profile_id(data: dict[str, Any], request_context: dict[str, Any]) -> str:
    return str(
        data.get("model_profile_id")
        or data.get("model_profile")
        or request_context.get("model_profile_id")
        or ""
    ).strip()


def _model_name(data: dict[str, Any], request_context: dict[str, Any], model_profile_id: str) -> str:
    return str(data.get("model") or request_context.get("model") or model_profile_id or "").strip()


def _model_profiles(data: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = data.get("model_profiles") if isinstance(data.get("model_profiles"), list) else data.get("profiles")
    return [profile for profile in profiles if isinstance(profile, dict)] if isinstance(profiles, list) else []


def _profile_with_edge_state(profile: dict[str, Any], *, edge_id: str, enabled: bool) -> dict[str, Any]:
    patched = copy.deepcopy(profile)
    metadata = patched.get("metadata") if isinstance(patched.get("metadata"), dict) else {}
    raw_ai_input = metadata.get("ai_input") if isinstance(metadata.get("ai_input"), dict) else {}
    ai_input = copy.deepcopy(raw_ai_input)
    disabled_edges = list(normalize_ai_input_config(raw_ai_input).get("disabled_edges") or [])
    if enabled:
        disabled_edges = [item for item in disabled_edges if item != edge_id]
    elif edge_id not in disabled_edges:
        disabled_edges.append(edge_id)
    ai_input["disabled_edges"] = disabled_edges
    metadata["ai_input"] = ai_input
    patched["metadata"] = metadata
    return patched


def _profile_with_owner_edge_states(
    profile_id: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    if not prompt_owner_available():
        return profile
    patched = copy.deepcopy(profile)
    metadata = (
        patched.get("metadata")
        if isinstance(patched.get("metadata"), dict)
        else {}
    )
    ai_input = (
        copy.deepcopy(metadata.get("ai_input"))
        if isinstance(metadata.get("ai_input"), dict)
        else {}
    )
    ai_input["disabled_edges"] = []
    metadata["ai_input"] = ai_input
    patched["metadata"] = metadata
    for edge_id, enabled in authored_edge_states(profile_id).items():
        patched = _profile_with_edge_state(
            patched,
            edge_id=edge_id,
            enabled=enabled,
        )
    return patched


def _find_edge(graph: Any, edge_id: str) -> dict[str, Any] | None:
    edges = graph.get("edges") if isinstance(graph, dict) else []
    for edge in edges if isinstance(edges, list) else []:
        if isinstance(edge, dict) and edge.get("id") == edge_id:
            return edge
    return None


def _find_node(graph: Any, node_id: str) -> dict[str, Any] | None:
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    for node in nodes if isinstance(nodes, list) else []:
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    return None


def _edge_id_for(segment_id: str, port: str) -> str:
    return f"edge:{segment_id}->{MODEL_INPUT_NODE_ID}.{port}"


def _port_for_segment(segment_id: str, source_type: str) -> str:
    if segment_id.startswith("tool_schema:"):
        return "tools"
    if source_type in {"memory_source", "retrieval_source"}:
        return "context"
    if source_type in {"profile_policy", "api_route"} or segment_id.startswith("policy:"):
        return "policy"
    return "system"


def _segment_label(segment: dict[str, Any], metadata: dict[str, Any]) -> str:
    return str(
        segment.get("name")
        or metadata.get("prompt_id")
        or metadata.get("resolved_prompt_id")
        or segment.get("tool_id")
        or segment.get("id")
        or "Prompt segment"
    )


def _segment_kind(segment_id: str, source_type: str, port: str) -> str:
    if segment_id.startswith("skill:") or source_type == "skill":
        return "skill"
    if port == "tools" or segment_id.startswith("tool_schema:"):
        return "tool-schema"
    if source_type == "memory_source":
        return "memory"
    if source_type == "retrieval_source":
        return "context"
    if source_type in {"profile_override", "profile_snapshot", "profile_prompt"}:
        return "profile"
    if source_type in {"extension", "canonical_fallback"}:
        return "extension"
    if source_type in {"pack", "pack_default"}:
        return "pack"
    if source_type == "component":
        return "component"
    if source_type == "api_route":
        return "context"
    return "prompt"


def _segment_extras(
    payload: dict[str, Any],
    segment: dict[str, Any],
    metadata: dict[str, Any],
    edge: dict[str, Any],
) -> dict[str, Any]:
    kind = str(payload.get("kind") or "prompt")
    source_type = str(payload.get("source_type") or "")
    port = str(payload.get("port") or "")
    reason = str(payload.get("reason") or _reason_for_segment(payload, metadata, edge))
    extras: dict[str, Any] = {
        "reason": reason,
        "explanation": reason,
        "input_role": _input_role(kind, port),
        "activation_detail": _activation_detail(payload, metadata, edge),
        "safety_boundary": _safety_boundary(kind),
        "source_priority": _source_priority(source_type),
    }
    tool_signal = _tool_signal(payload, segment, metadata)
    if tool_signal:
        extras["tool_signal"] = tool_signal
    skill_signal = _skill_signal(payload, metadata)
    if skill_signal:
        extras["skill_signal"] = skill_signal
    return extras


def _reason_for_segment(segment: dict[str, Any], metadata: dict[str, Any], edge: dict[str, Any]) -> str:
    status = str(segment.get("status") or "available")
    kind = str(segment.get("kind") or "")
    port = str(segment.get("port") or "")
    source_type = str(segment.get("source_type") or "")
    edge_id = str(segment.get("edge_id") or edge.get("id") or "").strip()
    if status != "active":
        return _reason_for_status(status, edge) or "Not included in this model input."
    if kind == "tool-schema":
        tool_name = str(metadata.get("display_name") or metadata.get("tool_name") or segment.get("label") or segment.get("prompt_id") or "this tool").strip()
        return (
            f"Tool schema exposed {tool_name} to the model as callable interface metadata. "
            "It can help the model request a tool call, but execution still requires tool policy, provider support, and authority approval."
        )
    if kind == "skill":
        return (
            "Runtime skill prompt matched the current message, selected skill, or tool metadata and was appended as system instructions for this response."
        )
    if kind == "memory":
        count = metadata.get("result_count")
        suffix = f" ({count} recalled item{'s' if count != 1 else ''})" if isinstance(count, int) and count > 0 else ""
        return f"Memory context was recalled for this conversation and inserted on the context port{suffix}."
    if kind == "context":
        if source_type == "api_route":
            return "Profile policy exposed this API route as policy context; it documents route availability and does not grant permissions by itself."
        count = metadata.get("result_count")
        suffix = f" ({count} result{'s' if count != 1 else ''})" if isinstance(count, int) and count > 0 else ""
        return f"Retrieved context matched the request and was inserted on the context port{suffix}."
    if port == "policy" or source_type == "profile_policy":
        return "Profile policy rules were connected to the policy port. They constrain behavior but do not originate from prompt text."
    if source_type == "profile_override":
        return "Profile override is the winning prompt source, so it replaced the snapshot/pack default in the model input."
    if source_type == "profile_snapshot":
        return "Profile snapshot supplied this prompt because no profile override was active."
    if source_type in {"pack", "pack_default"}:
        return "Pack default prompt was selected by the active profile and connected to the system prompt port."
    if source_type in {"extension", "canonical_fallback"}:
        return "Extension prompt was selected by the active profile and connected to the system prompt port."
    if edge_id:
        return f"Active AI Input Graph edge {edge_id} connected this segment to the {port or 'model'} input port."
    return "Selected by the active profile and included in the model input."


def _activation_detail(segment: dict[str, Any], metadata: dict[str, Any], edge: dict[str, Any]) -> dict[str, Any]:
    status = str(segment.get("status") or "available")
    kind = str(segment.get("kind") or "prompt")
    port = str(segment.get("port") or "")
    edge_id = str(segment.get("edge_id") or edge.get("id") or "")
    allow_disable = bool(segment.get("allow_disable", True))
    detail = {
        "state": status,
        "port": port,
        "edge_id": edge_id,
        "edge_kind": str(edge.get("kind") or ""),
        "effect": _input_role(kind, port),
        "control": "Can be toggled through AI Input Graph disabled_edges." if allow_disable else "Locked: allow_disable is false.",
        "reason": str(segment.get("reason") or ""),
    }
    if kind == "skill":
        detail["trigger"] = _skill_trigger_summary(metadata)
    elif kind == "tool-schema":
        detail["trigger"] = "Included because the tool is available to this profile/request and was not removed by the tool allowlist."
    elif kind in {"memory", "context"}:
        detail["trigger"] = str(metadata.get("source_kind") or segment.get("source") or "request context")
    else:
        detail["trigger"] = str(metadata.get("prompt_id") or metadata.get("resolved_prompt_id") or segment.get("prompt_id") or "profile selection")
    return detail


def _safety_boundary(kind: str) -> dict[str, Any]:
    summary = "Passive text only: cannot grant permissions, call tools, or mutate chat state."
    if kind == "tool-schema":
        summary = "Tool schema is interface metadata only; actual tool execution is checked by provider tool-calling, tool policy, and authority approval."
    elif kind == "skill":
        summary = "Skill prompt can add instructions for this response, but it cannot execute tools or bypass authority."
    return {
        "passive_text_only": True,
        "can_grant_permissions": False,
        "can_call_tools": False,
        "can_mutate_chat_state": False,
        "summary": summary,
    }


def _tool_signal(segment: dict[str, Any], raw_segment: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    kind = str(segment.get("kind") or "")
    if kind != "tool-schema":
        return {}
    tool_id = str(
        metadata.get("tool_id")
        or raw_segment.get("tool_id")
        or segment.get("prompt_id")
        or segment.get("id", "").removeprefix("tool_schema:")
        or ""
    ).strip()
    tool_name = str(metadata.get("tool_name") or raw_segment.get("name") or segment.get("label") or tool_id).strip()
    return {
        "tool_id": tool_id,
        "tool_name": tool_name,
        "display_name": str(metadata.get("display_name") or tool_name or tool_id),
        "provider_name": str(metadata.get("provider_name") or tool_name or tool_id),
        "source_pack_id": str(metadata.get("source_pack_id") or metadata.get("source") or ""),
        "available_to_model": str(segment.get("status") or "") == "active",
        "prompt_can_call_tool": False,
        "selection_source": "AI Input Graph tool schema segment",
        "execution_boundary": "The model may request this tool, then Rumi validates tool policy, provider support, local approval, and function/tool authority before any execution.",
        "skills": _list_strings(metadata.get("skills")),
        "skill_triggers": _list_strings(metadata.get("skill_triggers")),
    }


def _skill_signal(segment: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    kind = str(segment.get("kind") or "")
    if kind != "skill":
        return {}
    matched = metadata.get("matched_skills")
    matched_items = [dict(item) for item in matched if isinstance(item, dict)] if isinstance(matched, list) else []
    return {
        "matched": [
            {
                "id": str(item.get("id") or ""),
                "display_name": str(item.get("display_name") or item.get("id") or ""),
                "triggers": _list_strings(item.get("triggers")),
                "applies_to_tools": _list_strings(item.get("applies_to_tools")),
            }
            for item in matched_items
        ],
        "triggered_by": _skill_trigger_summary(metadata),
        "prompt_can_call_tool": False,
    }


def _skill_trigger_summary(metadata: dict[str, Any]) -> str:
    matched = metadata.get("matched_skills")
    if not isinstance(matched, list) or not matched:
        return "runtime skill selection"
    labels = []
    trigger_bits = []
    tool_bits = []
    for item in matched:
        if not isinstance(item, dict):
            continue
        label = str(item.get("display_name") or item.get("id") or "").strip()
        if label:
            labels.append(label)
        trigger_bits.extend(_list_strings(item.get("triggers")))
        tool_bits.extend(_list_strings(item.get("applies_to_tools")))
    parts = []
    if labels:
        parts.append("matched " + ", ".join(labels[:3]))
    if trigger_bits:
        parts.append("trigger words: " + ", ".join(dict.fromkeys(trigger_bits[:6])))
    if tool_bits:
        parts.append("tool scope: " + ", ".join(dict.fromkeys(tool_bits[:6])))
    return "; ".join(parts) if parts else "runtime skill selection"


def _input_role(kind: str, port: str) -> str:
    if kind == "tool-schema":
        return "tool schema exposed to the provider tools interface"
    if kind == "skill":
        return "runtime system instructions for this response"
    if kind == "memory":
        return "recalled memory inserted as context"
    if kind == "context":
        return "retrieved/context policy inserted into model context"
    if port == "policy":
        return "profile policy connected to the policy port"
    if port:
        return f"prompt text connected to the {port} port"
    return "prompt text connected to model input"


def _source_priority(source_type: str) -> str:
    if source_type == "profile_override":
        return "profile override wins over snapshots and pack defaults"
    if source_type == "profile_snapshot":
        return "profile snapshot wins over pack defaults unless an override exists"
    if source_type in {"pack", "pack_default"}:
        return "pack default is the fallback source"
    if source_type in {"extension", "canonical_fallback"}:
        return "extension-provided source"
    if source_type == "skill":
        return "runtime skill source, added after input-message matching"
    if source_type == "tool_schema":
        return "tool registry source, separate from prompt priority"
    return source_type or "runtime source"


def _list_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _is_editable(source_type: str, metadata: dict[str, Any]) -> bool:
    if source_type == "profile_override":
        return True
    if source_type in {"pack", "pack_default", "profile_snapshot", "component", "extension", "canonical_fallback"}:
        return False
    return not bool(metadata.get("read_only", False))


def _readonly_reason(source_type: str, metadata: dict[str, Any]) -> str:
    if _is_editable(source_type, metadata):
        return ""
    if source_type in {"pack", "pack_default"}:
        return "Pack prompts are read-only; create a profile override to edit."
    if source_type == "profile_snapshot":
        return "Profile snapshots are read-only; create a profile override to edit."
    if source_type == "component":
        return "Component prompts are read-only."
    if source_type in {"extension", "canonical_fallback"}:
        return "Extension prompts are read-only; create a profile override to edit."
    return "This segment is read-only in the current source."


def _reason_for_status(status: str, edge: dict[str, Any]) -> str:
    if status == "active":
        return "Connected to model input by the active AI Input Graph."
    if _edge_disabled_by_user(edge):
        return "Disabled by profile metadata.ai_input.disabled_edges."
    return ""


def _edge_disabled_by_user(edge: dict[str, Any]) -> bool:
    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    return bool(metadata.get("disabled_by_ai_input"))


def _disabled_reason_label(reason: str, edge: dict[str, Any]) -> str:
    if reason == "budget_exceeded":
        return "Dropped by the prompt token budget."
    if _edge_disabled_by_user(edge):
        return "Disabled by the user through AI Input Graph disabled_edges."
    if reason == "policy_disabled":
        return "Disabled by profile policy."
    if reason == "edge_disabled_or_gate_blocked":
        return "Blocked by a disabled edge or gate."
    return reason or "Disabled before provider payload assembly."


def _source_counts(segments: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for segment in segments:
        key = str(segment.get("kind") or segment.get("source_type") or "prompt")
        counts[key] = counts.get(key, 0) + 1
    return counts
