"""Finite per-Operation caller expansion for Function-scoped Profile edges.

Profile edges name a caller Function while captured Authority is exercised by
exact per-Operation ``FunctionPrincipal`` values.  A caller with several
Operations — for example ``tobkiri.workflow.provider`` — must expand to one
authority edge per admitted Operation, and a caller declared only in an
admitted Pack manifest (outgoing edges, never bound as a target) must resolve
without silently picking a principal or hiding an undeclared caller.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityScope,
    FunctionPrincipal,
)
from core_runtime.bootstrap.production_v4 import _manifest_caller_principals
from ecosystem.defaultspack.domain.runtime_v4 import ProfileResolutionDenied
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    RuntimeSurfaceError,
    RuntimeSurfaceErrorCode,
    _normalized_plan_bindings,
)
from tobkiri_host.composition import AuthorityCeilings, HostV4Composition
from tobkiri_host.errors import ResolutionError
from tests.conformance_support.host_profile import captured_host_profile
from tests.test_tobkiri_host_v4_composition import _capture


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_MANIFEST_PATH = (
    ROOT / "ecosystem/defaultspack/v4/packs/tobkiri_workflow_pack.pack.v4.json"
)
WORKFLOW_FUNCTION = "tobkiri.workflow.provider"
TARGET_FUNCTION = "rumi_provider_registry_pack.provider-registry.resource"
TARGET_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
TARGET_OPERATION = "rumi_provider_registry_pack.provider-registry-resource"


def _digest(seed: str) -> str:
    """Return a deterministic sha256-shaped digest for fixtures."""

    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _manifest(
    artifact_seed: str,
    functions: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the digest-pinned manifest shape used by admitted Packs."""

    return {
        "pack": {"artifact_digest": _digest(artifact_seed)},
        "functions": [
            {
                "id": function["id"],
                "implementation_digest": _digest(function["impl"]),
                "contract_revision_digest": _digest(function["rev"]),
                "operations": list(function["ops"]),
            }
            for function in functions
        ],
        "contracts": [
            {"revision_digest": _digest(function["rev"])}
            for function in functions
        ],
    }


def _principal_dict(
    artifact_seed: str,
    function: Mapping[str, Any],
    operation_id: str,
) -> dict[str, str]:
    """Return the five-field FunctionPrincipal record for one Operation."""

    return {
        "parent_artifact_digest": _digest(artifact_seed),
        "function_implementation_digest": _digest(function["impl"]),
        "function_id": str(function["id"]),
        "contract_revision_digest": _digest(function["rev"]),
        "operation_id": operation_id,
    }


def _target_function() -> dict[str, Any]:
    return {"id": "target.single", "impl": "t-impl", "rev": "t-rev", "ops": ["t.op"]}


def _caller_function(ops=("caller.op1", "caller.op2", "caller.op3")) -> dict[str, Any]:
    return {"id": "caller.multi", "impl": "c-impl", "rev": "c-rev", "ops": list(ops)}


def _edge(caller_function_id: str) -> dict[str, str]:
    return {
        "caller_function_id": caller_function_id,
        "target_provider_id": "target.single",
        "contract_id": "contract.target",
        "operation_id": "t.op",
    }


def _binding(
    caller_function_id: str,
    target_record: Mapping[str, str],
    *,
    pack_id: str = "target-pack",
    contract_id: str = "contract.target",
    operation_id: str = "t.op",
) -> dict[str, Any]:
    return {
        "caller_function_id": caller_function_id,
        "contract_id": contract_id,
        "operation_id": operation_id,
        "pack_id": pack_id,
        "authority_reference": "authority:reference:1",
        "function_principal": dict(target_record),
    }


def _snapshot(
    *,
    bindings: list[Mapping[str, Any]],
    edges: list[Mapping[str, str]],
    packs: Mapping[str, Mapping[str, Any]],
    effective_pack_ids: list[str],
) -> SimpleNamespace:
    """Build the minimal projection snapshot consumed by the surface."""

    resolved = SimpleNamespace(
        plan={"bindings": bindings},
        lock={
            "effective_set": [
                {"identity": pack_id, "artifact_digest": packs[pack_id]["pack"]["artifact_digest"]} for pack_id in effective_pack_ids
            ]
        },
        profile={
            "shell": {"pack_id": "shell-pack"},
            "requested_edges": edges,
        },
    )
    return SimpleNamespace(
        active=SimpleNamespace(resolved=resolved),
        catalog=SimpleNamespace(packs=dict(packs)),
    )


