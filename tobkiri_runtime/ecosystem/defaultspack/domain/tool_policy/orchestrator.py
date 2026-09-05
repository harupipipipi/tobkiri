from __future__ import annotations

from typing import Any

from blocks._common import error
from domain.agent_runtime.tool_ledger import ToolLedger
from domain.agent_runtime.run_store import AgentRunStore
from domain.hooks.dispatcher import dispatch_hook
from domain.tool.catalog_contract_client import ContractToolCatalog as ToolRegistry

from .audit import audit_tool_policy
from .internal_context import (
    profile_policy_context_is_trusted,
    sanitize_tool_context,
    sanitize_untrusted_tool_context,
    seal_tool_context,
)
from .policy import decide_tool_policy


def _is_cancelled(context: dict[str, Any] | None) -> bool:
    checker = context.get("is_cancelled") if isinstance(context, dict) else None
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


class ToolOrchestrator:
    """Approval, policy, sandbox, ledger, and execution gateway for tools."""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()
        self.ledger = ToolLedger()
        self.store = AgentRunStore()

    def run(self, tool_name: str, arguments: dict[str, Any] | None, context: dict[str, Any] | None) -> dict[str, Any]:
        profile_policy_trusted = profile_policy_context_is_trusted(context)
        if profile_policy_trusted:
            context = sanitize_tool_context(context)
        else:
            context = sanitize_untrusted_tool_context(context)
        if _is_cancelled(context):
            return error("Tool execution cancelled", "CANCELLED")
        tool_def = self._resolve_tool(tool_name)
        run_id = context.get("agent_run_id") or context.get("run_id")
        tool_call_id = context.get("tool_call_id")
        approval_id = context.get("approval_id")
        decision = decide_tool_policy(
            tool_def,
            context,
            tool_name=tool_name,
            arguments=arguments or {},
            approval_granted=self._server_approval_granted(run_id, tool_call_id, approval_id),
        )
        audit_tool_policy(
            context,
            "tool_policy_decision",
            {"tool_name": tool_name, "decision": decision.to_dict()},
        )
        if not decision.allowed:
            return error(decision.reason or "tool denied by policy", "POLICY_DENIED")
        if decision.requires_approval:
            return {
                "status": "waiting_approval",
                "data": {
                    "tool_name": tool_name,
                    "arguments": arguments or {},
                    "policy": decision.to_dict(),
                },
            }

        tool_call_id = tool_call_id or "call_{}_{}".format(run_id or "adhoc", tool_name)
        if run_id:
            self.ledger.started(str(run_id), str(tool_call_id), tool_name, arguments or {})
        dispatch_hook(
            "before_tool_call",
            {"run_id": run_id, "tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": arguments or {}},
        )

        invoke_context = seal_tool_context(context, decision.to_dict())
        invoke_context["sandbox_mode"] = decision.sandbox_mode
        if _is_cancelled(invoke_context):
            return error("Tool execution cancelled", "CANCELLED")
        # The legacy block route is an approval-persisted Capability API
        # adapter and intentionally rejects direct calls.  Agent execution
        # already carries the detached v4 CapabilityPlan, so invoke the last
        # non-core Tool boundary directly and retain the orchestrator envelope.
        from domain.tool.executor import ToolExecutor

        executor = ToolExecutor()
        executor._registry = self.registry
        executed = executor.execute(tool_name, arguments or {}, invoke_context)
        result = _tool_result_envelope(executed)

        if run_id:
            is_error = result.get("status") != "ok" or bool((result.get("data") or {}).get("is_error"))
            self.ledger.completed(str(run_id), str(tool_call_id), tool_name, result, is_error=is_error)
        dispatch_hook(
            "after_tool_call",
            {"run_id": run_id, "tool_call_id": tool_call_id, "tool_name": tool_name, "result": result},
        )
        return result

    def _resolve_tool(self, tool_name: str) -> dict[str, Any] | None:
        tool_def = self.registry.get(tool_name)
        if tool_def is not None:
            return tool_def
        for item in self.registry.list_tools():
            if item.get("name") == tool_name:
                return item
        return None

    def _server_approval_granted(self, run_id: Any, tool_call_id: Any, approval_id: Any) -> bool:
        if not run_id or not tool_call_id or not approval_id:
            return False
        return self.store.is_approval_granted(str(run_id), str(tool_call_id), str(approval_id))


def _tool_result_envelope(result: Any) -> dict[str, Any]:
    """Normalize the executor result to the agent-facing status/data shape."""

    if isinstance(result, dict) and result.get("status") in {
        "ok",
        "error",
        "waiting_approval",
    }:
        return result
    data = result if isinstance(result, dict) else {
        "result": str(result),
        "is_error": True,
        "widget": None,
    }
    status = "error" if bool(data.get("is_error")) else "ok"
    envelope: dict[str, Any] = {"status": status, "data": data}
    if status == "error":
        envelope["error"] = str(data.get("result") or "Tool execution failed")
        if data.get("error_type"):
            envelope["error_type"] = data["error_type"]
    return envelope
