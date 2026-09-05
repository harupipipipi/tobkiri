"""Joint Activity, Tool, Skill, budget, and approval plan compilation."""

from __future__ import annotations

import json
from typing import Any, Iterable

from domain.capability.activity_registry import ActivityRegistry
from domain.capability.models import (
    CapabilityPlan,
    CapabilityRegistrySnapshot,
    CapabilityTarget,
    stable_revision,
)
from domain.capability.policy import EffectPolicyEngine
from domain.capability.settings import normalize_capability_settings
from domain.capability.skill_lifecycle import SkillLifecycleStore
from domain.capability.tool_scope import ToolScope, normalize_tool_scope
from domain.chat.tool_selection_orchestrator import ToolSelectionOrchestrator
from domain.extensions.runtime import get_extension_registry
from domain.mention import extract_mention_values
from domain.skill_trigger import RuntimeSkillTriggerService


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class CapabilityOrchestrator:
    """Compile one Capability Plan for runtime and dry-run consumers."""

    def __init__(
        self,
        *,
        activities: Iterable[dict[str, Any]] | None = None,
        skills: Iterable[dict[str, Any]] | None = None,
        call_handler: Any = None,
    ) -> None:
        self._activities = ActivityRegistry(activities)
        self._skills = (
            [dict(skill) for skill in skills if isinstance(skill, dict)]
            if skills is not None
            else _runtime_skills()
        )
        self._call_handler = call_handler
        self._policy = EffectPolicyEngine()

    def resolve(
        self,
        *,
        user_text: str,
        tools: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
        runtime_profile: dict[str, Any] | None = None,
        selected_model_capabilities: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Resolve a versioned plan without executing any Tool."""

        del dry_run  # Resolution is always passive; execution is a separate API.
        context = context if isinstance(context, dict) else {}
        normalized_settings = normalize_capability_settings(settings)
        capabilities = normalized_settings["capabilities"]
        snapshot = _capability_snapshot(
            self._activities.list(),
            tools,
            self._skills,
        )
        plan = CapabilityPlan.empty(
            user_text=user_text,
            registry_revision=snapshot.revision,
            policy_revision=stable_revision(
                {
                    "approval": capabilities.get("approval"),
                    "profile": runtime_profile,
                }
            ),
        )
        plan.policy_generation = int(context.get("policy_generation") or 0)
        plan.diagnostics.extend(
            {"code": code, "message": message}
            for code, message in snapshot.diagnostics
        )
        if not capabilities.get("enabled", True):
            plan.diagnostics.append(
                {
                    "code": "capabilities_disabled",
                    "message": "The capability master gate is off.",
                }
            )
            return plan.to_dict()

        explicit_targets = _explicit_targets(user_text)
        activity_resolutions = self._activities.resolve_mentions(user_text)
        if not activity_resolutions:
            activity_resolutions = self._activities.infer(user_text, limit=3)
        invalid_activities = set(snapshot.invalid_activity_ids)
        activity_resolutions = [
            item
            for item in activity_resolutions
            if item.activity_id not in invalid_activities
        ]
        plan.explicit_mentions = explicit_targets
        plan.activities = [
            {
                "id": item.activity_id,
                "source": item.source,
                "confidence": item.confidence,
            }
            for item in activity_resolutions
        ]
        activity_ids = [item.activity_id for item in activity_resolutions]
        expansion = self._activities.expand(activity_ids, tools)
        tool_by_id = {
            _tool_id(tool): tool
            for tool in tools
            if isinstance(tool, dict) and _tool_id(tool)
        }
        explicit_tool_ids = [
            target.id for target in explicit_targets if target.kind == "tool"
        ]
        candidate_ids = _unique(
            [*explicit_tool_ids, *expansion["tool_ids"]]
        )
        if not candidate_ids:
            candidate_ids = list(tool_by_id)
            if activity_ids:
                plan.fallbacks.append(
                    {
                        "stage": "activity_expansion",
                        "reason": "activity_members_unavailable",
                    }
                )

        scope = _profile_tool_scope(runtime_profile)
        eligible_ids: list[str] = []
        for tool_id in candidate_ids:
            tool = tool_by_id.get(tool_id)
            if tool is None:
                plan.excluded_tools.append(
                    {
                        "id": tool_id,
                        "stage": "registry",
                        "reason": "not_registered",
                    }
                )
                continue
            if not scope.allows(tool_id):
                plan.excluded_tools.append(
                    {
                        "id": tool_id,
                        "stage": "runtime_profile",
                        "reason": f"tool_scope:{scope.mode}",
                    }
                )
                continue
            if tool.get("enabled") is False:
                plan.excluded_tools.append(
                    {
                        "id": tool_id,
                        "stage": "eligibility",
                        "reason": "disabled",
                    }
                )
                continue
            eligible_ids.append(tool_id)
        plan.tool_candidates = eligible_ids

        advanced = capabilities["advanced"]
        limit = int(advanced["max_attached_tools"])
        if activity_ids:
            activity_limits = [
                int(
                    (
                        self._activities.get(activity_id) or {}
                    ).get("selection", {}).get("max_attached_tools", limit)
                )
                for activity_id in activity_ids
            ]
            if activity_limits:
                limit = min([limit, *activity_limits])
        selected_tools = _select_tools(
            user_text=user_text,
            tools=[tool_by_id[tool_id] for tool_id in eligible_ids],
            limit=limit,
            model_capabilities=selected_model_capabilities,
            settings=settings,
            call_handler=self._call_handler,
        )
        plan.selected_tools = selected_tools
        plan.hydrated_tools = list(selected_tools)
        plan.attached_tools = list(selected_tools)
        plan.tool_schema_hashes = {
            tool_id: stable_revision(_tool_schema(tool_by_id[tool_id]))
            for tool_id in selected_tools
            if tool_id in tool_by_id
        }
        plan.tool_capability_grants = {
            tool_id: _tool_capability_grants(tool_by_id[tool_id])
            for tool_id in selected_tools
            if tool_id in tool_by_id
        }
        plan.provider_selections = _provider_selections(context)

        explicit_skills = [
            target.id for target in explicit_targets if target.kind == "skill"
        ]
        skill_eval = RuntimeSkillTriggerService(self._skills).evaluate(
            user_text=user_text,
            tool_names=selected_tools,
            context={
                **context,
                "skills": explicit_skills,
                "required_skills": expansion["required_skills"],
                "safety_skills": expansion["safety_skills"],
            },
        )
        matched = (
            skill_eval.get("matched", [])
            if isinstance(skill_eval, dict)
            else []
        )
        matched = matched[: int(advanced["max_attached_skills"])]
        plan.required_skills = _unique(
            [*expansion["safety_skills"], *expansion["required_skills"]]
        )
        plan.selected_skills = [
            str(item.get("id") or "")
            for item in matched
            if str(item.get("id") or "").strip()
        ]
        plan.loaded_skills = list(plan.selected_skills)

        for tool_id in selected_tools:
            tool = tool_by_id[tool_id]
            decisions = self._policy.resolve(
                tool,
                normalized_settings,
                profile_policy=_profile_policy(runtime_profile),
                full_access=bool(context.get("full_access")),
            )
            for decision in decisions:
                item = decision.to_dict()
                item["tool_id"] = tool_id
                plan.approval_effects.append(item)

        plan.tool_schema_tokens = sum(
            _estimated_schema_tokens(tool_by_id[tool_id])
            for tool_id in selected_tools
        )
        plan.skill_instruction_tokens = sum(
            _estimated_instruction_tokens(item) for item in matched
        )
        if plan.tool_schema_tokens > int(advanced["max_tool_schema_tokens"]):
            plan.diagnostics.append(
                {
                    "code": "tool_schema_budget_exceeded",
                    "message": "Selected Tool schemas exceed the configured budget.",
                }
            )
        if plan.skill_instruction_tokens > int(advanced["max_skill_tokens"]):
            plan.diagnostics.append(
                {
                    "code": "skill_budget_exceeded",
                    "message": "Selected Skill instructions exceed the configured budget.",
                }
            )
        for alias, activity_ids_for_alias in self._activities.collisions.items():
            plan.diagnostics.append(
                {
                    "code": "activity_alias_collision",
                    "message": f"{alias}: {', '.join(activity_ids_for_alias)}",
                }
            )
        return plan.to_dict()

    def compile_selected(
        self,
        *,
        user_text: str,
        selected_tools: list[dict[str, Any]],
        eligible_tools: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
        runtime_profile: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compile the exact model payload authority after eligibility filtering."""

        context = context if isinstance(context, dict) else {}
        normalized_settings = normalize_capability_settings(settings)
        capabilities = normalized_settings["capabilities"]
        snapshot = _capability_snapshot(
            self._activities.list(),
            eligible_tools,
            self._skills,
        )
        plan = CapabilityPlan.empty(
            user_text=user_text,
            registry_revision=snapshot.revision,
            policy_revision=stable_revision(
                {
                    "approval": capabilities.get("approval"),
                    "profile": runtime_profile,
                    "generation": context.get("policy_generation"),
                }
            ),
        )
        plan.policy_generation = int(context.get("policy_generation") or 0)
        plan.diagnostics.extend(
            {"code": code, "message": message}
            for code, message in snapshot.diagnostics
        )
        activity_ids = [
            str(item)
            for item in context.get("capability_activity_ids", [])
            if str(item).strip()
            and str(item) not in set(snapshot.invalid_activity_ids)
        ]
        plan.activities = [
            {
                "id": activity_id,
                "source": (
                    "explicit_mention"
                    if f"@{activity_id}" in user_text
                    or f"@activity:{activity_id}" in user_text
                    else "intent_lexical"
                ),
                "confidence": 1.0,
            }
            for activity_id in activity_ids
        ]
        plan.explicit_mentions = _explicit_targets(user_text)
        plan.tool_candidates = [
            _tool_id(tool) for tool in eligible_tools if _tool_id(tool)
        ]
        plan.selected_tools = [
            _tool_id(tool) for tool in selected_tools if _tool_id(tool)
        ]
        plan.hydrated_tools = list(plan.selected_tools)
        plan.attached_tools = list(plan.selected_tools)
        plan.tool_schema_hashes = {
            _tool_id(tool): stable_revision(_tool_schema(tool))
            for tool in selected_tools
            if _tool_id(tool)
        }
        plan.tool_capability_grants = {
            _tool_id(tool): _tool_capability_grants(tool)
            for tool in selected_tools
            if _tool_id(tool)
        }
        plan.provider_selections = _provider_selections(context)

        expansion = self._activities.expand(activity_ids, eligible_tools)
        safety_skills = _unique(
            [
                *expansion["safety_skills"],
                *context.get("safety_skills", []),
            ]
        )
        required_skills = _unique(
            [
                *expansion["required_skills"],
                *context.get("required_skills", []),
            ]
        )
        explicit_skills = [
            target.id
            for target in plan.explicit_mentions
            if target.kind == "skill"
        ]
        skill_eval = RuntimeSkillTriggerService(self._skills).evaluate(
            user_text=user_text,
            tool_names=list(plan.selected_tools),
            context={
                **context,
                "verified_explicit_skills": explicit_skills,
                "required_skills": required_skills,
                "safety_skills": safety_skills,
            },
        )
        matched = (
            skill_eval.get("matched", [])
            if isinstance(skill_eval, dict)
            else []
        )
        matched = matched[: int(capabilities["advanced"]["max_attached_skills"])]
        plan.required_skills = _unique([*safety_skills, *required_skills])
        plan.selected_skills = [
            str(item.get("id") or "")
            for item in matched
            if str(item.get("id") or "").strip()
        ]
        plan.loaded_skills = list(plan.selected_skills)
        plan.skill_instruction_hashes = {
            str(item["id"]): stable_revision(str(item.get("instruction") or ""))
            for item in matched
            if str(item.get("id") or "").strip()
        }
        skill_instructions = (
            str(skill_eval.get("instructions") or "").strip()
            if isinstance(skill_eval, dict)
            else ""
        )

        for tool in selected_tools:
            tool_id = _tool_id(tool)
            for decision in self._policy.resolve(
                tool,
                normalized_settings,
                profile_policy=_profile_policy(runtime_profile),
                full_access=bool(context.get("full_access")),
            ):
                item = decision.to_dict()
                item["tool_id"] = tool_id
                plan.approval_effects.append(item)
        plan.tool_schema_tokens = sum(
            _estimated_schema_tokens(tool) for tool in selected_tools
        )
        plan.skill_instruction_tokens = max(0, len(skill_instructions) // 4)
        result = plan.to_dict()
        result["_compiled_model_input"] = {
            "tool_ids": list(plan.selected_tools),
            "tool_schema_hashes": dict(plan.tool_schema_hashes),
            "tools": list(selected_tools),
            "skill_ids": list(plan.selected_skills),
            "skill_instruction_hashes": dict(plan.skill_instruction_hashes),
            "skill_instructions": skill_instructions,
            "matched_skills": matched,
        }
        return result


def _select_tools(
    *,
    user_text: str,
    tools: list[dict[str, Any]],
    limit: int,
    model_capabilities: dict[str, Any] | None,
    settings: dict[str, Any] | None,
    call_handler: Any,
) -> list[str]:
    if not tools or limit <= 0:
        return []
    result = ToolSelectionOrchestrator(call_handler=call_handler).select(
        user_text,
        tools,
        limit=limit,
        selected_model_capabilities=model_capabilities,
        settings=settings,
        prefilter=len(tools) > limit,
    )
    recommendations = (
        _list_or_empty(result.get("recommended_tools"))
        if isinstance(result, dict)
        else []
    )
    selected = [
        str(item.get("tool_id") or "").strip()
        for item in recommendations
        if isinstance(item, dict) and str(item.get("tool_id") or "").strip()
    ]
    if selected:
        return _unique(selected)[:limit]
    return [_tool_id(tool) for tool in tools[:limit] if _tool_id(tool)]


def _explicit_targets(text: str) -> list[CapabilityTarget]:
    result: list[CapabilityTarget] = []
    seen: set[tuple[str, str]] = set()
    for value in extract_mention_values(text):
        if ":" not in value:
            continue
        kind, target_id = value.split(":", 1)
        kind = kind.casefold()
        target_id = target_id.strip()
        if kind not in {"activity", "service", "tool", "skill"} or not target_id:
            continue
        key = (kind, target_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(CapabilityTarget(kind=kind, id=target_id))
    return result


def _profile_tool_scope(runtime_profile: dict[str, Any] | None) -> ToolScope:
    if not isinstance(runtime_profile, dict):
        return ToolScope()
    direct = runtime_profile.get("tool_scope")
    if direct is not None:
        return normalize_tool_scope(direct)
    defaultspack = runtime_profile.get("defaultspack")
    if isinstance(defaultspack, dict) and defaultspack.get("tool_scope") is not None:
        return normalize_tool_scope(defaultspack["tool_scope"])
    return ToolScope()


def _profile_policy(runtime_profile: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime_profile, dict):
        return {}
    policy = runtime_profile.get("policy")
    return dict(policy) if isinstance(policy, dict) else {}


def _runtime_skills() -> list[dict[str, Any]]:
    try:
        skills = get_extension_registry().skills().list(enabled_only=True)
        return SkillLifecycleStore().apply(skills)
    except Exception:
        return []


def _compact_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _tool_id(tool),
        "summary": tool.get("summary") or tool.get("description"),
        "tags": tool.get("tags", []),
        "schema_hash": stable_revision(tool.get("schema") or {}),
    }


def _tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("schema")
    if isinstance(schema, dict):
        return schema
    contract = tool.get("contract")
    if isinstance(contract, dict) and isinstance(contract.get("input_schema"), dict):
        return contract["input_schema"]
    return {}


def _compact_skill(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": skill.get("id"),
        "description": skill.get("description"),
        "scope": skill.get("scope"),
    }


def _tool_id(tool: dict[str, Any]) -> str:
    return str(tool.get("tool_id") or tool.get("name") or tool.get("id") or "").strip()


def _tool_capability_grants(tool: dict[str, Any]) -> list[str]:
    direct = tool.get("capability_grants")
    requirements = _mapping_or_empty(tool.get("capability_requirements"))
    values = list(direct) if isinstance(direct, list) else []
    for key in ("runtime", "connections"):
        raw = requirements.get(key)
        if isinstance(raw, list):
            values.extend(raw)
    normalized = {
        str(item).strip()
        for item in values
        if str(item or "").strip()
    }
    connection_capabilities = {
        "rumi.service.file.inspect.v1": "file.inspect",
        "rumi.service.ai.generate.v1": "ai.gateway.generate",
        "rumi.service.subagent.placement.compile.v1": (
            "subagent.placement.compile"
        ),
        "rumi.service.subagent.topology.compile.v1": (
            "subagent.topology.compile"
        ),
        "rumi.service.subagent.placement.patch.v1": (
            "subagent.placement.patch"
        ),
        "rumi.service.subagent.runtime.assignment.v1": (
            "subagent.runtime.assign"
        ),
    }
    normalized.update(
        connection_capabilities[item]
        for item in tuple(normalized)
        if item in connection_capabilities
    )
    if "rumi.service.repository.context.prepare.v1" in normalized:
        normalized.update(
            {
                "file.inspect",
                "ai.gateway.generate",
                "subagent.placement.compile",
                "repository.content.external_share",
            }
        )
    return sorted(normalized)


def _provider_selections(
    context: dict[str, Any],
) -> dict[str, list[str]]:
    """Project Host-selected provider identities into the canonical plan."""

    explicit = context.get("capability_provider_selections")
    if isinstance(explicit, dict):
        return {
            str(contract_id): sorted(
                {
                    str(provider_id).strip()
                    for provider_id in provider_ids
                    if str(provider_id).strip()
                }
            )
            for contract_id, provider_ids in explicit.items()
            if str(contract_id).strip() and isinstance(provider_ids, list)
        }
    try:
        from core_runtime.resolved_profile_scope import (
            persisted_resolved_profile,
        )

        resolved = persisted_resolved_profile()
        providers = getattr(resolved, "providers", ()) if resolved else ()
        result: dict[str, list[str]] = {}
        for provider in providers:
            contract_id = str(getattr(provider, "contract_id", "")).strip()
            provider_id = str(
                getattr(provider, "provider_instance_id", "")
            ).strip()
            if contract_id and provider_id:
                result.setdefault(contract_id, []).append(provider_id)
        return {
            key: sorted(set(value))
            for key, value in sorted(result.items())
        }
    except Exception:
        return {}


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _estimated_schema_tokens(tool: dict[str, Any]) -> int:
    schema = tool.get("schema")
    if not isinstance(schema, dict):
        contract = tool.get("contract")
        schema = (
            contract.get("input_schema")
            if isinstance(contract, dict)
            and isinstance(contract.get("input_schema"), dict)
            else {}
        )
    payload = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    return max(1, len(payload) // 4)


def _estimated_instruction_tokens(skill: dict[str, Any]) -> int:
    return max(1, len(str(skill.get("instruction") or "")) // 4)


def _capability_snapshot(
    activities: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    skills: list[dict[str, Any]],
) -> CapabilityRegistrySnapshot:
    tool_ids = tuple(sorted(_tool_id(tool) for tool in tools if _tool_id(tool)))
    skill_ids = tuple(
        sorted(
            str(skill.get("id") or "")
            for skill in skills
            if str(skill.get("id") or "").strip()
        )
    )
    activity_ids = tuple(
        sorted(
            str(activity.get("id") or "")
            for activity in activities
            if str(activity.get("id") or "").strip()
        )
    )
    known_skills = set(skill_ids)
    invalid: set[str] = set()
    diagnostics: list[tuple[str, str]] = []
    for activity in activities:
        activity_id = str(activity.get("id") or "").strip()
        members = _mapping_or_empty(activity.get("members"))
        skill_members = _mapping_or_empty(members.get("skills"))
        required = _unique(
            [
                *(skill_members.get("safety") or []),
                *(skill_members.get("required") or []),
            ]
        )
        missing_required = sorted(set(required) - known_skills)
        if missing_required:
            invalid.add(activity_id)
            diagnostics.append(
                (
                    "activity_required_skill_missing",
                    f"{activity_id}: {', '.join(missing_required)}",
                )
            )
        missing_optional = sorted(
            set(_unique(skill_members.get("optional") or [])) - known_skills
        )
        if missing_optional:
            diagnostics.append(
                (
                    "activity_optional_skill_missing",
                    f"{activity_id}: {', '.join(missing_optional)}",
                )
            )
    state = {
        "activities": [_compact_activity(item) for item in activities],
        "tools": [_compact_tool(item) for item in tools],
        "skills": [_compact_skill(item) for item in skills],
        "invalid_activity_ids": sorted(invalid),
        "diagnostics": diagnostics,
    }
    return CapabilityRegistrySnapshot(
        revision=stable_revision(state),
        activity_ids=activity_ids,
        tool_ids=tool_ids,
        skill_ids=skill_ids,
        invalid_activity_ids=tuple(sorted(invalid)),
        diagnostics=tuple(diagnostics),
    )


def _compact_activity(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": activity.get("id"),
        "version": activity.get("version"),
        "members": activity.get("members"),
    }
