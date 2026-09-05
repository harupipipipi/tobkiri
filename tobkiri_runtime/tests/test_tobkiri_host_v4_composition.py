from __future__ import annotations

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityScope, AuthorityStore, FunctionPrincipal
from ecosystem.defaultspack.domain.runtime_v4 import (
    ActivationStore,
    BundledCatalog,
    resolve_default_profile,
)
from tobkiri_host.composition import AuthorityCeilings, HostV4Composition
from tobkiri_host.contracts import OperationRoute
from tobkiri_host.errors import ResolutionError
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
)
from tests.v4_batch_support import authority_bindings_for_profile
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog


ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = "sha256:" + "9" * 64


def _resolved():
    catalog = load_packaged_profile_catalog()
    bindings = authority_bindings_for_profile(catalog.profiles["defaults"])
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            item["pack"]["artifact_digest"] for item in catalog.packs.values()
        },
        authority_snapshot_digest=SNAPSHOT,
        authority_bindings=bindings,
        security_epoch=1,
    )
    return catalog, resolved


def _artifacts(
    catalog: BundledCatalog,
    selected_pack_ids: set[str],
) -> tuple[PackArtifact, ...]:
    result = []
    for manifest in catalog.packs.values():
        if manifest["pack"]["id"] not in selected_pack_ids:
            continue
        functions = []
        variants = []
        for index, function in enumerate(manifest["functions"]):
            variant_id = f"variant.{index}"
            contract = next(
                item
                for item in manifest["contracts"]
                if item["revision_digest"] == function["contract_revision_digest"]
            )
            operations = tuple(
                ContractOperation(
                    contract_id=contract["contract_id"],
                    contract_version="1.0.0",
                    revision_digest=contract["revision_digest"],
                    operation_id=operation_id,
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                )
                for operation_id in function["operations"]
            )
            functions.append(
                FunctionArtifact(
                    function_id=function["id"],
                    implementation_digest=function["implementation_digest"],
                    variant_id=variant_id,
                    operations=operations,
                )
            )
            variants.append(
                ArtifactVariant(
                    variant_id=variant_id,
                    digest=function["implementation_digest"],
                    execution_kind=(
                        ExecutionKind.PACK_VM
                        if function.get("isolation") == "pack_vm"
                        else ExecutionKind.HOST_EXTENSION
                    ),
                    os="macos",
                    architecture="arm64",
                    runtime_abi="tobkiri-v4",
                    backend="verified-test-backend",
                )
            )
        result.append(
            PackArtifact(
                pack_id=manifest["pack"]["id"],
                version=manifest["pack"]["version"],
                digest=manifest["pack"]["artifact_digest"],
                publisher_lineage="tobkiri.repository",
                package_kind=PackageKind.NORMAL,
                functions=tuple(functions),
                variants=tuple(variants),
            )
        )
    return tuple(result)


def _capture(tmp_path: Path):
    catalog, resolved = _resolved()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=AuthorityStore(tmp_path / "authority.sqlite3"),
    )
    activation = store.activate(
        resolved,
        activation_id="activation:defaults-v4",
        created_at="2026-08-05T00:00:00Z",
    )
    restarted = store.load_active_snapshot()
    assert restarted.activation == activation
    resolved = restarted.resolved
    effective_pack_ids = {item["identity"] for item in resolved.lock["effective_set"]}
    assert "shell.tauri.default" in effective_pack_ids
    assert "shell.cli.default" not in effective_pack_ids
    artifacts = _artifacts(catalog, effective_pack_ids)
    principals = {
        (principal.function_id, principal.operation_id): principal
        for artifact in artifacts
        for function in artifact.functions
        for operation in function.operations
        for principal in (
            FunctionPrincipal(
                artifact.digest,
                function.implementation_digest,
                function.function_id,
                operation.revision_digest,
                operation.operation_id,
            ),
        )
    }
    routes = []
    for item in resolved.plan["bindings"]:
        principal = FunctionPrincipal.from_dict(item["function_principal"])
        function = next(
            function
            for artifact in artifacts
            for function in artifact.functions
            if function.function_id == principal.function_id
        )
        routes.append(
            OperationRoute(
                contract_id=item["contract_id"],
                operation_id=item["operation_id"],
                artifact_digest=item["artifact_digest"],
                function_id=principal.function_id,
                variant_id=function.variant_id,
                execution_domain_profile="verified.v4",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(principal.principal_id),
            )
        )
    scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest="sha256:" + "7" * 64,
    )
    ceilings = {
        (
            "defaults",
            "activation:defaults-v4",
            next(
                principal
                for (function_id, _operation_id), principal in principals.items()
                if function_id == edge["caller_function_id"]
            ).principal_id,
            principals[(edge["target_provider_id"], edge["operation_id"])].principal_id,
            edge["contract_id"],
            edge["operation_id"],
        ): AuthorityCeilings(scope, scope, scope)
        for edge in resolved.profile["requested_edges"]
    }
    composition = HostV4Composition.capture(
        profile=resolved.profile,
        lock=resolved.lock,
        plan=resolved.plan,
        activation=activation,
        artifacts=artifacts,
        routes=routes,
        authority_ceilings=ceilings,
    )
    return composition, resolved, activation, artifacts, routes, ceilings


def test_capture_uses_only_exact_effective_set_and_resolved_routes(tmp_path: Path) -> None:
    composition, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    assert composition.plan["plan_digest"] == resolved.plan["plan_digest"]
    assert composition.activation["activation_id"] == activation["activation_id"]
    assert (
        composition.catalog.resolve("conversation.turn.v1", "complete", ">=1").artifact.digest
        == resolved.plan["bindings"][0]["artifact_digest"]
    )

    with pytest.raises(ResolutionError, match="exactly equal"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts[:-1],
            routes=routes,
            authority_ceilings=ceilings,
        )


def test_capture_rejects_stale_plan_extra_route_and_injected_authority(tmp_path: Path) -> None:
    _composition, resolved, activation, artifacts, routes, ceilings = _capture(tmp_path)
    stale = dict(activation)
    stale["plan_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ResolutionError, match="ActivationRecord"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=stale,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=ceilings,
        )

    injected = dict(ceilings)
    injected[
        (
            "defaults",
            "activation:defaults-v4",
            "sha256:" + "1" * 64,
            next(iter(ceilings))[3],
            next(iter(ceilings))[4],
            next(iter(ceilings))[5],
        )
    ] = next(iter(ceilings.values()))
    with pytest.raises(ResolutionError, match="authority ceilings"):
        HostV4Composition.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            artifacts=artifacts,
            routes=routes,
            authority_ceilings=injected,
        )