def _base_snapshot(
    *,
    caller_ops=("caller.op1", "caller.op2", "caller.op3"),
    caller_function_id: str = "caller.multi",
    caller_manifest_present: bool = True,
) -> SimpleNamespace:
    """Snapshot with one edge from a caller to one signed target binding."""

    caller = _caller_function(caller_ops)
    caller = {**caller, "id": caller_function_id}
    shell = {"id": "shell.main", "impl": "s-impl", "rev": "s-rev", "ops": ["shell.op"]}
    target = _target_function()
    target_record = _principal_dict("target-art", target, "t.op")
    bindings = [_binding(caller_function_id, target_record)]
    packs = {
        "shell-pack": _manifest("shell-art", [shell]),
        "target-pack": _manifest("target-art", [target]),
    }
    effective = ["shell-pack", "target-pack"]
    if caller_manifest_present:
        packs["caller-pack"] = _manifest("caller-art", [caller])
        effective.append("caller-pack")
    return _snapshot(
        bindings=bindings,
        edges=[_edge(caller_function_id)],
        packs=packs,
        effective_pack_ids=effective,
    )


def test_manifest_caller_principals_cover_only_admitted_declarations() -> None:
    """Only admitted Pack manifests may declare caller principals."""

    caller = _caller_function()
    catalog = SimpleNamespace(
        packs={
            "admitted": _manifest("caller-art", [caller]),
            "stray": _manifest("stray-art", [_caller_function(("stray.op",))]),
        }
    )
    catalog.packs["stray"]["functions"][0]["id"] = "stray.fn"
    declared = _manifest_caller_principals(catalog, {"admitted"})
    assert set(declared) == {"caller.multi"}
    principals = {
        (principal.function_id, principal.operation_id): principal
        for principal in declared["caller.multi"]
    }
    assert set(principals) == {
        ("caller.multi", operation)
        for operation in ("caller.op1", "caller.op2", "caller.op3")
    }
    for principal in principals.values():
        assert principal.parent_artifact_digest == _digest("caller-art")


def test_manifest_caller_principals_dedup_and_reject_malformed() -> None:
    """Identical declarations dedup; malformed ones stay denied."""

    caller = _caller_function(("caller.op1",))
    catalog = SimpleNamespace(
        packs={"admitted": _manifest("caller-art", [caller])}
    )
    declared = _manifest_caller_principals(catalog, {"admitted", "admitted"})
    assert len(declared["caller.multi"]) == 1

    malformed = SimpleNamespace(
        packs={
            "admitted": {
                "pack": {"artifact_digest": _digest("caller-art")},
                "functions": [{"id": "caller.multi"}],
            }
        }
    )
    with pytest.raises(AuthorityDenied):
        _manifest_caller_principals(malformed, {"admitted"})


