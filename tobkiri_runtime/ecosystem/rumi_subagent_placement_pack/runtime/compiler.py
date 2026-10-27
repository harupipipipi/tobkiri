"""Compile immutable, least-authority Subagent Placement plans."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping

from core_runtime.capability_plan import (
    CapabilityPlanValidationError,
    validate_capability_plan,
)


CATALOG = "rumi.resource.subagent.catalog.v1"
PLACEMENT = "rumi.resource.subagent.placement.v1"
COMPILE = "rumi.service.subagent.placement.compile.v1"
STAGE = "rumi.service.subagent.placement.stage.v1"
PROTOCOL = "rumi.service.subagent.protocol.v1"
HOST_AUTHORITY = "rumi.service.host.authorize.v1"
PACK_ID = "rumi_subagent_placement_pack"

_SCHEMA_DEFINITION = "tobkiri.subagent/v1"
_SCHEMA_PLACEMENT = "tobkiri.subagent-placement/v1"
_SCHEMA_PLAN = "tobkiri.effective-subagent/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_APPROVAL_RANK = {"auto": 0, "confirm": 1, "deny": 2}
_ENFORCEMENT_RANK = {
    "behavioral_only": 0,
    "remote_attested": 1,
    "host_validated": 2,
    "sandbox_enforced": 3,
    "host_enforced": 4,
}
_STANDARD_BINDINGS = {
    "model",
    "tools",
    "skills",
    "memory",
    "memory.private",
    "memory.shared",
    "workspace",
    "context",
    "artifacts",
    "communication",
    "scheduler",
    "approval",
    "budget",
}
_BUILTIN_PROTOCOLS = (
    "agent-tool",
    "delegate",
    "direct-message",
    "handoff",
    "observer",
    "review",
    "scheduled",
    "shared-task-list",
    "supervisor",
    "team-member",
)


class PlacementCompileError(ValueError):
    """Raised when a Placement cannot compile without widening authority."""


class SubagentPlacementCompiler:
    """Resolve Pack declarations and compile an immutable effective plan."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def compile(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Compile one Placement against selected registries and policies."""

        _redeem_authority(self.client, payload)
        placement, placement_source = self._resolve_placement(payload)
        definition, definition_source = self._resolve_definition(placement)
        raw_capability_plan = _object(
            payload.get("capability_plan"),
            "capability_plan",
        )
        try:
            capability_plan = validate_capability_plan(raw_capability_plan)
        except CapabilityPlanValidationError as exc:
            raise PlacementCompileError(str(exc)) from exc
        selected_stage_ids = _selected_provider_ids(
            capability_plan,
            STAGE,
        )
        feature_warnings = self._validate_features(
            placement,
            selected_stage_ids,
        )
        bindings = _bindings(placement)
        _validate_binding_policies(definition, bindings)
        protocols = self._validate_protocols(placement, definition)
        authority = _compile_authority(
            definition,
            placement,
            capability_plan,
            payload,
        )
        budgets = _compile_budgets(definition, placement, payload)
        approval = _strictest_approval(definition, placement, payload)
        enforcement = _compile_enforcement(definition, placement, payload)
        behavior = _compile_behavior(definition, placement, payload)
        revisions = {
            "registry_revision": str(payload.get("registry_revision") or ""),
            "subagent_content_hash": _sha(definition),
            "placement_revision": _sha(placement),
            "capability_plan_revision": _capability_revision(capability_plan),
            "topology_revision": str(payload.get("topology_revision") or ""),
        }
        if not revisions["registry_revision"]:
            raise PlacementCompileError("registry_revision is required")
        base = {
            "schema_version": _SCHEMA_PLAN,
            "subagent": {
                "pack_id": definition_source["source_pack_id"],
                "provider_instance_id": definition_source[
                    "provider_instance_id"
                ],
                "id": definition["id"],
                "version": definition["version"],
                "content_hash": revisions["subagent_content_hash"],
            },
            "placement": {
                "pack_id": placement_source["source_pack_id"],
                "provider_instance_id": placement_source[
                    "provider_instance_id"
                ],
                "id": placement["id"],
                "revision": revisions["placement_revision"],
            },
            "presentation": dict(placement.get("presentation") or {}),
            "agent_kind": str(definition.get("kind") or "subagent"),
            "runtime_kind": _runtime_kind(definition),
            "role": dict(placement["role"]),
            "bindings": bindings,
            "model": _binding_projection(bindings, "model"),
            "tool_bindings": _binding_projection(bindings, "tools"),
            "skill_bindings": _binding_projection(bindings, "skills"),
            "memory_bindings": [
                dict(item)
                for item in bindings
                if str(item.get("slot") or "").startswith("memory")
            ],
            "workspace_binding": _binding_projection(
                bindings,
                "workspace",
            ),
            "protocol_bindings": protocols,
            "capability_plan_ref": _capability_ref(capability_plan),
            "effective_authority": authority,
            "authority_envelope": {
                "source": "host_intersection",
                "capabilities": authority,
            },
            "budgets": budgets,
            "approval": approval,
            "behavior": behavior,
            "enforcement": enforcement,
            "features": list(placement.get("features") or []),
            "warnings": feature_warnings,
            "revisions": revisions,
        }
        staged = self._apply_stages(base, selected_stage_ids)
        staged["plan_hash"] = _sha(
            {key: value for key, value in staged.items() if key != "plan_hash"}
        )
        return staged

    def _resolve_placement(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        placement_id = _identifier(payload.get("placement_id"), "placement_id")
        matches: list[tuple[dict[str, Any], dict[str, str]]] = []
        for provider in _providers(self.client, PLACEMENT):
            result = self.client.invoke(
                PLACEMENT,
                "get",
                {"placement_id": placement_id},
                provider_instance_id=provider["provider_instance_id"],
            )
            if isinstance(result, Mapping):
                placement = _placement(result)
                matches.append((placement, provider))
        if len(matches) != 1:
            raise PlacementCompileError(
                f"expected one Placement for {placement_id}; found {len(matches)}"
            )
        return matches[0]

    def _resolve_definition(
        self,
        placement: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        subject = _object(placement.get("subject"), "placement.subject")
        exact_ref = str(subject.get("exact_ref") or "").strip()
        selector = subject.get("selector")
        if not exact_ref and not isinstance(selector, Mapping):
            raise PlacementCompileError(
                "placement subject requires exact_ref or selector"
            )
        matches: list[tuple[dict[str, Any], dict[str, str]]] = []
        for provider in _providers(self.client, CATALOG):
            result = self.client.invoke(
                CATALOG,
                "resolve",
                {"exact_ref": exact_ref, "selector": dict(selector or {})},
                provider_instance_id=provider["provider_instance_id"],
            )
            values = result.get("matches") if isinstance(result, Mapping) else []
            for value in values if isinstance(values, list) else []:
                if isinstance(value, Mapping):
                    matches.append((_definition(value), provider))
        matches.sort(
            key=lambda item: (
                item[0]["id"],
                item[0]["version"],
                item[1]["provider_instance_id"],
            )
        )
        if len(matches) != 1:
            raise PlacementCompileError(
                "Placement subject must resolve to exactly one Subagent; "
                f"found {len(matches)}"
            )
        return matches[0]

    def _validate_features(
        self,
        placement: Mapping[str, Any],
        selected_stage_ids: set[str],
    ) -> list[str]:
        stages = _providers(
            self.client,
            STAGE,
            selected_provider_ids=selected_stage_ids,
        )
        available = {
            str(item.get("feature_id") or "")
            for item in stages
            if str(item.get("feature_id") or "")
        }
        warnings: list[str] = []
        for value in placement.get("features") or []:
            feature = _object(value, "placement feature")
            feature_id = _identifier(feature.get("feature_id"), "feature_id")
            if feature_id in available:
                continue
            if feature.get("requirement") == "required":
                raise PlacementCompileError(
                    f"required Placement feature is unavailable: {feature_id}"
                )
            warnings.append(f"advisory feature ignored: {feature_id}")
        return warnings

    def _validate_protocols(
        self,
        placement: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        supported = set(
            _strings(
                _object(
                    definition.get("interfaces"),
                    "subagent.interfaces",
                ).get("protocols")
            )
        )
        available = set(_BUILTIN_PROTOCOLS)
        for provider in _providers(self.client, PROTOCOL):
            protocols = self.client.invoke(
                PROTOCOL,
                "list",
                {},
                provider_instance_id=provider["provider_instance_id"],
            )
            for value in (
                protocols.get("protocols")
                if isinstance(protocols, Mapping)
                else []
            ) or []:
                if isinstance(value, Mapping):
                    available.add(str(value.get("id") or ""))
        result: list[dict[str, Any]] = []
        for value in placement.get("participation") or []:
            item = _object(value, "placement participation")
            ref = str(item.get("protocol_ref") or "").strip()
            protocol_id = ref.removeprefix("tobkiri.protocol/").split("/", 1)[0]
            if ref not in supported:
                raise PlacementCompileError(
                    f"Subagent does not support Placement protocol: {ref}"
                )
            if protocol_id not in available:
                raise PlacementCompileError(
                    f"Placement protocol provider is unavailable: {ref}"
                )
            result.append(dict(item))
        return result

    def _apply_stages(
        self,
        plan: dict[str, Any],
        selected_stage_ids: set[str],
    ) -> dict[str, Any]:
        current = _copy(plan)
        for provider in _providers(
            self.client,
            STAGE,
            selected_provider_ids=selected_stage_ids,
        ):
            if provider.get("feature_id") and not any(
                item.get("feature_id") == provider["feature_id"]
                for item in current.get("features") or []
                if isinstance(item, Mapping)
            ):
                continue
            result = self.client.invoke(
                STAGE,
                "compile",
                {"plan": _copy(current)},
                provider_instance_id=provider["provider_instance_id"],
            )
            fragment = (
                result.get("plan_fragment")
                if isinstance(result, Mapping)
                else None
            )
            if not isinstance(fragment, Mapping):
                raise PlacementCompileError(
                    "Placement compiler stage returned no plan fragment"
                )
            candidate = _merge_stage_fragment(current, fragment)
            _assert_no_authority_widening(current, candidate)
            current = candidate
        return current


def _redeem_authority(client: Any, payload: Mapping[str, Any]) -> None:
    receipt = str(payload.get("_authority_receipt") or "").strip()
    scope = payload.get("_authority_scope")
    if not receipt or not isinstance(scope, Mapping):
        raise PlacementCompileError("Host authority receipt is required")
    scope_arguments = scope.get("arguments")
    compile_arguments = {
        str(key): value
        for key, value in payload.items()
        if key
        not in {
            "_authority_receipt",
            "_authority_scope",
            "_contract_consumer_pack_id",
        }
    }
    if (
        not isinstance(scope_arguments, Mapping)
        or dict(scope_arguments) != compile_arguments
    ):
        raise PlacementCompileError(
            "Host authority Placement arguments do not match"
        )
    expected = dict(scope)
    expected.update(
        {
            "service_pack_id": PACK_ID,
            "operation": "subagent.placement.compile",
            "authority": "subagent.placement.compile",
            "receipt": receipt,
        }
    )
    result = client.invoke(HOST_AUTHORITY, "redeem", expected)
    if (
        not isinstance(result, Mapping)
        or not result.get("authorized")
        or not result.get("redeemed")
    ):
        raise PlacementCompileError(
            "Host authority receipt is invalid or already used"
        )


def create_compile_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the selected Placement compiler operation."""

    compiler = SubagentPlacementCompiler(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "compile":
            raise ValueError(f"unknown Placement compiler operation: {name}")
        return compiler.compile(payload)

    return operation


def create_protocol_catalog(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Expose built-in protocols without fixing them in the Core schema."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        del payload
        if name != "list":
            raise ValueError(f"unknown Subagent protocol operation: {name}")
        return {
            "protocols": [
                {
                    "id": protocol_id,
                    "ref": f"tobkiri.protocol/{protocol_id}/v1",
                }
                for protocol_id in _BUILTIN_PROTOCOLS
            ]
        }

    return operation


def create_core_stage(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Return the identity core stage used to verify chain invariants."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "compile":
            raise ValueError(f"unknown Placement stage operation: {name}")
        plan = _object(payload.get("plan"), "plan")
        return {
            "plan_fragment": {
                "diagnostics": {
                    "core_stage": "passed",
                    "effective_authority_count": len(
                        plan.get("effective_authority") or []
                    ),
                }
            }
        }

    return operation


def _providers(
    client: Any,
    contract_id: str,
    *,
    selected_provider_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    values = client.providers(contract_id)
    providers: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        provider_id = str(value.get("provider_instance_id") or "").strip()
        if not provider_id:
            continue
        if (
            selected_provider_ids is not None
            and provider_id not in selected_provider_ids
        ):
            continue
        providers.append(
            {
                "provider_instance_id": provider_id,
                "source_pack_id": str(value.get("source_pack_id") or ""),
                "content_hash": str(value.get("content_hash") or ""),
                "feature_id": str(value.get("feature_id") or ""),
            }
        )
    providers.sort(
        key=lambda item: (
            item["source_pack_id"],
            item["provider_instance_id"],
            item["content_hash"],
        )
    )
    return providers


def _definition(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _copy(value)
    if data.get("schema_version") != _SCHEMA_DEFINITION:
        raise PlacementCompileError("Subagent definition schema is invalid")
    _identifier(data.get("id"), "subagent.id")
    _identifier(data.get("version"), "subagent.version")
    _object(data.get("runtime"), "subagent.runtime")
    _object(data.get("interfaces"), "subagent.interfaces")
    _object(data.get("ports"), "subagent.ports")
    _object(data.get("requirements"), "subagent.requirements")
    _object(data.get("behavior"), "subagent.behavior")
    return data


def _placement(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _copy(value)
    if data.get("schema_version") != _SCHEMA_PLACEMENT:
        raise PlacementCompileError("Subagent Placement schema is invalid")
    _identifier(data.get("id"), "placement.id")
    _object(data.get("subject"), "placement.subject")
    _object(data.get("role"), "placement.role")
    _object(data.get("governance"), "placement.governance")
    _bindings(data)
    return data


def _bindings(placement: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in placement.get("bindings") or []:
        binding = _object(value, "placement binding")
        slot = str(binding.get("slot") or "").strip()
        if not slot or slot in seen:
            raise PlacementCompileError(
                "Placement binding slots must be non-empty and unique"
            )
        if slot not in _STANDARD_BINDINGS and not binding.get("provider_ref"):
            raise PlacementCompileError(
                f"unknown binding requires a provider_ref: {slot}"
            )
        seen.add(slot)
        result.append(dict(binding))
    result.sort(key=lambda item: str(item["slot"]))
    return result


def _binding_projection(
    bindings: Iterable[Mapping[str, Any]],
    slot: str,
) -> dict[str, Any]:
    for binding in bindings:
        if str(binding.get("slot") or "") == slot:
            return dict(binding)
    return {}


def _runtime_kind(definition: Mapping[str, Any]) -> str:
    runtime = _object(definition.get("runtime"), "subagent.runtime")
    driver = str(runtime.get("driver_key") or "").lower()
    supported = {
        "agent_run",
        "utility_model_call",
        "human_task",
        "remote_agent",
        "composite_team",
    }
    if driver in supported:
        return driver
    if "utility" in driver:
        return "utility_model_call"
    if "remote" in driver or "a2a" in driver:
        return "remote_agent"
    if "human" in driver:
        return "human_task"
    if "team" in driver or "composite" in driver:
        return "composite_team"
    return "agent_run"


def _validate_binding_policies(
    definition: Mapping[str, Any],
    bindings: Iterable[Mapping[str, Any]],
) -> None:
    ports = _object(definition.get("ports"), "subagent.ports")
    provided = {
        str(binding.get("slot") or "")
        for binding in bindings
    }
    for slot, raw in ports.items():
        port = _object(raw, f"subagent.ports.{slot}")
        policy = str(port.get("binding_policy") or "")
        candidates = {
            value for value in provided if value == slot or value.startswith(slot + ".")
        }
        if policy in {"placement", "placement_required"} and not candidates:
            raise PlacementCompileError(
                f"Placement requires binding for Subagent port: {slot}"
            )
        if policy == "forbidden" and candidates:
            raise PlacementCompileError(
                f"Placement binds forbidden Subagent port: {slot}"
            )


def _compile_authority(
    definition: Mapping[str, Any],
    placement: Mapping[str, Any],
    capability_plan: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[str]:
    requirements = _object(
        definition.get("requirements"),
        "subagent.requirements",
    )
    implementation = set(_strings(requirements.get("required_capabilities")))
    implementation.update(_strings(requirements.get("optional_capabilities")))
    layers = [implementation]
    for source_name, source in (
        ("placement governance", placement.get("governance")),
        ("profile_policy", payload.get("profile_policy")),
        ("workspace_policy", payload.get("workspace_policy")),
        ("host_policy", payload.get("host_policy")),
        ("task_grant", payload.get("task_grant")),
    ):
        if source is None:
            continue
        layer = _object(source, source_name)
        if "allowed_capabilities" in layer:
            layers.append(
                set(_strings(layer.get("allowed_capabilities")))
            )
    granted = _capability_grants(capability_plan)
    layers.append(granted)
    effective = set.intersection(*layers) if layers else set()
    denied: set[str] = set(_strings(requirements.get("denied_capabilities")))
    for source in (
        placement.get("governance"),
        payload.get("profile_policy"),
        payload.get("workspace_policy"),
        payload.get("host_policy"),
        payload.get("task_grant"),
    ):
        if isinstance(source, Mapping):
            denied.update(_strings(source.get("denied_capabilities")))
    effective.difference_update(denied)
    required = set(_strings(requirements.get("required_capabilities")))
    if not required.issubset(effective):
        missing = ", ".join(sorted(required - effective))
        raise PlacementCompileError(
            f"required Subagent capabilities are unavailable: {missing}"
        )
    return sorted(effective)


def _compile_budgets(
    definition: Mapping[str, Any],
    placement: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, int | float]:
    keys = (
        "maximum_parallel_runs",
        "maximum_steps",
        "maximum_tool_calls",
        "maximum_cost",
        "timeout_seconds",
        "context_token_budget",
    )
    sources: list[Mapping[str, Any]] = [
        _object(definition.get("limits") or {}, "subagent.limits"),
        _object(placement.get("governance"), "placement.governance"),
    ]
    for value in (
        payload.get("profile_policy"),
        payload.get("workspace_policy"),
        payload.get("host_policy"),
        payload.get("task_grant"),
    ):
        if isinstance(value, Mapping):
            sources.append(value)
    result: dict[str, int | float] = {}
    for key in keys:
        values = [
            float(source[key])
            for source in sources
            if key in source and float(source[key]) >= 0
        ]
        if values:
            selected = min(values)
            result[key] = (
                int(selected)
                if all(float(value).is_integer() for value in values)
                else selected
            )
    return result


def _strictest_approval(
    definition: Mapping[str, Any],
    placement: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, str]:
    values: list[str] = []
    for source in (
        definition.get("governance"),
        placement.get("governance"),
        payload.get("profile_policy"),
        payload.get("workspace_policy"),
        payload.get("host_policy"),
        payload.get("task_grant"),
    ):
        if not isinstance(source, Mapping):
            continue
        approval = source.get("approval")
        if isinstance(approval, Mapping):
            values.append(str(approval.get("minimum") or "auto"))
        elif source.get("minimum_approval"):
            values.append(str(source["minimum_approval"]))
    normalized = [
        value if value in _APPROVAL_RANK else "deny" for value in values
    ]
    strictest = max(normalized or ["auto"], key=_APPROVAL_RANK.__getitem__)
    return {"minimum": strictest}


def _compile_enforcement(
    definition: Mapping[str, Any],
    placement: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, str]:
    del definition
    host_enforcement = payload.get("host_enforcement")
    if not isinstance(host_enforcement, Mapping):
        raise PlacementCompileError("Host enforcement receipt is required")
    result: dict[str, str] = {}
    for key, raw in host_enforcement.items():
        value = str(raw)
        if value not in _ENFORCEMENT_RANK:
            raise PlacementCompileError(
                f"unknown enforcement mode for {key}: {value}"
            )
        result[str(key)] = value
    required = placement.get("required_enforcement")
    for key, raw in required.items() if isinstance(required, Mapping) else []:
        value = str(raw)
        actual = result.get(str(key), "behavioral_only")
        if _ENFORCEMENT_RANK.get(actual, -1) < _ENFORCEMENT_RANK.get(value, 99):
            raise PlacementCompileError(
                f"required enforcement is unavailable: {key}={value}"
            )
    return dict(sorted(result.items()))


def _compile_behavior(
    definition: Mapping[str, Any],
    placement: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    base = _object(definition.get("behavior"), "subagent.behavior")
    role = _object(
        _object(placement.get("role"), "placement.role").get("behavior") or {},
        "placement.role.behavior",
    )
    layers = [
        {"kind": "host_invariant", "value": payload.get("host_invariant") or {}},
        {"kind": "pack_base", "value": base},
        {"kind": "placement_role", "value": role},
        {"kind": "required_skills", "value": _skill_bindings(placement)},
        {
            "kind": "project_instructions",
            "value": payload.get("project_instructions") or [],
        },
        {
            "kind": "task_instructions",
            "value": payload.get("task_instructions") or [],
        },
        {
            "kind": "output_contract",
            "value": _object(placement.get("role"), "placement.role").get(
                "completion_contracts"
            )
            or [],
        },
    ]
    return {
        "layers": [
            {**layer, "sha256": _sha(layer["value"])}
            for layer in layers
            if layer["value"]
        ]
    }


def _skill_bindings(placement: Mapping[str, Any]) -> list[str]:
    for binding in placement.get("bindings") or []:
        if isinstance(binding, Mapping) and binding.get("slot") == "skills":
            return sorted(
                set(
                    _strings(binding.get("required"))
                    + _strings(binding.get("optional"))
                )
            )
    return []


def _capability_grants(plan: Mapping[str, Any]) -> set[str]:
    return set(_strings(plan.get("effective_capabilities")))


def _capability_ref(plan: Mapping[str, Any]) -> str:
    return str(plan.get("plan_id") or "").strip()


def _capability_revision(plan: Mapping[str, Any]) -> str:
    return str(plan.get("digest") or "").strip()


def _selected_provider_ids(
    plan: Mapping[str, Any],
    contract_id: str,
) -> set[str]:
    selections = plan.get("provider_selections")
    if not isinstance(selections, Mapping):
        return set()
    return set(_strings(selections.get(contract_id)))


def _merge_stage_fragment(
    plan: Mapping[str, Any],
    fragment: Mapping[str, Any],
) -> dict[str, Any]:
    allowed = {
        "diagnostics",
        "warnings",
        "effective_authority",
        "budgets",
        "approval",
        "enforcement",
        "behavior",
        "protocol_bindings",
    }
    unknown = set(fragment) - allowed
    if unknown:
        raise PlacementCompileError(
            "Placement stage returned forbidden fields: "
            + ", ".join(sorted(unknown))
        )
    result = _copy(plan)
    for key, value in fragment.items():
        if key in {"diagnostics", "warnings"}:
            current = result.get(key)
            if isinstance(current, list) and isinstance(value, list):
                result[key] = [*current, *_copy(value)]
            elif isinstance(current, Mapping) and isinstance(value, Mapping):
                result[key] = {**current, **_copy(value)}
            else:
                result[key] = _copy(value)
        else:
            result[key] = _copy(value)
    return result


def _assert_no_authority_widening(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    before_authority = set(_strings(before.get("effective_authority")))
    after_authority = set(_strings(after.get("effective_authority")))
    if not after_authority.issubset(before_authority):
        raise PlacementCompileError("Placement stage widened authority")
    before_budgets = before.get("budgets")
    after_budgets = after.get("budgets")
    if isinstance(before_budgets, Mapping) and isinstance(after_budgets, Mapping):
        for key, value in after_budgets.items():
            if key in before_budgets and float(value) > float(before_budgets[key]):
                raise PlacementCompileError(
                    f"Placement stage widened budget: {key}"
                )
    before_approval = str(
        _object(before.get("approval") or {}, "approval").get("minimum")
        or "auto"
    )
    after_approval = str(
        _object(after.get("approval") or {}, "approval").get("minimum")
        or "auto"
    )
    if _APPROVAL_RANK.get(after_approval, -1) < _APPROVAL_RANK.get(
        before_approval,
        99,
    ):
        raise PlacementCompileError("Placement stage weakened approval")
    if after.get("enforcement") != before.get("enforcement"):
        raise PlacementCompileError(
            "Placement stage changed Host enforcement"
        )
    before_protocols = {
        _sha(value)
        for value in before.get("protocol_bindings") or []
        if isinstance(value, Mapping)
    }
    after_protocols = {
        _sha(value)
        for value in after.get("protocol_bindings") or []
        if isinstance(value, Mapping)
    }
    if not after_protocols.issubset(before_protocols):
        raise PlacementCompileError(
            "Placement stage widened protocol bindings"
        )
    if after.get("behavior") != before.get("behavior"):
        raise PlacementCompileError(
            "Placement stage changed sealed behavior"
        )


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlacementCompileError(f"{field} must be an object")
    return dict(value)


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise PlacementCompileError(f"{field} is invalid")
    return text


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(
        value,
        (str, bytes, Mapping),
    ):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
