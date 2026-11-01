"""Compile Team Placement Maps and pin runtime assignments."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping

from .compiler import PlacementCompileError


SCHEMA_PLACEMENT_MAP = "tobkiri.subagent-placement-map/v1"
SCHEMA_PLACEMENT_PATCH = "tobkiri.subagent-placement-patch/v1"
SCHEMA_EFFECTIVE_TOPOLOGY = "tobkiri.effective-subagent-topology/v1"
SCHEMA_RUNTIME_ASSIGNMENT = "tobkiri.subagent-runtime-assignment/v1"
SCHEMA_EFFECTIVE_PLAN = "tobkiri.effective-subagent/v1"
SCHEMA_SUBAGENT = "tobkiri.subagent/v1"
_APPROVAL_RANK = {"auto": 0, "confirm": 1, "deny": 2}


def content_hash(value: Any) -> str:
    """Return the canonical JSON content hash for a topology resource."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _without_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): deepcopy(item)
        for key, item in value.items()
        if key != "content_hash"
    }


def _validate_hash(value: Mapping[str, Any]) -> None:
    expected = str(value.get("content_hash") or "")
    if expected and content_hash(_without_hash(value)) != expected:
        raise PlacementCompileError("Placement Map content hash does not match")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlacementCompileError(f"{field} must be an object")
    return dict(value)