def test_composition_expands_declared_caller_operations(tmp_path: Path) -> None:
    """A declared extra caller Operation must be covered by a second edge."""

    _, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    edge = next(
        item
        for item in resolved.profile["requested_edges"]
        if item["caller_function_id"] != resolved.profile["shell"]["pack_id"]
    )
    caller_function_id = edge["caller_function_id"]
    base_principal = next(
        FunctionPrincipal(
            artifact.digest,
            function.implementation_digest,
            function.function_id,
            operation.revision_digest,
            operation.operation_id,
        )
        for artifact in artifacts
        for function in artifact.functions
        for operation in function.operations
        if function.function_id == caller_function_id
    )
    extra = FunctionPrincipal(
        base_principal.parent_artifact_digest,
        base_principal.function_implementation_digest,
        caller_function_id,
        base_principal.contract_revision_digest,
        "caller.operation.declared-only",
    )
    declarations = {
        caller_function_id: (base_principal.to_dict(), extra.to_dict())
    }
    # Expansion follows the verified artifact's finite operation declaration;
    # caller_function_declarations cannot invent an extra callable operation.
    original_artifacts = artifacts
    artifacts = tuple(replace(artifact, functions=tuple(
        replace(function, operations=(*function.operations, replace(function.operations[0], operation_id=extra.operation_id)))
        if function.function_id == caller_function_id else function
        for function in artifact.functions
    )) for artifact in artifacts)
    with pytest.raises(ResolutionError, match="verified artifact operations"):
        HostV4Composition.capture(
            profile=resolved.profile, lock=resolved.lock, plan=resolved.plan,
            activation=activation, artifacts=original_artifacts, routes=routes,
            authority_ceilings=ceilings, caller_function_declarations=declarations,
        )
    with pytest.raises(ResolutionError, match="cover exactly"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=ceilings,
            caller_function_declarations=declarations,
        )

    scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest="sha256:" + "7" * 64,
    )
    target_principals = {
        FunctionPrincipal.from_dict(item["function_principal"]).principal_id: (
            str(item["contract_id"]),
            str(item["operation_id"]),
        )
        for item in resolved.plan["bindings"]
        if item["caller_function_id"] == caller_function_id
    }
    expanded = dict(ceilings)
    expanded.update(
        {
            (
                str(resolved.profile["profile_id"]),
                str(activation["activation_id"]),
                extra.principal_id,
                target_id,
                contract_id,
                operation_id,
            ): AuthorityCeilings(scope, scope, scope)
            for target_id, (contract_id, operation_id) in target_principals.items()
        }
    )
    composition = HostV4Composition.capture(
        profile=resolved.profile,
        lock=resolved.lock,
        plan=resolved.plan,
        activation=activation,
        artifacts=artifacts,
        routes=routes,
        authority_ceilings=expanded,
        caller_function_declarations=declarations,
    )
    assert composition.plan["plan_digest"] == resolved.plan["plan_digest"]


def test_composition_rejects_conflicting_caller_declaration(
    tmp_path: Path,
) -> None:
    """A declared principal for a known Operation must be byte-identical."""

    _, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    edge = resolved.profile["requested_edges"][0]
    caller_function_id = edge["caller_function_id"]
    base_principal = next(
        FunctionPrincipal(
            artifact.digest,
            function.implementation_digest,
            function.function_id,
            operation.revision_digest,
            operation.operation_id,
        )
        for artifact in artifacts
        for function in artifact.functions
        for operation in function.operations
        if function.function_id == caller_function_id
    )
    conflicting = FunctionPrincipal(
        _digest("different-artifact"),
        base_principal.function_implementation_digest,
        caller_function_id,
        base_principal.contract_revision_digest,
        base_principal.operation_id,
    )
    with pytest.raises(ResolutionError, match="conflicts"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=ceilings,
            caller_function_declarations={
                caller_function_id: (conflicting.to_dict(),)
            },
        )


def test_composition_rejects_mislabeled_caller_declaration(
    tmp_path: Path,
) -> None:
    """Declarations keyed under the wrong Function id are refused."""

    _, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    base_principal = next(
        FunctionPrincipal(
            artifact.digest,
            function.implementation_digest,
            function.function_id,
            operation.revision_digest,
            operation.operation_id,
        )
        for artifact in artifacts
        for function in artifact.functions
        for operation in function.operations
    )
    with pytest.raises(ResolutionError, match="inconsistent"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=ceilings,
            caller_function_declarations={
                "unrelated.function": (base_principal.to_dict(),)
            },
        )


def test_projection_expands_declared_caller_operations() -> None:
    """One binding row expands to one row per declared caller Operation."""

    snapshot = _base_snapshot()
    caller = _caller_function()
    rows = _normalized_plan_bindings(snapshot)
    assert len(rows) == len(caller["ops"])
    expected = {
        FunctionPrincipal.from_dict(
            _principal_dict("caller-art", caller, operation)
        ).principal_id
        for operation in caller["ops"]
    }
    assert {row["source_principal_id"] for row in rows} == expected
    assert len({row["binding_id"] for row in rows}) == 1
    assert len({row["edge_digest"] for row in rows}) == 1


