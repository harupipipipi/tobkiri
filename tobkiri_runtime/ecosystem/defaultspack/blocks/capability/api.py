"""Capability catalog, settings, resolve, approval, and diagnostics routes."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from blocks._common import error, ok, timestamp
from domain.capability.activity_registry import ActivityRegistry
from domain.capability.orchestrator import CapabilityOrchestrator
from domain.capability.models import stable_revision
from domain.capability.repository import (
    CapabilityOwnerMismatch,
    CapabilityPlanAlreadyExecuted,
    CapabilityRepository,
    StaleCapabilityPlan,
)
from domain.capability.skill_lifecycle import SkillLifecycleStore
from domain.extensions.manifest import ManifestValidationError, validate_manifest
from domain.extensions.runtime import get_extension_registry
from domain.tool.registry import ToolRegistry
from domain.tool.schema_adapter import ToolSchemaError, adapt_tool_definitions


def run(input_data: dict[str, Any] | None, context: dict[str, Any] | None = None):
    payload = input_data if isinstance(input_data, dict) else {}
    action = str(payload.get("action") or "catalog")
    context = context if isinstance(context, dict) else {}
    repository = CapabilityRepository()

    try:
        if action == "catalog":
            include_advanced = bool(payload.get("advanced"))
            if include_advanced and not _has_developer_capability(context):
                return error(
                    "Advanced Tool catalog requires developer capability",
                    "FORBIDDEN",
                )
            return ok(_catalog(include_advanced=include_advanced))
        if action == "settings":
            return ok(repository.settings())
        if action == "update_settings":
            patch = payload.get("settings", payload.get("patch"))
            if patch is None:
                patch = {
                    key: value
                    for key, value in payload.items()
                    if key != "action"
                }
            return ok(repository.update_settings(patch))
        if action == "resolve":
            owner = _authority_scope(context)
            plan = CapabilityOrchestrator().resolve(
                user_text=str(payload.get("user_text") or payload.get("prompt") or ""),
                tools=ToolRegistry().list_tools(),
                settings=repository.settings(),
                runtime_profile=payload.get("runtime_profile"),
                selected_model_capabilities=payload.get("model_capabilities"),
                context={
                    **context,
                    "policy_generation": repository.policy_generation(),
                },
                dry_run=True,
            )
            repository.put_plan(plan, owner=owner)
            return ok(plan)
        if action == "plan":
            return _plan(repository, payload, context)
        if action == "trace":
            trace_id = str(payload.get("trace_id") or "").strip()
            record = repository.get_trace(
                trace_id,
                owner=_authority_scope(context),
                require_owner=True,
            )
            return (
                ok(record)
                if record is not None
                else error("Capability trace not found", "NOT_FOUND")
            )
        if action == "approve":
            return _approve(repository, payload, context)
        if action == "execute":
            return _execute(repository, payload, context)
        if action == "validate_manifest":
            manifest = payload.get("manifest")
            return ok({"valid": True, "manifest": validate_manifest(manifest)})
        if action == "compile_schema":
            return _compile_schema(payload)
        if action == "skills":
            skills = get_extension_registry(force_reload=True).skills().list(
                enabled_only=False
            )
            return ok({"skills": SkillLifecycleStore().list(skills)})
        if action == "update_skill":
            skills = get_extension_registry(force_reload=True).skills().list(
                enabled_only=False
            )
            skill_id = str(payload.get("skill_id") or "").strip()
            if not skill_id:
                return error("skill_id is required", "INVALID_INPUT")
            return ok(
                SkillLifecycleStore().set_enabled(
                    skill_id,
                    bool(payload.get("enabled")),
                    skills,
                )
            )
    except ManifestValidationError as exc:
        return error(str(exc), "INVALID_MANIFEST")
    except ToolSchemaError as exc:
        return error(str(exc), "INVALID_TOOL_SCHEMA")
    except CapabilityOwnerMismatch:
        return error("Capability record belongs to another scope", "FORBIDDEN")
    except (TypeError, ValueError) as exc:
        return error(str(exc), "INVALID_INPUT")
    except KeyError as exc:
        return error(f"Skill not found: {exc.args[0]}", "NOT_FOUND")
    return error(f"Unsupported capability action: {action}", "INVALID_ACTION")


def _catalog(*, include_advanced: bool = False) -> dict[str, Any]:
    extensions = get_extension_registry(force_reload=True)
    activities = ActivityRegistry(extensions.activities().list(enabled_only=True))
    tools = ToolRegistry()
    activity_records = activities.list()
    tool_records = tools.list_tools()
    return {
        "schema_version": "tobkiri.capability-catalog/v1",
        "activities": activity_records,
        "rail_items": [
            {"kind": "widget", "id": "capability-master"},
            *(
                {"kind": "activity", "id": str(activity.get("id") or "")}
                for activity in activity_records
            ),
        ],
        "tools": tool_records if include_advanced else [],
        "advanced_tools": tool_records if include_advanced else [],
        "skills": extensions.skills().list(enabled_only=True),
        "diagnostics": [
            *activities.diagnostics,
            *tools.diagnostics(),
        ],
    }


def _plan(
    repository: CapabilityRepository,
    payload: dict[str, Any],
    context: dict[str, Any],
):
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        return error("plan_id is required", "INVALID_INPUT")
    record = repository.get_plan(
        plan_id,
        owner=_authority_scope(context),
        require_owner=True,
    )
    if record is None:
        return error("Capability Plan not found", "NOT_FOUND")
    return ok(record)


def _approve(
    repository: CapabilityRepository,
    payload: dict[str, Any],
    context: dict[str, Any],
):
    plan_id = str(payload.get("plan_id") or "")
    owner = _authority_scope(context)
    current = repository.get_plan(
        plan_id,
        owner=owner,
        require_owner=True,
    )
    if current is None or not isinstance(current.get("plan"), dict):
        return error("Capability Plan not found", "NOT_FOUND")
    plan_effects = current["plan"].get("approval", {}).get("effects", [])
    denied = [
        effect
        for effect in plan_effects
        if isinstance(effect, dict) and effect.get("mode") == "deny"
    ]
    if denied:
        return error("Capability Plan contains denied effects", "POLICY_DENIED")
    if int(current["plan"].get("policy_generation") or 0) != repository.policy_generation():
        return error("Capability policy generation changed", "STALE_PLAN")
    approved_effects = (
        payload.get("approved_effects")
        if isinstance(payload.get("approved_effects"), list)
        else []
    )
    required = {
        (str(effect.get("tool_id") or ""), str(effect.get("class") or ""))
        for effect in plan_effects
        if isinstance(effect, dict) and effect.get("mode") == "confirm"
    }
    approved = {
        (str(effect.get("tool_id") or ""), str(effect.get("class") or ""))
        for effect in approved_effects
        if isinstance(effect, dict)
    }
    if not required.issubset(approved):
        return error(
            "Every confirm effect must be explicitly approved",
            "APPROVAL_INCOMPLETE",
            details={"missing": sorted(required - approved)},
        )
    try:
        record = repository.approve_plan(
            plan_id,
            registry_revision=str(payload.get("registry_revision") or ""),
            policy_revision=str(payload.get("policy_revision") or ""),
            approved_effects=approved_effects,
            principal_id=owner["principal_id"],
            owner=owner,
            invocation=_validated_invocation(payload, current["plan"]),
        )
    except KeyError:
        return error("Capability Plan not found", "NOT_FOUND")
    except StaleCapabilityPlan as exc:
        return error(str(exc), "STALE_PLAN")
    return ok(record)


def _execute(
    repository: CapabilityRepository,
    payload: dict[str, Any],
    context: dict[str, Any],
):
    plan_id = str(payload.get("plan_id") or "").strip()
    owner = _authority_scope(context)
    record = repository.get_plan(
        plan_id,
        owner=owner,
        require_owner=True,
    )
    if record is None:
        return error("Capability Plan not found", "NOT_FOUND")
    approval = record.get("approval")
    plan = record.get("plan")
    if not isinstance(approval, dict) or not isinstance(plan, dict):
        return error("Capability Plan must be approved before execution", "APPROVAL_REQUIRED")
    if (
        approval.get("registry_revision") != plan.get("registry_revision")
        or approval.get("policy_revision") != plan.get("policy_revision")
    ):
        return error("Capability Plan approval is stale", "STALE_PLAN")
    if int(plan.get("policy_generation") or 0) != repository.policy_generation():
        return error("Capability policy generation changed", "STALE_PLAN")

    executor = context.get("capability_plan_executor")
    if not callable(executor):
        return error(
            "No capability_plan_executor is bound; execution fails closed",
            "EXECUTOR_UNAVAILABLE",
        )
    try:
        repository.claim_execution(
            plan_id,
            {"at": timestamp(), "status": "running"},
            owner=owner,
            invocation=_validated_invocation(payload, plan),
        )
    except CapabilityPlanAlreadyExecuted:
        return error("Capability Plan was already executed", "ALREADY_EXECUTED")
    except StaleCapabilityPlan as exc:
        return error(str(exc), "STALE_PLAN")
    try:
        result = executor(plan, approval, payload.get("input"))
    except Exception as exc:
        repository.complete_execution(
            plan_id,
            {
                "at": timestamp(),
                "status": "outcome_unknown",
                "error": str(exc),
            },
            owner=owner,
        )
        return error(
            "Capability Plan execution outcome is unknown; automatic retry is forbidden",
            "OUTCOME_UNKNOWN",
        )
    stored = repository.complete_execution(
        plan_id,
        {"at": timestamp(), "status": "succeeded", "result": result},
        owner=owner,
    )
    return ok(stored)


def _compile_schema(payload: dict[str, Any]):
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = ToolRegistry().list_tools()
    schemas = adapt_tool_definitions(tools)
    return ok(
        {
            "valid": True,
            "tool_count": len(schemas),
            "schemas": schemas,
        }
    )


def _authority_scope(context: dict[str, Any]) -> dict[str, str]:
    return {
        "principal_id": str(
            context.get("principal_id") or context.get("user_id") or "local-user"
        ),
        "workspace_id": str(
            context.get("workspace_id")
            or context.get("conversation_workspace_id")
            or "local-workspace"
        ),
        "conversation_id": str(
            context.get("conversation_id") or "local-conversation"
        ),
        "profile_id": str(context.get("profile_id") or "default"),
    }


def _has_developer_capability(context: dict[str, Any]) -> bool:
    if context.get("developer_mode") is True:
        return True
    capabilities = context.get("principal_capabilities")
    return (
        isinstance(capabilities, (list, tuple, set))
        and "developer" in {str(item) for item in capabilities}
    )


def _invocation(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("invocation")
    if not isinstance(value, dict):
        value = payload.get("input")
    return value if isinstance(value, dict) else {}


def _validated_invocation(
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    invocation = dict(_invocation(payload))
    tool_id = str(
        invocation.get("tool_id") or invocation.get("tool_name") or ""
    ).strip()
    selected = (
        plan.get("tools", {}).get("selected", [])
        if isinstance(plan.get("tools"), dict)
        else []
    )
    if tool_id not in selected:
        raise ValueError("invocation Tool is not selected by the Capability Plan")
    tool = ToolRegistry().get(tool_id)
    if not isinstance(tool, dict):
        raise ValueError("invocation Tool is no longer registered")
    arguments = invocation.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("invocation arguments must be an object")
    schema = tool.get("schema")
    if not isinstance(schema, dict):
        contract = tool.get("contract")
        schema = (
            contract.get("input_schema")
            if isinstance(contract, dict)
            and isinstance(contract.get("input_schema"), dict)
            else {"type": "object"}
        )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        raise ValueError(f"invocation arguments: {errors[0].message}")
    return {
        "tool_id": tool_id,
        "tool_version": str(tool.get("version") or ""),
        "implementation_digest": stable_revision(
            {
                "execution": tool.get("execution"),
                "source_pack_id": tool.get("source_pack_id"),
                "schema": schema,
            }
        ),
        "arguments": arguments,
    }
