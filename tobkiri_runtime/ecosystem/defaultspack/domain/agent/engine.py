import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import gen_id, timestamp
from domain.agent.execution import AgentExecution
from domain.agent.step import AgentStep
from domain.agent_runtime.policy import session_key_for
from domain.agent_runtime.run_store import AgentRunStore
from domain.agent_runtime.transcript import TranscriptStore
from domain.agent.placement_catalog import (
    compatibility_effective_plan,
    runtime_assignment_for_plan,
    verify_effective_plan,
)
from domain.ai_client.capability_tokens import (
    missing_model_capabilities,
    model_requirements_from_tokens,
    normalize_capability_tokens,
)
from domain.ai_client.model_router import ModelRoutingRequest, route_model_request
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.ai_client.model_search import get_model_capabilities
from domain.capabilities.runtime_snapshot import build_runtime_capability_snapshot
from domain.tool.eligibility import filter_tool_definitions_by_eligibility
from domain.tool.schema_adapter import (
    adapt_tool_definitions,
    build_tool_execution_context,
    connected_tool_names,
    filter_tool_definitions_for_runtime_profile,
    max_tool_calls,
    policy_from_context,
    resolve_runtime_profile_context,
    runtime_profile_enforced_tool_names,
    tool_name_from_definition,
)
from domain.chat.loop_guard import emergency_budget_from_context, explicit_param_max_tool_calls


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _authority_approval_from_ai_result(ai_result):
    if not isinstance(ai_result, dict):
        return None
    status = str(ai_result.get("status") or ai_result.get("finish_reason") or "").strip().lower()
    code = str(ai_result.get("code") or "").strip().lower()
    error_payload = ai_result.get("error") if isinstance(ai_result.get("error"), dict) else {}
    details = error_payload.get("details") if isinstance(error_payload.get("details"), dict) else {}
    if not details and isinstance(ai_result.get("authority"), dict):
        details = ai_result.get("authority")
    detail_status = str(details.get("status") or details.get("finish_reason") or "").strip().lower()
    error_code = str(error_payload.get("code") or "").strip().lower()
    if (
        status != "authority_approval_required"
        and code != "authority_approval_required"
        and error_code != "authority_approval_required"
        and detail_status != "authority_approval_required"
    ):
        return None
    approval = dict(details)
    approval.setdefault("status", "authority_approval_required")
    approval.setdefault("approval_required", True)
    approval.setdefault("requires_approval", True)
    approval.setdefault("finish_reason", "authority_approval_required")
    message = str(
        approval.get("message")
        or approval.get("display_summary")
        or approval.get("reason")
        or error_payload.get("message")
        or ai_result.get("message")
        or ai_result.get("error")
        or "Authority approval required"
    )
    approval.setdefault("message", message)
    approval.setdefault("error", message)
    approval.setdefault("code", "authority_approval_required")
    return approval


def _attachment_modalities(attachments):
    items = attachments if isinstance(attachments, list) else []
    has_images = False
    has_files = False
    for attachment in items:
        if not isinstance(attachment, dict):
            continue
        mime = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
        if mime.startswith("image/") or str(attachment.get("dataUrl") or attachment.get("data_url") or "").startswith("data:image/"):
            has_images = True
        else:
            has_files = True
    return {"has_images": has_images, "has_files": has_files}


def _message_content_with_attachments(task, attachments):
    items = attachments if isinstance(attachments, list) else []
    if not items:
        return task
    content = []
    if str(task or ""):
        content.append({"type": "text", "text": str(task or "")})
    for attachment in items:
        if not isinstance(attachment, dict):
            continue
        mime = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
        data_url = attachment.get("dataUrl") or attachment.get("data_url")
        if mime.startswith("image/") and isinstance(data_url, str) and data_url.startswith("data:image/"):
            content.append({"type": "image_url", "image_url": {"url": data_url}})
            continue
        label = str(attachment.get("name") or mime or "file")
        content.append({"type": "text", "text": "[attachment] " + label})
    return content or str(task or "")


def _text_from_content_blocks(content):
    if not isinstance(content, list):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif isinstance(block, str) and block.strip():
            parts.append(block.strip())
    return "\n\n".join(parts) if parts else content


def _effective_plan_role_instructions(plan):
    behavior = plan.get("behavior") if isinstance(plan, dict) else {}
    layers = behavior.get("layers") if isinstance(behavior, dict) else []
    for layer in reversed(layers if isinstance(layers, list) else []):
        if not isinstance(layer, dict):
            continue
        if layer.get("kind") != "placement_role":
            continue
        value = layer.get("value")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            instructions = value.get("instructions")
            if isinstance(instructions, str):
                return instructions
    return None