def test_projection_resolves_caller_without_incoming_binding() -> None:
    """A caller admitted only through its manifest still resolves exactly."""

    snapshot = _base_snapshot()
    # The caller Pack is admitted (effective_set) yet has no target binding:
    # the caller is declared-but-never-bound and must not be masked.
    rows = _normalized_plan_bindings(snapshot)
    assert rows
    assert all(
        row["owner_pack_id"] == "target-pack" for row in rows
    )


def test_projection_denies_undeclared_caller() -> None:
    """A caller absent from every admitted declaration stays denied."""

    snapshot = _base_snapshot(caller_manifest_present=False)
    with pytest.raises(RuntimeSurfaceError) as failure:
        _normalized_plan_bindings(snapshot)
    assert failure.value.code is RuntimeSurfaceErrorCode.DIGEST_MISMATCH
    assert "no exact Profile edge principals" in str(failure.value)


def test_projection_denies_conflicting_caller_declaration() -> None:
    """A manifest declaration conflicting with a signed binding is denied."""

    caller = _caller_function(("caller.op1",))
    target = _target_function()
    conflict_record = _principal_dict("caller-art", caller, "caller.op1")
    conflict_record["function_implementation_digest"] = _digest("forged-impl")
    bindings = [
        _binding("shell.main", _principal_dict("target-art", target, "t.op")),
        _binding(
            "shell.main",
            conflict_record,
            pack_id="caller-pack",
            contract_id="contract.caller",
            operation_id="caller.op1",
        ),
    ]
    edges = [_edge("shell.main"), _edge("shell.main")]
    edges[1]["target_provider_id"] = "caller.multi"
    edges[1]["contract_id"] = "contract.caller"
    edges[1]["operation_id"] = "caller.op1"
    snapshot = _snapshot(
        bindings=bindings,
        edges=edges,
        packs={
            "shell-pack": _manifest(
                "shell-art",
                [{"id": "shell.main", "impl": "s-impl", "rev": "s-rev", "ops": ["shell.op"]}],
            ),
            "target-pack": _manifest("target-art", [target]),
            "caller-pack": _manifest("caller-art", [caller]),
        },
        effective_pack_ids=["shell-pack", "target-pack", "caller-pack"],
    )
    with pytest.raises(RuntimeSurfaceError) as failure:
        _normalized_plan_bindings(snapshot)
    assert failure.value.code is RuntimeSurfaceErrorCode.DIGEST_MISMATCH


def _workflow_edge() -> dict[str, Any]:
    """The exact root failure: function-scoped edge from a 20-Operation caller."""

    return {
        "caller_function_id": WORKFLOW_FUNCTION,
        "target_provider_id": TARGET_FUNCTION,
        "contract_id": TARGET_CONTRACT,
        "operation_id": TARGET_OPERATION,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {
                "contract": [TARGET_CONTRACT],
                "operation": [TARGET_OPERATION],
            },
            "quotas": {},
            "exact_request_digest": None,
            "opaque": False,
        },
    }


def _workflow_caller_suffixes() -> set[str]:
    """Expected per-Operation caller suffixes for the Workflow provider."""

    manifest = json.loads(WORKFLOW_MANIFEST_PATH.read_text())
    function = next(
        item for item in manifest["functions"] if item["id"] == WORKFLOW_FUNCTION
    )
    return {
        FunctionPrincipal(
            str(manifest["pack"]["artifact_digest"]),
            str(function["implementation_digest"]),
            str(function["id"]),
            str(function["contract_revision_digest"]),
            str(operation_id),
        ).principal_id.removeprefix("sha256:")[:24]
        for operation_id in function["operations"]
    }


def test_capture_expands_multi_operation_caller_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """capture_production_dispatch admits the finite caller expansion."""

    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=("tobkiri_workflow_pack",),
        exclude_callers=(WORKFLOW_FUNCTION,),
        exclude_targets=(WORKFLOW_FUNCTION, "tobkiri.workflow.stop.provider"),
        edges=(_workflow_edge(),),
        backends=(),
    ) as (session, store):
        session.assert_operation_ready(TARGET_CONTRACT, TARGET_OPERATION)
        records = [
            record
            for record in store.list_provider_authorities()
            if record.provider.function_id == TARGET_FUNCTION
            and f".{TARGET_OPERATION.replace('.', '-')}." in record.record_id
        ]
        assert records
        expected_suffixes = _workflow_caller_suffixes()
        assert len(expected_suffixes) > 1
        record_ids = {record.record_id for record in records}
        covered = {
            suffix for suffix in expected_suffixes
            if any(f".{suffix}." in record_id for record_id in record_ids)
        }
        assert covered == expected_suffixes


