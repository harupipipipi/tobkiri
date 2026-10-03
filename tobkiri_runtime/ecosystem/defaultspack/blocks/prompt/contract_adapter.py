"""Finite legacy `/api/prompts/*` adapter to global prompt contracts.

Owner: rumi_prompt_studio_pack
Removal Wave: 10
Sunset: 2027-12-31
"""

from __future__ import annotations

from blocks._common import error, ok
from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    invoke_global_contract,
)
from core_runtime.resolved_profile_scope import active_resolved_profile
from domain.capability.orchestrator import CapabilityOrchestrator
from domain.capability.repository import CapabilityRepository
from domain.tool.registry import ToolRegistry

_RESOURCE_CONTRACT = "rumi.resource.prompt.studio.v1"
_AUTHOR_CONTRACT = "rumi.action.prompt.author.v1"
_VERSION_CONTRACT = "rumi.action.prompt.version.v1"
_TEST_CONTRACT = "rumi.action.prompt.test.v1"

_OPERATION_CONTRACT = {
    "editor.load": _RESOURCE_CONTRACT,
    "active": _RESOURCE_CONTRACT,
    "traces": _RESOURCE_CONTRACT,
    "save": _AUTHOR_CONTRACT,
    "override": _AUTHOR_CONTRACT,
    "delete": _AUTHOR_CONTRACT,
    "diff": _AUTHOR_CONTRACT,
    "lint": _AUTHOR_CONTRACT,
    "compact": _AUTHOR_CONTRACT,
    "convert": _AUTHOR_CONTRACT,
    "build": _AUTHOR_CONTRACT,
    "context_vars": _AUTHOR_CONTRACT,
    "preview_toggle": _AUTHOR_CONTRACT,
    "edge.toggle": _AUTHOR_CONTRACT,
    "edge.preview": _AUTHOR_CONTRACT,
    "conditional": _AUTHOR_CONTRACT,
    "inherit": _AUTHOR_CONTRACT,
    "preview": _TEST_CONTRACT,
    "toggle": _AUTHOR_CONTRACT,
    "versions": _VERSION_CONTRACT,
    "rollback": _VERSION_CONTRACT,
    "test": _TEST_CONTRACT,
}


def run(input_data: dict, context: dict) -> dict:
    """Translate a legacy route into one implementation-neutral invocation."""
    data = dict(input_data) if isinstance(input_data, dict) else {}
    operation = str(data.pop("_contract_operation", "")).strip()
    if not operation:
        operation = str(data.pop("action", "")).strip().lower()
    operation = {
        "load": "editor.load",
        "editor": "editor.load",
        "create_override": "save",
        "version_list": "versions",
        "test_input": "test",
    }.get(operation, operation)
    if operation == "override":
        operation = "save"
    if data.get("edge_id"):
        if operation == "toggle":
            operation = "edge.toggle"
        elif operation == "preview_toggle":
            operation = "edge.preview"
    contract_id = _OPERATION_CONTRACT.get(operation)
    registry = context.get("v4_dispatch_session") if isinstance(context, dict) else None
    plan = active_resolved_profile()
    if not contract_id or registry is None or plan is None:
        return error("Prompt Studio contract is unavailable", "PROMPT_STUDIO_UNAVAILABLE")
    requested_profile = str(data.get("profile_id") or "").strip()
    if requested_profile and requested_profile != plan.profile_id:
        return error("Prompt Studio profile is not active", "PROMPT_STUDIO_DENIED")
    data["profile_id"] = plan.profile_id
    if operation in {"test", "preview"}:
        repository = CapabilityRepository()
        data["capability_plan"] = CapabilityOrchestrator(
            call_handler=context.get("call_handler")
        ).resolve(
            user_text=str(
                data.get("user_text")
                or data.get("input")
                or data.get("prompt")
                or ""
            ),
            tools=ToolRegistry().list_tools(),
            settings=repository.settings(),
            runtime_profile=(
                context.get("runtime_profile")
                if isinstance(context.get("runtime_profile"), dict)
                else None
            ),
            selected_model_capabilities=(
                data.get("model_capabilities")
                if isinstance(data.get("model_capabilities"), dict)
                else None
            ),
            context={
                **context,
                "policy_generation": repository.policy_generation(),
            },
            dry_run=True,
        )
    if contract_id.startswith("rumi.action.") and context.get(
        "_tool_server_approved"
    ) is not True:
        return error("Prompt Studio action requires approval", "PROMPT_STUDIO_DENIED")
    try:
        return ok(
            invoke_global_contract(
                registry,
                contract_id,
                operation,
                data,
            )
        )
    except GlobalContractUnavailable as exc:
        return error(str(exc), "PROMPT_STUDIO_UNAVAILABLE")
    except GlobalContractInvocationError as exc:
        code = (
            "PROMPT_WRITE_CONFLICT"
            if exc.code == "PromptWriteConflict"
            else "PROMPT_STUDIO_FAILED"
        )
        return error(str(exc), code)
    except Exception as exc:
        code = (
            "PROMPT_WRITE_CONFLICT"
            if type(exc).__name__ == "PromptWriteConflict"
            else "PROMPT_STUDIO_FAILED"
        )
        return error(str(exc), code)