def _items(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PlacementCompileError(f"{field} must be an array")
    result = []
    for item in value:
        result.append(_object(item, field + " item"))
    return result


def validate_placement_map(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one desired Team topology without executing it."""

    placement_map = deepcopy(dict(value))
    if placement_map.get("schema_version") != SCHEMA_PLACEMENT_MAP:
        raise PlacementCompileError("Placement Map schema is invalid")
    map_id = str(placement_map.get("id") or "").strip()
    if not map_id:
        raise PlacementCompileError("Placement Map id is required")
    revision = placement_map.get("revision")
    if not isinstance(revision, int) or revision < 1:
        raise PlacementCompileError("Placement Map revision must be positive")
    main = _object(placement_map.get("main"), "placement_map.main")
    main_id = str(main.get("placement_id") or "").strip()
    if not main_id:
        raise PlacementCompileError("Placement Map Main placement is required")
    if str(main.get("mode") or "") not in {
        "interactive",
        "coordinator",
        "facade",
    }:
        raise PlacementCompileError("Placement Map Main mode is invalid")
    placements = _items(
        placement_map.get("placements"),
        "placement_map.placements",
    )
    placement_ids: list[str] = []
    main_count = 0
    for placement in placements:
        placement_id = str(placement.get("placement_id") or "").strip()
        if not placement_id or placement_id in placement_ids:
            raise PlacementCompileError(
                "Placement Map members require unique placement_id values"
            )
        kind = str(placement.get("kind") or "").strip()
        if kind not in {"main", "subagent"}:
            raise PlacementCompileError(
                f"Placement Map member kind is invalid: {placement_id}"
            )
        if kind == "main":
            main_count += 1
            if placement_id != main_id:
                raise PlacementCompileError(
                    "Placement Map Main identity does not match member list"
                )
        placement_ids.append(placement_id)
    if main_count != 1:
        raise PlacementCompileError(
            "Placement Map requires exactly one logical Main Agent"
        )
    if main_id not in placement_ids:
        raise PlacementCompileError(
            "Placement Map Main placement is not a member"
        )
    team_bindings = _items(
        placement_map.get("team_bindings", []),
        "placement_map.team_bindings",
    )
    slots: set[str] = set()
    for binding in team_bindings:
        slot = str(binding.get("slot") or "").strip()
        if not slot or slot in slots:
            raise PlacementCompileError(
                "Team bindings require unique non-empty slots"
            )
        if not binding.get("provider_ref"):
            raise PlacementCompileError(
                f"Team binding requires provider_ref: {slot}"
            )
        slots.add(slot)
    _object(
        placement_map.get("exposed_ports", {}),
        "placement_map.exposed_ports",
    )
    _object(
        placement_map.get("governance", {}),
        "placement_map.governance",
    )
    _validate_hash(placement_map)
    return placement_map


def _plan_index(
    plans: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in plans:
        plan = deepcopy(dict(value))
        if plan.get("schema_version") != SCHEMA_EFFECTIVE_PLAN:
            raise PlacementCompileError("Effective Subagent Plan is invalid")
        expected_hash = str(plan.get("plan_hash") or "")
        unsigned = {
            key: item for key, item in plan.items() if key != "plan_hash"
        }
        if not expected_hash or content_hash(unsigned) != expected_hash:
            raise PlacementCompileError(
                "Effective Subagent Plan hash does not match"
            )
        placement = _object(plan.get("placement"), "plan.placement")
        placement_id = str(placement.get("id") or "").strip()
        if not placement_id or placement_id in result:
            raise PlacementCompileError(
                "Effective Plans require unique Placement identities"
            )
        result[placement_id] = plan
    return result


def _constrain_plan_by_team(
    plan: Mapping[str, Any],
    governance: Mapping[str, Any],
) -> dict[str, Any]:
    result = deepcopy(dict(plan))
    budgets = _object(result.get("budgets", {}), "plan.budgets")
    for key in (
        "maximum_parallel_runs",
        "maximum_steps",
        "maximum_tool_calls",
        "maximum_cost",
        "timeout_seconds",
        "context_token_budget",
    ):
        team_value = governance.get(key)
        if not isinstance(team_value, (int, float)) or team_value < 0:
            continue
        plan_value = budgets.get(key)
        budgets[key] = (
            min(plan_value, team_value)
            if isinstance(plan_value, (int, float))
            else team_value
        )
    result["budgets"] = budgets
    plan_approval = _object(result.get("approval", {}), "plan.approval")
    team_approval = governance.get("approval")
    if isinstance(team_approval, Mapping):
        current = str(plan_approval.get("minimum") or "auto")
        candidate = str(team_approval.get("minimum") or "auto")
        strictest = max(
            (
                current if current in _APPROVAL_RANK else "deny",
                candidate if candidate in _APPROVAL_RANK else "deny",
            ),
            key=_APPROVAL_RANK.__getitem__,
        )
        plan_approval["minimum"] = strictest
    result["approval"] = plan_approval
    result["plan_hash"] = content_hash(
        {key: item for key, item in result.items() if key != "plan_hash"}
    )
    return result


def compile_placement_map(
    placement_map: Mapping[str, Any],
    plans: Iterable[Mapping[str, Any]],
    *,
    registry_revision: str,
) -> dict[str, Any]:
    """Compile desired Team topology into one immutable Effective Topology."""

    desired = validate_placement_map(placement_map)
    if not str(registry_revision or "").strip():
        raise PlacementCompileError("registry_revision is required")
    indexed = _plan_index(plans)
    member_ids = [
        str(item["placement_id"]) for item in desired["placements"]
    ]
    missing = sorted(set(member_ids) - set(indexed))
    extra = sorted(set(indexed) - set(member_ids))
    if missing or extra:
        raise PlacementCompileError(
            "Placement Map and Effective Plans differ"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; extra: {', '.join(extra)}" if extra else "")
        )
    governance = _object(
        desired.get("governance", {}),
        "placement_map.governance",
    )
    constrained = {
        placement_id: _constrain_plan_by_team(
            indexed[placement_id],
            governance,
        )
        for placement_id in member_ids
    }
    topology = {
        "schema_version": SCHEMA_EFFECTIVE_TOPOLOGY,
        "placement_map": {
            "id": desired["id"],
            "revision": int(desired["revision"]),
            "content_hash": str(
                desired.get("content_hash")
                or content_hash(_without_hash(desired))
            ),
        },
        "main": deepcopy(desired["main"]),
        "members": [
            {
                **deepcopy(member),
                "effective_plan_hash": constrained[
                    str(member["placement_id"])
                ]["plan_hash"],
            }
            for member in desired["placements"]
        ],
        "plans": constrained,
        "team_bindings": deepcopy(desired.get("team_bindings", [])),
        "exposed_ports": deepcopy(desired.get("exposed_ports", {})),
        "governance": governance,
        "registry_revision": str(registry_revision),
    }
    topology["topology_hash"] = content_hash(topology)
    return topology


def apply_placement_patch(
    current: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply an explicit revision-checked Placement Map patch.

    Runs already assigned to the previous revision are intentionally untouched.
    """

    desired = validate_placement_map(current)
    if patch.get("schema_version") != SCHEMA_PLACEMENT_PATCH:
        raise PlacementCompileError("Placement Patch schema is invalid")
    expected_revision = str(patch.get("expected_revision") or "")
    current_revision = str(
        desired.get("content_hash")
        or content_hash(_without_hash(desired))
    )
    if expected_revision != current_revision:
        raise PlacementCompileError("Placement Patch revision is stale")
    operations = _items(patch.get("operations"), "placement_patch.operations")
    updated = deepcopy(desired)
    members = {
        str(item["placement_id"]): deepcopy(item)
        for item in updated["placements"]
    }
    bindings = {
        str(item["slot"]): deepcopy(item)
        for item in updated.get("team_bindings", [])
    }
    for operation in operations:
        action = str(operation.get("op") or "")
        target = str(operation.get("target") or "")
        key = str(operation.get("key") or "").strip()
        value = operation.get("value")
        if action not in {"add", "replace", "remove"}:
            raise PlacementCompileError("Placement Patch operation is invalid")
        if target == "placement":
            table = members
            identity_field = "placement_id"
        elif target == "team_binding":
            table = bindings
            identity_field = "slot"
        elif target == "main":
            if action != "replace" or not isinstance(value, Mapping):
                raise PlacementCompileError(
                    "Main Agent changes require a complete replacement"
                )
            updated["main"] = deepcopy(dict(value))
            continue
        else:
            raise PlacementCompileError("Placement Patch target is invalid")
        if not key:
            raise PlacementCompileError("Placement Patch key is required")
        if action == "remove":
            if key not in table:
                raise PlacementCompileError(
                    f"Placement Patch target does not exist: {key}"
                )
            del table[key]
            continue
        if not isinstance(value, Mapping):
            raise PlacementCompileError(
                "Placement Patch value must be an object"
            )
        normalized = deepcopy(dict(value))
        if str(normalized.get(identity_field) or "") != key:
            raise PlacementCompileError(
                f"Placement Patch {identity_field} does not match key"
            )
        if action == "add" and key in table:
            raise PlacementCompileError(
                f"Placement Patch target already exists: {key}"
            )
        if action == "replace" and key not in table:
            raise PlacementCompileError(
                f"Placement Patch target does not exist: {key}"
            )
        table[key] = normalized
    updated["placements"] = list(members.values())
    updated["team_bindings"] = list(bindings.values())
    updated["revision"] = int(updated["revision"]) + 1
    updated["previous_revision"] = current_revision
    updated.pop("content_hash", None)
    validate_placement_map(updated)
    updated["content_hash"] = content_hash(updated)
    return updated


def create_runtime_assignment(
    plan: Mapping[str, Any],
    *,
    run_id: str,
    root_scope_id: str,
    parent_run_id: str | None = None,
    root_run_id: str | None = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Pin one new runtime instance to an immutable Effective Plan."""

    indexed = _plan_index([plan])
    pinned = next(iter(indexed.values()))
    placement = _object(pinned.get("placement"), "plan.placement")
    clean_run_id = str(run_id or "").strip()
    clean_scope = str(root_scope_id or "").strip()
    if not clean_run_id or not clean_scope:
        raise PlacementCompileError(
            "Runtime Assignment requires run_id and root_scope_id"
        )
    assignment = {
        "schema_version": SCHEMA_RUNTIME_ASSIGNMENT,
        "assignment_id": str(instance_id or f"assignment:{clean_run_id}"),
        "run_id": clean_run_id,
        "instance_id": str(instance_id or f"subagent:{clean_run_id}"),
        "agent_kind": str(pinned.get("agent_kind") or "subagent"),
        "runtime_kind": str(pinned.get("runtime_kind") or "agent_run"),
        "placement_id": str(placement.get("id") or ""),
        "placement_revision": str(placement.get("revision") or ""),
        "placement_map_id": str(placement.get("map_id") or ""),
        "effective_plan_hash": str(pinned["plan_hash"]),
        "root_scope_id": clean_scope,
        "parent_run_id": str(parent_run_id or "") or None,
        "root_run_id": str(root_run_id or clean_run_id),
        "protocol_membership": [
            str(item.get("protocol_ref"))
            for item in pinned.get("protocol_bindings", [])
            if isinstance(item, Mapping) and item.get("protocol_ref")
        ],
        "state": "assigned",
    }
    assignment["assignment_hash"] = content_hash(assignment)
    return assignment


def adapt_remote_agent_card(
    card: Mapping[str, Any],
    *,
    pack_id: str,
    minimum_trust: str = "verified",
) -> dict[str, Any]:
    """Convert an opaque remote Agent Card into a Subagent Definition.

    The adapter intentionally records remote enforcement as attestation. A
    Placement that requires host enforcement will therefore fail closed in the
    normal compiler.
    """

    remote = _object(card, "remote_agent_card")
    agent_id = str(
        remote.get("id")
        or remote.get("name")
        or remote.get("agent_id")
        or ""
    ).strip()
    endpoint = str(
        remote.get("url")
        or remote.get("endpoint")
        or ""
    ).strip()
    if not agent_id or not endpoint:
        raise PlacementCompileError(
            "Remote Agent Card requires identity and endpoint"
        )
    capabilities = remote.get("capabilities")
    capability_values = (
        [
            str(key)
            for key, enabled in capabilities.items()
            if enabled
        ]
        if isinstance(capabilities, Mapping)
        else [
            str(value)
            for value in capabilities or []
            if str(value).strip()
        ]
    )
    skills = remote.get("skills")
    produced = []
    for skill in skills if isinstance(skills, list) else []:
        if isinstance(skill, Mapping):
            produced.extend(
                str(value)
                for value in skill.get("produces", [])
                if str(value).strip()
            )
    return {
        "schema_version": SCHEMA_SUBAGENT,
        "kind": "subagent",
        "id": f"{pack_id}.{agent_id}",
        "version": str(remote.get("version") or "1.0.0"),
        "display_name": str(
            remote.get("display_name")
            or remote.get("name")
            or agent_id
        ),
        "description": str(
            remote.get("description")
            or "Opaque remote Subagent."
        ),
        "runtime": {
            "driver_contract": "rumi.service.subagent.runtime.v1",
            "driver_key": "remote_agent",
            "supported_isolation": ["remote"],
            "state_modes": ["ephemeral", "resumable"],
            "endpoint": endpoint,
        },
        "interfaces": {
            "accepts": [
                "tobkiri.work-item/v1",
            ],
            "produces": sorted(set(produced))
            or ["tobkiri.agent-result/v1"],
            "protocols": [
                "tobkiri.protocol/delegate/v1",
                "tobkiri.protocol/handoff/v1",
                "tobkiri.protocol/agent-tool/v1",
            ],
        },
        "ports": {
            "model": {"binding_policy": "fixed"},
            "tools": {"binding_policy": "fixed"},
            "skills": {"binding_policy": "fixed"},
            "memory": {"binding_policy": "forbidden"},
            "workspace": {"binding_policy": "forbidden"},
        },
        "requirements": {
            "model_capabilities": capability_values,
            "runtime_capabilities": ["network.remote_agent"],
            "required_capabilities": [],
            "optional_capabilities": [],
            "denied_capabilities": [],
            "minimum_pack_trust": minimum_trust,
        },
        "behavior": {
            "placement_role_instructions": "optional",
        },
        "enforcement": {
            "remote_internal_tools": "remote_attested",
            "system_prompt": "remote_attested",
            "output_schema": "host_validated",
        },
        "remote_card_hash": content_hash(remote),
    }


def export_topology_as_subagent(
    topology: Mapping[str, Any],
    *,
    subagent_id: str,
    display_name: str,
) -> dict[str, Any]:
    """Export a compiled Team as one composite Subagent contract."""

    value = _object(topology, "effective_topology")
    if value.get("schema_version") != SCHEMA_EFFECTIVE_TOPOLOGY:
        raise PlacementCompileError("Effective topology schema is invalid")
    expected = str(value.get("topology_hash") or "")
    unsigned = {
        key: item for key, item in value.items() if key != "topology_hash"
    }
    if not expected or content_hash(unsigned) != expected:
        raise PlacementCompileError("Effective topology hash does not match")
    ports = _object(value.get("exposed_ports", {}), "exposed_ports")
    return {
        "schema_version": SCHEMA_SUBAGENT,
        "kind": "subagent",
        "id": str(subagent_id),
        "version": "1.0.0",
        "display_name": str(display_name),
        "description": "Composite Subagent backed by a compiled Team topology.",
        "runtime": {
            "driver_contract": "rumi.service.subagent.runtime.v1",
            "driver_key": "composite_team",
            "supported_isolation": ["process", "sandbox", "remote"],
            "state_modes": ["ephemeral", "resumable"],
        },
        "interfaces": {
            "accepts": list(ports.get("input") or []),
            "produces": list(ports.get("output") or []),
            "protocols": [
                "tobkiri.protocol/delegate/v1",
                "tobkiri.protocol/agent-tool/v1",
                "tobkiri.protocol/team-member/v1",
            ],
        },
        "ports": {
            "model": {"binding_policy": "forbidden"},
            "tools": {"binding_policy": "forbidden"},
            "skills": {"binding_policy": "forbidden"},
            "memory": {"binding_policy": "fixed"},
            "workspace": {"binding_policy": "fixed"},
        },
        "requirements": {
            "model_capabilities": [],
            "runtime_capabilities": ["subagent.composite_team"],
            "required_capabilities": [],
            "optional_capabilities": [],
            "denied_capabilities": [],
            "minimum_pack_trust": "verified",
        },
        "behavior": {
            "placement_role_instructions": "optional",
        },
        "enforcement": {
            "team_topology": "host_enforced",
            "output_schema": "host_validated",
        },
        "topology_hash": expected,
    }


def create_topology_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Expose deterministic topology compilation without side effects."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name == "validate":
            return {
                "placement_map": validate_placement_map(
                    _object(payload.get("placement_map"), "placement_map")
                )
            }
        if name == "compile":
            return compile_placement_map(
                _object(payload.get("placement_map"), "placement_map"),
                payload.get("plans")
                if isinstance(payload.get("plans"), list)
                else [],
                registry_revision=str(
                    payload.get("registry_revision") or ""
                ),
            )
        raise ValueError(f"unknown topology operation: {name}")

    return operation


def create_patch_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Expose revision-checked desired-topology patches."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "apply":
            raise ValueError(f"unknown Placement Patch operation: {name}")
        return apply_placement_patch(
            _object(payload.get("current"), "current"),
            _object(payload.get("patch"), "patch"),
        )

    return operation


def create_assignment_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Expose immutable runtime assignment creation."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "assign":
            raise ValueError(f"unknown runtime assignment operation: {name}")
        return create_runtime_assignment(
            _object(payload.get("plan"), "plan"),
            run_id=str(payload.get("run_id") or ""),
            root_scope_id=str(payload.get("root_scope_id") or ""),
            parent_run_id=(
                str(payload.get("parent_run_id"))
                if payload.get("parent_run_id")
                else None
            ),
            root_run_id=(
                str(payload.get("root_run_id"))
                if payload.get("root_run_id")
                else None
            ),
            instance_id=(
                str(payload.get("instance_id"))
                if payload.get("instance_id")
                else None
            ),
        )

    return operation
