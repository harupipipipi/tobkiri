from __future__ import annotations

import json
from typing import Any

from domain.ai_client.model_call import call_model
from domain.agent.subagent_roles import get_subagent_role
from domain.agent.placement_catalog import compile_utility_effective_plan


_DELEGATE_CONTEXT_KEYS = (
    "conversation_id",
    "node_id",
    "graph_id",
    "agent_id",
    "company_id",
    "timezone",
)
_TRUSTED_AUTHORITY_KEYS = ("principal_id", "authority_principal_id")


class SubagentOrchestrator:
    def __init__(self, *, call_handler: Any = None) -> None:
        self._call_handler = call_handler

    def run(
        self,
        role_id: str,
        payload: dict[str, Any] | None = None,
        *,
        model: str = "",
        settings: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        role = get_subagent_role(role_id)
        if role is None:
            raise ValueError("unknown subagent role: " + str(role_id))
        payload = payload if isinstance(payload, dict) else {}
        selected_model = model or _model_for_role(role_id, settings or {})
        output = (
            self._run_with_model(role_id, payload, selected_model, role, context=context)
            if (selected_model or self._call_handler is not None)
            else None
        )
        if output is None:
            output = self._deterministic_output(role_id, payload)
        effective_plan = compile_utility_effective_plan(
            role_id,
            model=selected_model or "default",
            output_schema=str(role.get("output_schema") or "object"),
            maximum_tokens=int(role.get("max_tokens") or 800),
        )
        return {
            "role_id": role_id,
            "model": selected_model,
            "role": role,
            "agent_kind": "subagent",
            "runtime_kind": "utility_model_call",
            "subagent_role": str(
                role.get("subagent_role") or role_id
            ),
            "placement_id": effective_plan["placement"]["id"],
            "placement_revision": effective_plan["placement"]["revision"],
            "effective_plan_hash": effective_plan["plan_hash"],
            "effective_subagent_plan": effective_plan,
            "output": output,
            "events": [
                {
                    "type": "subagent_completed",
                    "role_id": role_id,
                    "model": selected_model,
                    "output_schema": role.get("output_schema"),
                    "runtime_kind": "utility_model_call",
                    "placement_id": effective_plan["placement"]["id"],
                    "effective_plan_hash": effective_plan["plan_hash"],
                }
            ],
        }

    def _run_with_model(
        self,
        role_id: str,
        payload: dict[str, Any],
        model: str,
        role: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        prompt = _prompt_for_role(role_id, payload, role)
        runtime_context = _trusted_model_context(context)
        runtime_context["_model_call_depth"] = 0
        try:
            response = call_model(
                {
                    "model_hint": model,
                    "question": prompt,
                    "output_schema": role.get("output_schema"),
                    "max_tokens": role.get("max_tokens", 800),
                    "thinking_level": "none",
                    "required_capabilities": ["model.image_input"] if role_id == "vision_ocr" else [],
                },
                runtime_context,
                call_handler=self._call_handler,
            )
        except Exception:
            return None
        if isinstance(response, dict) and response.get("status") == "ok":
            output = response.get("output")
            if isinstance(output, dict):
                return output
            parsed = _parse_json_response({"data": {"content": str(output or "")}})
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _deterministic_output(role_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if role_id == "tool_selector":
            tools = payload.get("candidate_tools") if isinstance(payload.get("candidate_tools"), list) else []
            selected = []
            for tool in tools[:8]:
                if not isinstance(tool, dict):
                    continue
                tool_id = str(tool.get("tool_id") or tool.get("name") or "").strip()
                if tool_id:
                    selected.append({"tool_id": tool_id, "confidence": 0.55, "reason": "keyword prefilter candidate"})
            return {"recommended_tools": selected, "not_selected": [], "requires_tool_calling_model": bool(selected)}
        if role_id == "prompt_compactor":
            text = str(payload.get("prompt") or "")
            return {"original_chars": len(text), "compact_chars": len(text.strip()), "suggested_prompt": text.strip(), "risk": "low"}
        if role_id == "context_summarizer":
            return {"summary": str(payload.get("text") or "")[:1200], "source": "deterministic"}
        if role_id == "model_router":
            return {"reason_codes": ["deterministic_router"], "selected_model": payload.get("preferred_model", "")}
        if role_id == "vision_ocr":
            return {"summary": "画像添付あり", "uncertainties": ["subagent did not call a vision model"]}
        return {}


def run_subagent(
    role_id: str,
    payload: dict[str, Any] | None = None,
    *,
    model: str = "",
    settings: dict[str, Any] | None = None,
    call_handler: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return SubagentOrchestrator(call_handler=call_handler).run(
        role_id,
        payload,
        model=model,
        settings=settings,
        context=context,
    )


def run_subagent_compat(
    role_id: str,
    payload: dict[str, Any] | None = None,
    *,
    model: str = "",
    settings: dict[str, Any] | None = None,
    call_handler: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cleaned_role_id = str(role_id or "").strip()
    cleaned_payload = payload if isinstance(payload, dict) else {}
    if get_subagent_role(cleaned_role_id) is not None:
        result = run_subagent(
            cleaned_role_id,
            cleaned_payload,
            model=model,
            settings=settings,
            call_handler=call_handler,
            context=context,
        )
        result["compatibility_alias"] = "subagent"
        result["route_kind"] = "utility_model_call"
        return result
    if cleaned_role_id in {"delegate", "agent_delegate", "task"} or str(cleaned_payload.get("task") or cleaned_payload.get("prompt") or "").strip():
        return _delegate_via_input(cleaned_role_id, cleaned_payload, model=model, context=context)
    raise ValueError("unknown subagent role: " + cleaned_role_id)


def _model_for_role(role_id: str, settings: dict[str, Any]) -> str:
    utility_models = settings.get("utility_models") if isinstance(settings.get("utility_models"), dict) else {}
    return str(utility_models.get(role_id) or utility_models.get("subagent_default") or "")


def _prompt_for_role(role_id: str, payload: dict[str, Any], role: dict[str, Any]) -> str:
    return (
        "You are a utility subagent. Return JSON only.\n"
        "role: {}\n"
        "schema: {}\n"
        "payload:\n{}"
    ).format(role_id, role.get("output_schema"), json.dumps(payload, ensure_ascii=False, indent=2)[:12000])


def _parse_json_response(response: Any) -> dict[str, Any] | None:
    data = response.get("data") if isinstance(response, dict) and response.get("status") == "ok" else response
    if isinstance(data, dict) and any(key in data for key in ("recommended_tools", "summary", "selected_model", "suggested_prompt")):
        return data
    content = data.get("content") if isinstance(data, dict) else None
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(str(block.get("text") or block) if isinstance(block, dict) else str(block) for block in content)
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_assistant_text_from_result(value: Any, *, _depth: int = 0) -> str:
    if _depth > 8:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text[:1] in {"{", "["}:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return text
            nested_text = extract_assistant_text_from_result(parsed, _depth=_depth + 1)
            return nested_text or text
        return text
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("raw_text")
                if text:
                    parts.append(str(text))
                    continue
            nested_text = extract_assistant_text_from_result(item, _depth=_depth + 1)
            if nested_text:
                parts.append(nested_text)
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if not isinstance(value, dict):
        return ""

    for key in ("assistant_text", "raw_text", "output_text", "text", "answer", "summary", "message", "content"):
        if key not in value:
            continue
        text = extract_assistant_text_from_result(value.get(key), _depth=_depth + 1)
        if text:
            return text

    for key in ("data", "result", "output", "response"):
        if key not in value:
            continue
        text = extract_assistant_text_from_result(value.get(key), _depth=_depth + 1)
        if text:
            return text

    transport_keys = {
        "status",
        "execution_id",
        "delegate",
        "result",
        "data",
        "output",
        "response",
        "code",
        "error",
        "is_error",
        "error_type",
    }
    if _depth > 0 and value and not set(value).issubset(transport_keys):
        return json.dumps(value, ensure_ascii=False)
    return ""


def _delegate_via_input(
    role_id: str,
    payload: dict[str, Any],
    *,
    model: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from domain.input.dispatcher import dispatch_input
    from domain.input.envelope import RumiInputEnvelope

    task = str(payload.get("task") or payload.get("prompt") or "").strip()
    if not task:
        raise ValueError("task is required for delegated compatibility alias")
    dispatch_context = _delegate_dispatch_context(payload, context)
    delegate_params = _delegate_params(payload)
    params = {
        "task": task,
        "tools": list(payload.get("tools") if isinstance(payload.get("tools"), list) else []),
        "model": str(payload.get("model") or model or ""),
        "system_prompt": payload.get("system_prompt"),
        "runtime_profile_key": payload.get("runtime_profile_key"),
        "capability_profile": payload.get("capability_profile"),
        "required_capabilities": payload.get("required_capabilities") or payload.get("capability"),
        "params": delegate_params,
    }
    payload_profile_id = str(payload.get("profile_id") or "").strip()
    if payload_profile_id:
        params.setdefault("profile_id", payload_profile_id)
    for key in _DELEGATE_CONTEXT_KEYS:
        if key in dispatch_context and dispatch_context.get(key) not in ("", None, [], {}):
            params.setdefault(key, dispatch_context.get(key))
    if "timeout_seconds" in payload:
        params["timeout_seconds"] = payload.get("timeout_seconds")
    metadata = {"compatibility_alias": "subagent", "role_id": role_id}
    if payload_profile_id:
        metadata["profile_id"] = payload_profile_id
    for key in ("profile_id", "company_id"):
        if dispatch_context.get(key):
            metadata[key] = dispatch_context.get(key)
    result = dispatch_input(
        RumiInputEnvelope(
            role="user",
            input=task,
            chat={},
            source={"type": "compatibility", "provider": "subagent"},
            target=_delegate_target(payload, dispatch_context),
            delivery={"action_id": "agent.delegate"},
            attachments=list(payload.get("attachments") if isinstance(payload.get("attachments"), list) else []),
            metadata=metadata,
            params=params,
            tools=list(payload.get("tools") if isinstance(payload.get("tools"), list) else []),
        ),
        dispatch_context,
    )
    if isinstance(result, dict):
        assistant_text = extract_assistant_text_from_result(result)
        if assistant_text:
            result["assistant_text"] = assistant_text
        result.setdefault("compatibility_alias", "subagent")
        result.setdefault("route_kind", "agent.delegate")
    return result


def _delegate_dispatch_context(payload: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(context or {}) if isinstance(context, dict) else {}
    sources = [
        payload.get("params") if isinstance(payload.get("params"), dict) else {},
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        payload,
    ]
    for source in sources:
        for key in _DELEGATE_CONTEXT_KEYS:
            value = source.get(key) if isinstance(source, dict) else None
            if value in ("", None, [], {}):
                continue
            updated.setdefault(key, value)
    for key in _TRUSTED_AUTHORITY_KEYS:
        if isinstance(context, dict) and context.get(key) not in ("", None, [], {}):
            updated[key] = context.get(key)
    _synthesize_trusted_profile_principal(updated, context)
    return updated


def _trusted_model_context(context: dict[str, Any] | None) -> dict[str, Any]:
    updated = dict(context or {}) if isinstance(context, dict) else {}
    _synthesize_trusted_profile_principal(updated, context)
    return updated


def _synthesize_trusted_profile_principal(updated: dict[str, Any], context: dict[str, Any] | None) -> None:
    if not isinstance(context, dict):
        return
    profile_id = str(context.get("profile_id") or "").strip()
    principal_id = str(context.get("principal_id") or context.get("authority_principal_id") or "").strip()
    if principal_id:
        updated.setdefault("principal_id", principal_id)
    if profile_id and not principal_id:
        updated["principal_id"] = "profile:" + profile_id


def _delegate_target(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {}
    conversation_id = str(
        payload.get("conversation_id")
        or payload.get("target_conversation_id")
        or context.get("conversation_id")
        or ""
    ).strip()
    if conversation_id:
        target["conversation_id"] = conversation_id
    return target


def _delegate_params(payload: dict[str, Any]) -> dict[str, Any]:
    params = dict(payload.get("params") if isinstance(payload.get("params"), dict) else {})
    for source_key, target_key in (
        ("output_dir", "output_dir"),
        ("outputDir", "output_dir"),
        ("allowed_paths", "allowed_paths"),
        ("allowedPaths", "allowed_paths"),
        ("metadata", "metadata"),
    ):
        if source_key in payload and target_key not in params:
            params[target_key] = payload[source_key]
    if "output_dir" in params or "allowed_paths" in params:
        params.setdefault(
            "workspace_write_contract",
            {
                "output_dir": params.get("output_dir"),
                "allowed_paths": list(params.get("allowed_paths") if isinstance(params.get("allowed_paths"), list) else []),
                "mode": "create-from-empty-directory",
            },
        )
    return params
