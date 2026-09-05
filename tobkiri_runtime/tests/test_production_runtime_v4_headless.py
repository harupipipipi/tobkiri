from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from core_runtime.global_contract_dispatch import (
    GlobalContractUnavailable,
    captured_profile_id,
    invoke_global_contract,
)
from core_runtime.di_container import DIContainer
from core_runtime.authority.v4 import AuthorityScope, AuthorityStore, FunctionPrincipal
from ecosystem.defaultspack.domain.runtime_v4 import (
    ActivationStore,
    BundledCatalog,
    resolve_default_profile,
)
from ecosystem.rumi_file_inspect_pack.runtime.inspect import FileInspectService
from tobkiri_host.composition import AuthorityCeilings
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    PackArtifact,
    PackageKind,
)
from tobkiri_host.runtime import (
    ProductionRuntimeV4,
    V4DispatchSession,
    install_dispatch_session,
)


ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


def _principal(binding: Mapping[str, Any]) -> FunctionPrincipal:
    return FunctionPrincipal.from_dict(binding["function_principal"])


def _shell_artifact(catalog: BundledCatalog) -> PackArtifact:
    manifest = catalog.packs["shell.tauri.default"]
    functions: list[FunctionArtifact] = []
    variants: list[ArtifactVariant] = []
    for index, function in enumerate(manifest["functions"]):
        contract = next(
            item
            for item in manifest["contracts"]
            if item["revision_digest"] == function["contract_revision_digest"]
        )
        variant_id = f"shell.test.verified.{index}"
        functions.append(
            FunctionArtifact(
                function_id=function["id"],
                implementation_digest=function["implementation_digest"],
                variant_id=variant_id,
                operations=tuple(
                    ContractOperation(
                        contract_id=contract["contract_id"],
                        contract_version="1.0.0",
                        revision_digest=contract["revision_digest"],
                        operation_id=operation_id,
                        input_schema={"type": "object"},
                        output_schema={"type": "object"},
                    )
                    for operation_id in function["operations"]
                ),
            )
        )
        variants.append(
            ArtifactVariant(
                variant_id=variant_id,
                digest=function["implementation_digest"],
                execution_kind=ExecutionKind.HOST_EXTENSION,
                os="test",
                architecture="test",
                runtime_abi="test-v1",
                backend="verified.shell.test",
            )
        )
    return PackArtifact(
        pack_id=manifest["pack"]["id"],
        version=manifest["pack"]["version"],
        digest=manifest["pack"]["artifact_digest"],
        publisher_lineage="tobkiri.repository",
        package_kind=PackageKind.NORMAL,
        functions=tuple(functions),
        variants=tuple(variants),
    )


def _resolved(catalog: BundledCatalog):
    source = catalog.profiles["defaults"]
    edges = source["requested_edges"]
    references = {
        "|".join(
            edge[key]
            for key in (
                "caller_function_id",
                "target_provider_id",
                "contract_id",
                "operation_id",
            )
        ): f"authority-ref:headless.{index:08d}"
        for index, edge in enumerate(edges)
    }
    return resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            manifest["pack"]["artifact_digest"]
            for manifest in catalog.packs.values()
        },
        authority_snapshot_digest="sha256:" + "9" * 64,
        authority_bindings=references,
        security_epoch=1,
    )