def test_capture_denies_edge_from_undeclared_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller absent from the admitted inventory is denied, never guessed.

    Profile resolution already refuses callers outside the selected closure;
    the capture-level declaration check below stays as fail-closed depth for
    plans that bypass the official renderer.
    """

    edge = _workflow_edge()
    edge["caller_function_id"] = "tobkiri.undeclared.ghost-caller"
    with pytest.raises(ProfileResolutionDenied, match="caller"):
        with captured_host_profile(
            tmp_path,
            monkeypatch,
            packs=(),
            edges=(edge,),
            backends=(),
        ):
            pass


def test_capture_grants_only_declared_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-operation authority stays absent for undeclared edge operations."""

    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=("tobkiri_workflow_pack",),
        exclude_callers=(WORKFLOW_FUNCTION,),
        exclude_targets=(WORKFLOW_FUNCTION, "tobkiri.workflow.stop.provider"),
        edges=(_workflow_edge(),),
        backends=(),
    ) as (_session, store):
        caller_suffixes = _workflow_caller_suffixes()
        caller_records = [
            record
            for record in store.list_provider_authorities()
            if any(
                f".{suffix}." in record.record_id
                for suffix in caller_suffixes
            )
        ]
        assert caller_records
        allowed_operation = TARGET_OPERATION.replace(".", "-")
        assert all(
            f".{allowed_operation}." in record.record_id
            for record in caller_records
        )
        foreign = [
            record
            for record in caller_records
            if record.provider.function_id != TARGET_FUNCTION
        ]
        assert foreign == []


def test_manifest_caller_principals_reject_unpinned_artifact_identity():
    catalog = SimpleNamespace(packs={"admitted": _manifest("caller-art", [_caller_function(("caller.op1",))])})
    with pytest.raises(AuthorityDenied, match="ProfileLock"):
        _manifest_caller_principals(catalog, {"admitted"}, expected_artifact_digests={"admitted": _digest("other-art")})


def test_projection_rejects_manifest_outside_locked_artifact_pin():
    snapshot = _base_snapshot()
    snapshot.active.resolved.lock['effective_set'][0]['artifact_digest'] = _digest('stale-artifact')
    with pytest.raises(RuntimeSurfaceError, match="ProfileLock"):
        _normalized_plan_bindings(snapshot)


def test_caller_functions_cannot_merge_distinct_artifact_owners():
    catalog = SimpleNamespace(packs={
        "first": _manifest("first-art", [_caller_function(("caller.op1",))]),
        "second": _manifest("second-art", [_caller_function(("caller.op2",))]),
    })
    with pytest.raises(AuthorityDenied, match="ambiguous artifact ownership"):
        _manifest_caller_principals(catalog, {"first", "second"})


def test_projection_cannot_merge_disjoint_operations_from_two_owners():
    snapshot = _base_snapshot(caller_ops=("caller.op1",))
    snapshot.catalog.packs['second-caller-pack'] = _manifest('second-caller-art', [_caller_function(("caller.op2",))])
    snapshot.active.resolved.lock['effective_set'].append({
        'identity': 'second-caller-pack', 'artifact_digest': _digest('second-caller-art'),
    })
    with pytest.raises(RuntimeSurfaceError, match="ambiguous artifact ownership"):
        _normalized_plan_bindings(snapshot)


def test_caller_function_cannot_merge_conflicting_contract_revisions():
    first = _caller_function(("caller.op1",))
    second = {**_caller_function(("caller.op2",)), "rev": "different-contract"}
    catalog = SimpleNamespace(packs={"admitted": _manifest("caller-art", [first, second])})
    with pytest.raises(AuthorityDenied, match="ambiguous artifact ownership"):
        _manifest_caller_principals(catalog, {"admitted"})