def _bind_effective_subagent_plan(
    *,
    execution_id,
    context,
    model,
    tools,
    system_prompt,
):
    plan = context.get("effective_subagent_plan")
    if isinstance(plan, dict):
        verify_effective_plan(plan)
        expected_hash = str(context.get("effective_plan_hash") or "").strip()
        if expected_hash and expected_hash != str(plan.get("plan_hash") or ""):
            raise ValueError("Effective Subagent Plan identity does not match")
    else:
        agent_id = str(context.get("agent_id") or "delegate").strip()
        plan = compatibility_effective_plan(
            agent_id=agent_id,
            model=str(model or "default"),
            tools=tools or [],
            system_prompt=system_prompt,
            host_policy=(
                context.get("profile_policy")
                if isinstance(context.get("profile_policy"), dict)
                else {}
            ),
        )
    plan_model = plan.get("model") if isinstance(plan.get("model"), dict) else {}
    selected_model = str(plan_model.get("model_id") or model or "default")
    tool_bindings = (
        plan.get("tool_bindings")
        if isinstance(plan.get("tool_bindings"), dict)
        else {}
    )
    allowed_tool_ids = {
        str(value).strip()
        for value in tool_bindings.get("allow_tool_ids", [])
        if str(value).strip()
    }
    denied_tool_ids = {
        str(value).strip()
        for value in tool_bindings.get("deny_tool_ids", [])
        if str(value).strip()
    }
    bounded_tools = []
    for tool in tools or []:
        tool_id = tool_name_from_definition(tool)
        if not tool_id and isinstance(tool, str):
            tool_id = tool.strip()
        if not tool_id or tool_id in denied_tool_ids:
            continue
        if allowed_tool_ids and tool_id not in allowed_tool_ids:
            continue
        bounded_tools.append(tool)
    placement = (
        plan.get("placement") if isinstance(plan.get("placement"), dict) else {}
    )
    context.update(
        {
            "agent_kind": str(plan.get("agent_kind") or "subagent"),
            "runtime_kind": str(plan.get("runtime_kind") or "agent_run"),
            "placement_id": str(placement.get("id") or ""),
            "placement_revision": str(placement.get("revision") or ""),
            "placement_map_id": str(placement.get("map_id") or ""),
            "protocol_membership": [
                value.get("protocol_ref")
                for value in plan.get("protocol_bindings", [])
                if isinstance(value, dict) and value.get("protocol_ref")
            ],
            "effective_subagent_plan": plan,
            "effective_plan_hash": str(plan["plan_hash"]),
            "root_scope_id": str(
                context.get("root_scope_id")
                or context.get("root_run_id")
                or execution_id
            ),
        }
    )
    context["runtime_assignment"] = runtime_assignment_for_plan(
        plan,
        run_id=execution_id,
        root_scope_id=str(context["root_scope_id"]),
        parent_run_id=(
            str(context.get("parent_run_id"))
            if context.get("parent_run_id")
            else None
        ),
        root_run_id=(
            str(context.get("root_run_id"))
            if context.get("root_run_id")
            else execution_id
        ),
    )
    return (
        selected_model,
        bounded_tools,
        _effective_plan_role_instructions(plan) or system_prompt,
    )


def _route_agent_model(
    *,
    task,
    model,
    tools,
    params,
    required_capabilities,
    modalities,
    context,
):
    model_requirements = model_requirements_from_tokens(required_capabilities)
    thinking_level = str((params or {}).get("thinking_level") or (params or {}).get("requested_thinking_level") or "").strip()
    requested_tool_names = [
        name for name in (tool_name_from_definition(tool) for tool in tools or []) if name
    ]
    has_tools = bool(requested_tool_names)
    route_needed = bool(
        any(model_requirements.values())
        or has_tools
        or modalities.get("has_images")
        or modalities.get("has_files")
        or thinking_level not in {"", "none"}
    )
    if not route_needed:
        return model if model else "default"
    settings = ModelRuntimeSettingsService().get_settings()
    preferred_model = str(
        model
        if model and model != "default"
        else settings.get("preferred_model") or model or "stub/default"
    ).strip() or "stub/default"
    decision = route_model_request(
        ModelRoutingRequest(
            user_text=str(task or ""),
            has_images=bool(modalities.get("has_images") or model_requirements["image_input"]),
            has_files=bool(modalities.get("has_files")),
            requested_tools=requested_tool_names,
            requires_tool_calling=bool(model_requirements["tool_calling"] or has_tools),
            requires_fast=bool(model_requirements["fast"]),
            requested_thinking_level=thinking_level or ("medium" if model_requirements["thinking"] else "none"),
            preferred_model=preferred_model,
            preferred_group=str(settings.get("preferred_model_group") or "default"),
            auto_route_within_group=bool(settings.get("auto_route_within_group", True)),
            task_hints={
                **((params or {}).get("task_hints") if isinstance((params or {}).get("task_hints"), dict) else {}),
                "modalities": modalities,
                "required_capabilities": list(required_capabilities or []),
            },
            settings=settings,
        )
    )
    if isinstance(context, dict):
        context["model_routing"] = decision.to_dict()
    return decision.selected_model or preferred_model