def test_headless_activation_compiles_exact_plan_and_reads_after_restart(
    tmp_path: Path,
) -> None:
    catalog = BundledCatalog.load(_bundle_root())
    resolved = _resolved(catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "proof.txt").write_text("verified-v4\n", encoding="utf-8")
    activation_store = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=AuthorityStore(tmp_path / "authority.sqlite3"),
        catalog=catalog,
    )
    activation_store.activate(
        resolved,
        activation_id="activation:headless-v4",
        created_at="2026-08-05T00:00:00Z",
    )
    restarted = activation_store.load_active_snapshot()
    bindings = restarted.resolved.plan["bindings"]
    shell = _shell_artifact(catalog)
    shell_principals = {
        function.function_id: FunctionPrincipal(
            shell.digest,
            function.implementation_digest,
            function.function_id,
            operation.revision_digest,
            operation.operation_id,
        )
        for function in shell.functions
        for operation in function.operations
    }
    binding_by_operation = {
        (binding["contract_id"], binding["operation_id"]): binding
        for binding in bindings
    }
    provider_principals = {
        binding["function_principal"]["function_id"]: _principal(binding)
        for binding in bindings
    }
    scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest="sha256:" + "7" * 64,
    )
    caller_principals = {**shell_principals, **provider_principals}
    ceilings = {}
    for edge in catalog.profiles["defaults"]["requested_edges"]:
        target = _principal(
            binding_by_operation[(edge["contract_id"], edge["operation_id"])]
        )
        caller = caller_principals[edge["caller_function_id"]]
        ceilings[(caller.principal_id, target.principal_id)] = AuthorityCeilings(
            scope, scope, scope
        )
    effective = {
        item["identity"]: item["artifact_digest"]
        for item in restarted.resolved.lock["effective_set"]
    }
    runtime = ProductionRuntimeV4.capture(
        profile=restarted.resolved.profile,
        lock=restarted.resolved.lock,
        plan=restarted.resolved.plan,
        activation=restarted.activation,
        pack_roots={
            binding["pack_id"]: ROOT / "ecosystem" / binding["pack_id"]
            for binding in bindings
        },
        supporting_artifacts=(shell,),
        verified_effective_artifacts=effective,
        authority_ceilings=ceilings,
    )
    assert runtime.composition.plan["plan_digest"] == restarted.resolved.plan[
        "plan_digest"
    ]

    mount = {
        "root_path": str(workspace),
        "revision": "mount-1",
    }

    class MountClient:
        def invoke(
            self, _contract: str, operation: str, _payload: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            return (
                {"selected_workspace_id": "workspace-1"}
                if operation == "list"
                else mount
            )

    stat = workspace.stat()
    binding = {
        "workspace_id": "workspace-1",
        "access": "read_only",
        "mount_revision": "mount-1",
        "canonical_root": str(workspace.resolve()),
        "root_st_dev": int(stat.st_dev),
        "root_st_ino": int(stat.st_ino),
    }
    binding["root_identity"] = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    service = FileInspectService(MountClient())
    payload = {
        "profile_id": "defaults",
        "workspace_id": "workspace-1",
        "path": "proof.txt",
        "require_selected": True,
        "_workspace_binding": binding,
    }
    assert service.invoke("read", payload)["content"] == "verified-v4\n"
    with pytest.raises(PermissionError):
        service.invoke("read", {**payload, "path": "../outside.txt"})


def test_production_capture_rejects_unapproved_extra_and_stale_scope(
    tmp_path: Path,
) -> None:
    catalog = BundledCatalog.load(_bundle_root())
    resolved = _resolved(catalog)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=AuthorityStore(tmp_path / "authority.sqlite3"),
        catalog=catalog,
    )
    activation = store.activate(
        resolved,
        activation_id="activation:negative-v4",
        created_at="2026-08-05T00:00:00Z",
    )
    effective = {
        item["identity"]: item["artifact_digest"]
        for item in resolved.lock["effective_set"]
    }
    effective["injected"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="Pack roots"):
        ProductionRuntimeV4.capture(
            profile=resolved.profile,
            lock=resolved.lock,
            plan=resolved.plan,
            activation=activation,
            pack_roots={"defaultspack": ROOT / "ecosystem" / "defaultspack"},
            supporting_artifacts=(),
            verified_effective_artifacts=effective,
            authority_ceilings={},
        )


def test_global_dispatch_requires_explicit_v4_snapshot_session() -> None:
    class Session:
        profile_id = "profile:test-v4"

        def invoke(
            self,
            contract_id: str,
            operation_id: str,
            payload: Mapping[str, Any],
            *,
            version_range: str = ">=1,<2",
        ) -> Mapping[str, Any]:
            return {
                "contract_id": contract_id,
                "operation_id": operation_id,
                "payload": dict(payload),
                "version_range": version_range,
            }

        def provider_metadata(
            self, contract_id: str
        ) -> tuple[Mapping[str, Any], ...]:
            return ({"contract_id": contract_id},)

    result = invoke_global_contract(Session(), "example.service.v1", "read", {})
    assert result["operation_id"] == "read"
    assert captured_profile_id(Session()) == "profile:test-v4"
    with pytest.raises(GlobalContractUnavailable, match="live registry lookup"):
        invoke_global_contract(object(), "example.service.v1", "read", {})


def test_dispatch_session_install_publishes_only_captured_instance() -> None:
    container = DIContainer()
    session = V4DispatchSession(
        broker=object(),  # type: ignore[arg-type]
        context_for=lambda _contract, _operation: None,  # type: ignore[arg-type]
        effect_scope_for=lambda _contract, _operation, _payload: {},
        providers={},
        profile_id="profile:installed-v4",
        plan_digest="sha256:" + "1" * 64,
        profile_revision="sha256:" + "2" * 64,
        activation_id="activation:headless-install-test",
    )
    install_dispatch_session(container, session)
    assert container.get("v4_dispatch_session") is session

    invalid = V4DispatchSession(
        broker=object(),  # type: ignore[arg-type]
        context_for=lambda _contract, _operation: None,  # type: ignore[arg-type]
        effect_scope_for=lambda _contract, _operation, _payload: {},
        providers={},
        profile_id="",
        plan_digest="sha256:" + "1" * 64,
        profile_revision="sha256:" + "2" * 64,
        activation_id="activation:headless-invalid-test",
    )
    with pytest.raises(ValueError, match="profile_id"):
        install_dispatch_session(container, invalid)