class AgentEngine:
    def __init__(self):
        self._executions = {}
        self._run_store = AgentRunStore()
        self._transcripts = TranscriptStore()

    def _create_transcript(self, execution_id, context, metadata=None):
        if not isinstance(context, dict):
            return None
        if context.get("transcript_id"):
            return context.get("transcript_id")
        transcript_id = self._transcripts.create(
            execution_id,
            metadata=metadata or {},
        )
        context["transcript_id"] = transcript_id
        return transcript_id

    def _append_transcript_event(self, execution, event_type, payload=None):
        context = getattr(execution, "context", {}) or {}
        transcript_id = context.get("transcript_id")
        if not transcript_id:
            return
        try:
            self._transcripts.append(
                transcript_id,
                event_type,
                payload or {
                    "execution_id": execution.execution_id,
                    "status": execution.status,
                    "current_step": execution.current_step,
                },
            )
        except Exception:
            pass

    def _persist_execution(self, execution, event_type="run_step", payload=None):
        try:
            if self._durably_cancelled(execution.execution_id) and execution.status != "cancelled":
                execution.status = "cancelled"
                execution.pending_tool_call = None
                execution.updated_at = timestamp()
                return
            context = getattr(execution, "context", {}) or {}
            session_key = session_key_for(context, agent_id=context.get("agent_id"))
            self._run_store.save_execution(
                execution,
                session_key=session_key,
                transcript_id=context.get("transcript_id"),
            )
            self._append_transcript_event(execution, event_type, payload)
        except Exception:
            pass

    def _touch_execution(self, execution, event_type, payload=None):
        try:
            self._run_store.touch(
                execution.execution_id,
                status=getattr(execution, "status", None),
                event_type=event_type,
                payload=payload or {"status": getattr(execution, "status", None)},
            )
        except Exception:
            pass

    def _durably_cancelled(self, execution_id):
        try:
            run = self._run_store.get_run(execution_id)
            return isinstance(run, dict) and run.get("status") == "cancelled"
        except Exception:
            return False

    def _context_cancelled(self, execution):
        checker = (getattr(execution, "context", {}) or {}).get("is_cancelled")
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    def _is_cancelled(self, execution):
        return (
            execution.status == "cancelled"
            or self._context_cancelled(execution)
            or self._durably_cancelled(execution.execution_id)
        )

    def _cancelled_result(self, execution):
        execution.status = "cancelled"
        execution.pending_tool_call = None
        execution.updated_at = timestamp()
        self._persist_execution(execution, "run_completed", {"status": "cancelled"})
        return {"execution_id": execution.execution_id, "status": "cancelled", "result": execution.to_dict()}

    def _execution_from_store(self, execution_id):
        data = self._run_store.load_execution_dict(execution_id)
        if not isinstance(data, dict):
            return None
        execution = AgentExecution(
            execution_id=data.get("execution_id", execution_id),
            task=data.get("task", ""),
            tools=data.get("tools", []),
            model=data.get("model", "default"),
            system_prompt=data.get("system_prompt"),
        )
        execution.status = data.get("status", "created")
        execution.result = data.get("result")
        execution.error = data.get("error")
        execution.messages = data.get("messages", []) if isinstance(data.get("messages"), list) else []
        execution.pending_tool_call = data.get("pending_tool_call")
        execution.queued_tool_calls = data.get("queued_tool_calls", [])
        execution.created_at = data.get("created_at", execution.created_at)
        execution.updated_at = data.get("updated_at", execution.updated_at)
        execution.steps = []
        for item in data.get("steps", []) if isinstance(data.get("steps"), list) else []:
            if not isinstance(item, dict):
                continue
            step = AgentStep(
                step_id=item.get("step_id", gen_id("step_")),
                step_number=item.get("step_number", len(execution.steps) + 1),
                step_type=item.get("step_type", "unknown"),
                content=item.get("content", {}),
                created_at=item.get("created_at", timestamp()),
            )
            step.status = item.get("status", "completed")
            execution.steps.append(step)
        execution.current_step = data.get("current_step", len(execution.steps))
        execution.context = data.get("context", {}) if isinstance(data.get("context"), dict) else {}
        self._executions[execution_id] = execution
        return execution

    def _get_execution(self, execution_id):
        return self._executions.get(execution_id) or self._execution_from_store(execution_id)

    def _get_instruction_queue(self):
        try:
            from blocks.agent._state import get_instruction_queue
            return get_instruction_queue()
        except Exception:
            return None

    def _inject_pending_instructions(self, execution):
        queue = self._get_instruction_queue()
        if queue is None:
            return False
        if not queue.has_pending(execution.execution_id):
            return False
        pending = queue.get_pending(execution.execution_id)
        if not pending:
            return False
        parts = []
        has_urgent = False
        for instr in pending:
            priority = str(instr.get("priority") or "")
            prefix = "[URGENT] " if priority in {"urgent", "high"} else ""
            parts.append(prefix + instr["instruction"])
            if priority in {"urgent", "high"}:
                has_urgent = True
        if len(parts) == 1:
            combined = parts[0]
        else:
            combined = "\n".join(
                "- " + p for p in parts
            )
        header = (
            "[RUNTIME INSTRUCTION — URGENT: Override current approach] "
            if has_urgent
            else "[RUNTIME INSTRUCTION — Additional guidance from user] "
        )
        message_content = header + combined
        execution.messages.append({"role": "user", "content": message_content})
        execution.add_step("instruction_injected", {
            "count": len(pending),
            "has_urgent": has_urgent,
            "instructions": [
                {"id": i["id"], "priority": i["priority"], "instruction": i["instruction"]}
                for i in pending
            ],
        })
        return True

    def _process_conversation_steer(self, execution):
        try:
            from domain.chat.steer import ConversationSteerStore

            context = dict(getattr(execution, "context", {}) or {})
            conversation_id = str(context.get("conversation_id") or "")
            processed = ConversationSteerStore().process_for_agent_run(
                execution.execution_id,
                conversation_id=conversation_id,
                context=context,
            )
            if processed:
                execution.add_step("conversation_steer", {
                    "processed": len(processed),
                    "items": [
                        {"id": item.get("id"), "status": item.get("status")}
                        for item in processed
                        if isinstance(item, dict)
                    ],
                })
            return processed
        except Exception as exc:
            execution.add_step("conversation_steer_error", {"error": str(exc)})
            return []

    def _ai_complete(self, messages, model, context, tools=None):
        from blocks.ai.complete import run as ai_complete_run
        params = context.get("params") if isinstance(context, dict) and isinstance(context.get("params"), dict) else {}
        result = ai_complete_run({"messages": messages, "model": model, "tools": tools or [], "params": params}, context)
        return result

    def _execute_tool(self, tool_name, tool_args, context):
        from domain.tool_policy.orchestrator import ToolOrchestrator
        from domain.tool_policy.internal_context import mark_trusted_profile_policy_context

        trusted_context = mark_trusted_profile_policy_context(dict(context or {}))
        return ToolOrchestrator().run(tool_name, tool_args, trusted_context)

    def _tool_name_from_definition(self, tool):
        return tool_name_from_definition(tool)

    def _connected_tool_names(self, execution):
        runtime_profile = getattr(execution, "context", {}).get("runtime_profile")
        agent_id = getattr(execution, "context", {}).get("agent_id")
        return connected_tool_names(execution.tools, runtime_profile, agent_id)

    def _enforced_tool_names(self, execution):
        context = getattr(execution, "context", {}) or {}
        return runtime_profile_enforced_tool_names(
            context.get("runtime_profile"),
            context.get("agent_id"),
            execution.tools,
        )

    def _tool_call_count(self, execution):
        return sum(1 for step in execution.steps if step.step_type == "tool_result")

    def _normalize_tool_args(self, tool_args):
        if isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
                return parsed if isinstance(parsed, dict) else {"value": parsed}
            except (TypeError, ValueError):
                return {"value": tool_args}
        if isinstance(tool_args, dict):
            return tool_args
        return {}

    def _normalize_tool_call(self, raw_call):
        if not isinstance(raw_call, dict):
            return None
        function_def = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
        tool_name = (
            raw_call.get("name")
            or raw_call.get("tool_name")
            or function_def.get("name")
            or "unknown"
        )
        tool_args = (
            raw_call.get("args")
            if "args" in raw_call
            else raw_call.get("input", function_def.get("arguments", {}))
        )
        return {
            "type": "tool_call",
            "tool_name": str(tool_name),
            "tool_args": self._normalize_tool_args(tool_args),
            "raw": raw_call,
        }

    def _reject_unconnected_tool_call(self, execution, parsed):
        connected_tools = self._connected_tool_names(execution)
        enforced_tools = self._enforced_tool_names(execution)
        tool_name = parsed.get("tool_name", "")
        allowed_tools = enforced_tools if enforced_tools is not None else connected_tools
        if tool_name in allowed_tools:
            return False
        execution.status = "error"
        execution.error = "tool call is not connected to this agent: " + tool_name
        execution.add_step("error", {
            "error": execution.error,
            "tool_name": tool_name,
            "connected_tools": sorted(connected_tools),
            "enforced_tools": sorted(enforced_tools) if enforced_tools is not None else None,
        })
        return True

    def _reject_policy_violation(self, execution, parsed):
        context = getattr(execution, "context", {}) or {}
        limit = max_tool_calls(context)
        if limit is None:
            params = context.get("params") if isinstance(context, dict) and isinstance(context.get("params"), dict) else {}
            limit = explicit_param_max_tool_calls(params)
        if limit is None or self._tool_call_count(execution) < limit:
            return False
        tool_name = parsed.get("tool_name", "")
        execution.status = "error"
        execution.error = "max tool calls exceeded"
        execution.add_step("error", {
            "error": execution.error,
            "tool_name": tool_name,
            "max_tool_calls": limit,
        })
        return True

    def _set_pending_tool_call(self, execution, parsed):
        queued = parsed.get("tool_calls", []) if isinstance(parsed.get("tool_calls"), list) else []
        execution.queued_tool_calls = queued[1:] if queued and queued[0].get("raw") == parsed.get("raw") else queued
        execution.status = "waiting_approval"
        raw = parsed.get("raw", {}) if isinstance(parsed.get("raw"), dict) else {}
        tool_call_id = str(
            raw.get("id")
            or raw.get("tool_call_id")
            or parsed.get("tool_call_id")
            or gen_id("call_")
        )
        approval_id = gen_id("approval_")
        execution.pending_tool_call = {
            "id": tool_call_id,
            "tool_call_id": tool_call_id,
            "approval_id": approval_id,
            "tool_name": parsed["tool_name"],
            "tool_args": self._normalize_tool_args(parsed["tool_args"]),
            "raw": raw,
        }
        execution.add_step("tool_call", {
            "tool_name": parsed["tool_name"],
            "tool_args": execution.pending_tool_call["tool_args"],
        })
        step = execution.steps[-1]
        step.status = "pending"

    def _auto_approval_enabled(self, execution):
        context = getattr(execution, "context", {}) or {}
        if not isinstance(context, dict):
            return False
        return _truthy(policy_from_context(context).get("yolo_mode"))

    def _auto_approve_pending_tool_call(self, execution):
        if not execution.pending_tool_call:
            return None
        if self._auto_approval_enabled(execution):
            self._persist_execution(
                execution,
                "tool_auto_approved",
                execution.pending_tool_call,
            )
            return self.approve(execution.execution_id, source="agent.full_access")
        context = getattr(execution, "context", {}) or {}
        from domain.tool.approval_reviewer import (
            delegated_approval_requested,
            review_tool_action,
        )

        if not delegated_approval_requested(context):
            return None
        pending = execution.pending_tool_call
        tool_name = str(pending.get("tool_name") or "")
        tool_def = next(
            (
                item
                for item in execution.tools
                if isinstance(item, dict)
                and self._tool_name_from_definition(item) == tool_name
            ),
            {},
        )
        review = review_tool_action(
            tool_name,
            tool_def,
            pending.get("tool_args")
            if isinstance(pending.get("tool_args"), dict)
            else {},
            context,
        )
        pending["delegated_review"] = review
        self._persist_execution(execution, "tool_delegated_reviewed", pending)
        if review.get("decision") == "approve":
            return self.approve(
                execution.execution_id,
                source="agent.approval_reviewer",
            )
        if review.get("decision") == "deny":
            return self.reject(
                execution.execution_id,
                str(review.get("reason") or "Denied by delegated reviewer"),
            )
        return None

    def _authority_approval_result(self, execution, parsed):
        approval = dict(parsed.get("content") if isinstance(parsed.get("content"), dict) else {})
        message = str(
            approval.get("message")
            or approval.get("display_summary")
            or approval.get("reason")
            or "Authority approval required"
        )
        execution.status = "authority_approval_required"
        execution.error = message
        execution.add_step("authority_approval_required", approval)
        self._persist_execution(execution, "approval_requested", approval)
        return {
            "execution_id": execution.execution_id,
            "status": "authority_approval_required",
            "approval_required": True,
            "requires_approval": True,
            "finish_reason": "authority_approval_required",
            "result": execution.to_dict(),
            "authority": approval,
        }

    def _parse_ai_response(self, ai_result):
        if ai_result.get("status") != "ok":
            authority_approval = _authority_approval_from_ai_result(ai_result)
            if authority_approval is not None:
                return {
                    "type": "authority_approval_required",
                    "content": authority_approval,
                }
            return {
                "type": "error",
                "content": ai_result.get("error", "AI call failed"),
            }
        data = ai_result.get("data", {})
        parsed_calls = []
        if isinstance(data, dict) and data.get("tool_calls"):
            tool_calls = data["tool_calls"]
            raw_calls = tool_calls if isinstance(tool_calls, list) else [tool_calls]
            parsed_calls.extend(
                call for call in (self._normalize_tool_call(raw) for raw in raw_calls) if call
            )
        if isinstance(data, dict) and isinstance(data.get("content"), list):
            for part in data["content"]:
                if not isinstance(part, dict) or part.get("type") not in {"tool_use", "tool_call"}:
                    continue
                normalized = self._normalize_tool_call(part)
                if normalized:
                    parsed_calls.append(normalized)
        if parsed_calls:
            first = dict(parsed_calls[0])
            first["tool_calls"] = parsed_calls
            return first
        content = ""
        if isinstance(data, dict):
            content = data.get("content", data.get("text", str(data)))
            content = _text_from_content_blocks(content)
        elif isinstance(data, str):
            content = data
        else:
            content = str(data)
        return {"type": "text", "content": content}

    def _promote_queued_tool_call(self, execution):
        if not execution.queued_tool_calls:
            return False
        parsed = execution.queued_tool_calls.pop(0)
        remaining = list(execution.queued_tool_calls)
        if self._reject_unconnected_tool_call(execution, parsed) or self._reject_policy_violation(execution, parsed):
            return True
        self._set_pending_tool_call(execution, parsed)
        execution.queued_tool_calls = remaining
        return True

    def _build_initial_messages(self, execution):
        messages = []
        if execution.system_prompt:
            messages.append({"role": "system", "content": execution.system_prompt})
        attachments = execution.context.get("attachments") if isinstance(getattr(execution, "context", None), dict) else []
        messages.append({"role": "user", "content": _message_content_with_attachments(execution.task, attachments)})
        return messages

    def execute(self, task, tools, model, system_prompt, context):
        execution_id = gen_id("agent_")
        execution_context = dict(context or {}) if isinstance(context, dict) else {}
        execution_context = resolve_runtime_profile_context(execution_context)
        try:
            model, tools, system_prompt = _bind_effective_subagent_plan(
                execution_id=execution_id,
                context=execution_context,
                model=model,
                tools=tools,
                system_prompt=system_prompt,
            )
        except ValueError as exc:
            execution = AgentExecution(
                execution_id=execution_id,
                task=task,
                tools=[],
                model=str(model or "default"),
                system_prompt=system_prompt,
            )
            execution.context = execution_context
            execution.status = "error"
            execution.error = str(exc)
            execution.add_step(
                "error",
                {
                    "code": "EFFECTIVE_SUBAGENT_PLAN_INVALID",
                    "error": str(exc),
                },
            )
            self._persist_execution(
                execution,
                "run_failed",
                {"error": execution.error},
            )
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": execution.to_dict(),
            }
        required_capabilities = normalize_capability_tokens(execution_context.get("required_capabilities"))
        if required_capabilities:
            execution_context["required_capabilities"] = required_capabilities
        params = dict(execution_context.get("params") if isinstance(execution_context.get("params"), dict) else {})
        execution_context["params"] = params
        attachments = list(execution_context.get("attachments") if isinstance(execution_context.get("attachments"), list) else [])
        modalities = _attachment_modalities(attachments)
        normalized_tools = adapt_tool_definitions(tools if tools else [])
        provider_tools = filter_tool_definitions_for_runtime_profile(
            normalized_tools,
            execution_context.get("runtime_profile"),
            execution_context.get("agent_id"),
        )
        policy = policy_from_context(execution_context)
        model = _route_agent_model(
            task=task,
            model=model,
            tools=provider_tools,
            params=params,
            required_capabilities=required_capabilities,
            modalities=modalities,
            context=execution_context,
        )
        selected_capabilities = get_model_capabilities(model if model else "default") or {}
        missing_capabilities = missing_model_capabilities(required_capabilities, selected_capabilities)
        if missing_capabilities:
            execution = AgentExecution(
                execution_id=execution_id,
                task=task,
                tools=provider_tools,
                model=model if model else "default",
                system_prompt=system_prompt,
            )
            execution.context = execution_context
            execution.status = "error"
            execution.error = "selected model does not satisfy required capabilities: " + ", ".join(missing_capabilities)
            execution.add_step(
                "error",
                {
                    "code": "MODEL_CAPABILITY_UNSATISFIED",
                    "missing_capabilities": missing_capabilities,
                    "required_capabilities": required_capabilities,
                    "model": model,
                },
            )
            self._persist_execution(execution, "run_failed", {"error": execution.error})
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": execution.to_dict(),
            }
        runtime_snapshot = build_runtime_capability_snapshot(
            user_text=str(task or ""),
            modalities=modalities,
            model_capabilities=selected_capabilities,
            context=execution_context,
            policy=policy,
        )
        eligibility = filter_tool_definitions_by_eligibility(
            provider_tools,
            runtime_snapshot,
            policy=policy,
            connected_tool_names=connected_tool_names(
                provider_tools,
                execution_context.get("runtime_profile"),
                agent_id=execution_context.get("agent_id"),
            ),
        )
        provider_tools = list(eligibility.get("allowed_tools") or [])
        execution = AgentExecution(
            execution_id=execution_id,
            task=task,
            tools=provider_tools,
            model=model if model else "default",
            system_prompt=system_prompt,
        )
        execution.context = execution_context
        execution.context["tool_filter_result"] = list(eligibility.get("entries") or [])
        execution.context["runtime_capability_snapshot"] = runtime_snapshot.as_dict()
        self._create_transcript(execution_id, execution.context, {"task": task, "model": model})
        self._executions[execution_id] = execution
        execution.status = "running"
        execution.messages = self._build_initial_messages(execution)
        for message in execution.messages:
            self._transcripts.append_message(execution.context["transcript_id"], message)
        execution.add_step("think", {"action": "start", "task": task})
        self._persist_execution(execution, "run_started", {"task": task})
        self._inject_pending_instructions(execution)
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        self._touch_execution(execution, "model_call_started", {"phase": "execute"})
        ai_result = self._ai_complete(execution.messages, execution.model, execution.context, execution.tools)
        self._touch_execution(execution, "model_call_completed", {"phase": "execute"})
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        parsed = self._parse_ai_response(ai_result)
        if parsed["type"] == "authority_approval_required":
            return self._authority_approval_result(execution, parsed)
        if parsed["type"] == "error":
            execution.status = "error"
            execution.error = parsed["content"]
            execution.add_step("error", {"error": parsed["content"]})
            self._persist_execution(execution, "run_failed", {"error": parsed["content"]})
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": execution.to_dict(),
            }
        if parsed["type"] == "tool_call":
            if self._reject_unconnected_tool_call(execution, parsed) or self._reject_policy_violation(execution, parsed):
                self._persist_execution(execution, "run_failed", {"error": execution.error})
                return {
                    "execution_id": execution_id,
                    "status": "error",
                    "result": execution.to_dict(),
                }
            self._set_pending_tool_call(execution, parsed)
            self._transcripts.append_tool_call(
                execution.context["transcript_id"],
                execution.pending_tool_call,
            )
            auto_result = self._auto_approve_pending_tool_call(execution)
            if auto_result is not None:
                return auto_result
            self._persist_execution(execution, "approval_requested", execution.pending_tool_call)
            return {
                "execution_id": execution_id,
                "status": "waiting_approval",
                "result": execution.to_dict(),
            }
        execution.status = "completed"
        execution.result = parsed["content"]
        execution.messages.append({"role": "assistant", "content": parsed["content"]})
        self._transcripts.append_message(execution.context["transcript_id"], execution.messages[-1])
        execution.add_step("response", {"content": parsed["content"]})
        self._process_conversation_steer(execution)
        self._persist_execution(execution, "run_completed", {"result": parsed["content"]})
        return {
            "execution_id": execution_id,
            "status": "completed",
            "result": execution.to_dict(),
        }

    def approve(self, execution_id, source="agent.approve"):
        execution = self._get_execution(execution_id)
        if not execution:
            return {"execution_id": execution_id, "status": "error", "result": {"error": "execution not found"}}
        if execution.status != "waiting_approval":
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": {"error": "execution is not waiting for approval, current status: " + execution.status},
            }
        pending = execution.pending_tool_call
        if not pending:
            return {"execution_id": execution_id, "status": "error", "result": {"error": "no pending tool call"}}
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        execution.status = "running"
        execution.pending_tool_call = None
        tool_call_id = pending.get("tool_call_id") or pending.get("id") or gen_id("call_")
        approval_id = pending.get("approval_id") or f"approval_{tool_call_id}"
        self._run_store.record_approval(
            str(approval_id),
            execution.execution_id,
            str(tool_call_id),
            status="approved",
            decision={"source": source},
        )
        context_for_tool = dict(getattr(execution, "context", {}) or {})
        context_for_tool["agent_run_id"] = execution.execution_id
        context_for_tool["tool_call_id"] = str(tool_call_id)
        context_for_tool["approval_id"] = str(approval_id)
        context_for_tool["profile_policy"] = policy_from_context(context_for_tool)
        context_for_tool = build_tool_execution_context(
            context_for_tool,
            pending["tool_name"],
            self._connected_tool_names(execution),
        )
        self._touch_execution(execution, "tool_call_started", {"tool_name": pending["tool_name"]})
        tool_result = self._execute_tool(pending["tool_name"], pending["tool_args"], context_for_tool)
        self._touch_execution(execution, "tool_call_completed", {"tool_name": pending["tool_name"]})
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        tool_content = ""
        if isinstance(tool_result, dict):
            tool_content = tool_result.get("data", tool_result.get("error", str(tool_result)))
        else:
            tool_content = str(tool_result)
        execution.add_step("tool_result", {
            "tool_name": pending["tool_name"],
            "result": tool_content,
        })
        execution.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [pending.get("raw", {"name": pending["tool_name"], "args": pending["tool_args"]})],
        })
        execution.messages.append({
            "role": "tool",
            "content": str(tool_content) if not isinstance(tool_content, str) else tool_content,
            "name": pending["tool_name"],
            "tool_call_id": str(tool_call_id),
        })
        self._transcripts.append_tool_result(
            execution.context["transcript_id"],
            {"tool_name": pending["tool_name"], "result": tool_content},
        )
        self._persist_execution(execution, "tool_completed", {"tool_name": pending["tool_name"]})
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        if self._promote_queued_tool_call(execution):
            auto_result = self._auto_approve_pending_tool_call(execution)
            if auto_result is not None:
                return auto_result
            self._persist_execution(execution, "approval_requested", execution.pending_tool_call or {})
            return {
                "execution_id": execution_id,
                "status": execution.status,
                "result": execution.to_dict(),
            }
        tool_executions = sum(1 for s in execution.steps if s.step_type == "tool_result")
        emergency_budget = emergency_budget_from_context(getattr(execution, "context", {}) or {})
        if tool_executions >= emergency_budget.max_tool_executions:
            execution.status = "paused_emergency"
            execution.error = "operator emergency tool execution budget reached"
            execution.add_step(
                "pause",
                {
                    "reason": "max_tool_executions",
                    "tool_executions": tool_executions,
                    "max_tool_executions": emergency_budget.max_tool_executions,
                    "resumable": True,
                },
            )
            self._persist_execution(execution, "run_paused_emergency", {"error": execution.error})
            return {
                "execution_id": execution_id,
                "status": execution.status,
                "result": execution.to_dict(),
            }
        self._inject_pending_instructions(execution)
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        self._touch_execution(execution, "model_call_started", {"phase": "approve"})
        ai_result = self._ai_complete(execution.messages, execution.model, context_for_tool, execution.tools)
        self._touch_execution(execution, "model_call_completed", {"phase": "approve"})
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        parsed = self._parse_ai_response(ai_result)
        if parsed["type"] == "authority_approval_required":
            return self._authority_approval_result(execution, parsed)
        if parsed["type"] == "error":
            execution.status = "error"
            execution.error = parsed["content"]
            execution.add_step("error", {"error": parsed["content"]})
            self._persist_execution(execution, "run_failed", {"error": parsed["content"]})
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": execution.to_dict(),
            }
        if parsed["type"] == "tool_call":
            if self._reject_unconnected_tool_call(execution, parsed) or self._reject_policy_violation(execution, parsed):
                self._persist_execution(execution, "run_failed", {"error": execution.error})
                return {
                    "execution_id": execution_id,
                    "status": "error",
                    "result": execution.to_dict(),
                }
            self._set_pending_tool_call(execution, parsed)
            self._transcripts.append_tool_call(
                execution.context["transcript_id"],
                execution.pending_tool_call,
            )
            auto_result = self._auto_approve_pending_tool_call(execution)
            if auto_result is not None:
                return auto_result
            self._persist_execution(execution, "approval_requested", execution.pending_tool_call)
            return {
                "execution_id": execution_id,
                "status": "waiting_approval",
                "result": execution.to_dict(),
            }
        execution.status = "completed"
        execution.result = parsed["content"]
        execution.messages.append({"role": "assistant", "content": parsed["content"]})
        self._transcripts.append_message(execution.context["transcript_id"], execution.messages[-1])
        execution.add_step("response", {"content": parsed["content"]})
        self._process_conversation_steer(execution)
        self._persist_execution(execution, "run_completed", {"result": parsed["content"]})
        return {
            "execution_id": execution_id,
            "status": "completed",
            "result": execution.to_dict(),
        }

    def reject(self, execution_id, reason):
        execution = self._get_execution(execution_id)
        if not execution:
            return {"execution_id": execution_id, "status": "error", "result": {"error": "execution not found"}}
        if execution.status != "waiting_approval":
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": {"error": "execution is not waiting for approval, current status: " + execution.status},
            }
        if not reason:
            reason = "Rejected by user"
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        execution.status = "running"
        pending = execution.pending_tool_call
        execution.pending_tool_call = None
        rejection_msg = (
            "The user rejected the tool call to '"
            + (pending["tool_name"] if pending else "unknown")
            + "'. Reason: "
            + reason
            + ". Please suggest an alternative approach."
        )
        execution.messages.append({"role": "user", "content": rejection_msg})
        execution.add_step("think", {"action": "rejection", "reason": reason})
        self._inject_pending_instructions(execution)
        context_for_ai = dict(getattr(execution, "context", {}) or {})
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        self._touch_execution(execution, "model_call_started", {"phase": "reject"})
        ai_result = self._ai_complete(execution.messages, execution.model, context_for_ai, execution.tools)
        self._touch_execution(execution, "model_call_completed", {"phase": "reject"})
        if self._is_cancelled(execution):
            return self._cancelled_result(execution)
        parsed = self._parse_ai_response(ai_result)
        if parsed["type"] == "authority_approval_required":
            return self._authority_approval_result(execution, parsed)
        if parsed["type"] == "error":
            execution.status = "error"
            execution.error = parsed["content"]
            execution.add_step("error", {"error": parsed["content"]})
            self._persist_execution(execution, "run_failed", {"error": parsed["content"]})
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": execution.to_dict(),
            }
        if parsed["type"] == "tool_call":
            if self._reject_unconnected_tool_call(execution, parsed) or self._reject_policy_violation(execution, parsed):
                self._persist_execution(execution, "run_failed", {"error": execution.error})
                return {
                    "execution_id": execution_id,
                    "status": "error",
                    "result": execution.to_dict(),
                }
            self._set_pending_tool_call(execution, parsed)
            self._transcripts.append_tool_call(
                execution.context["transcript_id"],
                execution.pending_tool_call,
            )
            auto_result = self._auto_approve_pending_tool_call(execution)
            if auto_result is not None:
                return auto_result
            self._persist_execution(execution, "approval_requested", execution.pending_tool_call)
            return {
                "execution_id": execution_id,
                "status": "waiting_approval",
                "result": execution.to_dict(),
            }
        execution.status = "completed"
        execution.result = parsed["content"]
        execution.messages.append({"role": "assistant", "content": parsed["content"]})
        self._transcripts.append_message(execution.context["transcript_id"], execution.messages[-1])
        execution.add_step("response", {"content": parsed["content"]})
        self._process_conversation_steer(execution)
        self._persist_execution(execution, "run_completed", {"result": parsed["content"]})
        return {
            "execution_id": execution_id,
            "status": "completed",
            "result": execution.to_dict(),
        }

    def cancel(self, execution_id):
        execution = self._get_execution(execution_id)
        if not execution:
            return {"execution_id": execution_id, "status": "error", "result": {"error": "execution not found"}}
        execution.status = "cancelled"
        execution.pending_tool_call = None
        execution.updated_at = timestamp()
        execution.add_step("think", {"action": "cancelled"})
        self._persist_execution(execution, "run_completed", {"status": "cancelled"})
        return {"execution_id": execution_id, "status": "cancelled"}

    def status(self, execution_id):
        execution = self._get_execution(execution_id)
        if not execution:
            return {"execution_id": execution_id, "status": "error", "result": {"error": "execution not found"}}
        return {
            "execution_id": execution_id,
            "status": execution.status,
            "steps": [s.to_dict() for s in execution.steps],
            "current_step": execution.current_step,
        }

    def plan(self, task, tools, model, system_prompt, context):
        execution_id = gen_id("agent_")
        execution_context = dict(context or {}) if isinstance(context, dict) else {}
        plan_system = system_prompt if system_prompt else ""
        plan_system += (
            "\n\nYou are in PLANNING mode. Do NOT execute any actions. "
            "Create a step-by-step plan for the following task. "
            "Return the plan as a numbered list. Do not call any tools."
        )
        execution = AgentExecution(
            execution_id=execution_id,
            task=task,
            tools=[],
            model=model if model else "default",
            system_prompt=plan_system,
        )
        execution.context = execution_context
        self._create_transcript(execution_id, execution.context, {"task": task, "mode": "plan"})
        self._executions[execution_id] = execution
        execution.status = "running"
        messages = []
        messages.append({"role": "system", "content": plan_system})
        messages.append({"role": "user", "content": task})
        execution.messages = messages
        for message in execution.messages:
            self._transcripts.append_message(execution.context["transcript_id"], message)
        execution.add_step("plan", {"action": "planning", "task": task})
        self._persist_execution(execution, "run_started", {"mode": "plan"})
        self._touch_execution(execution, "model_call_started", {"phase": "plan"})
        ai_result = self._ai_complete(messages, execution.model, execution.context, [])
        self._touch_execution(execution, "model_call_completed", {"phase": "plan"})
        parsed = self._parse_ai_response(ai_result)
        if parsed["type"] == "authority_approval_required":
            return self._authority_approval_result(execution, parsed)
        if parsed["type"] == "error":
            execution.status = "error"
            execution.error = parsed["content"]
            execution.add_step("error", {"error": parsed["content"]})
            self._persist_execution(execution, "run_failed", {"error": parsed["content"]})
            return {
                "execution_id": execution_id,
                "status": "error",
                "result": execution.to_dict(),
            }
        plan_content = parsed.get("content", "")
        if parsed["type"] == "tool_call":
            plan_content = "Agent attempted tool call during planning: " + str(parsed)
        execution.status = "planned"
        execution.result = plan_content
        execution.add_step("plan", {"plan": plan_content})
        self._persist_execution(execution, "run_completed", {"status": "planned"})
        return {
            "execution_id": execution_id,
            "status": "planned",
            "plan": plan_content,
            "result": execution.to_dict(),
        }
