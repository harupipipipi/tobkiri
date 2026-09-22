"""The smallest complete Profile v4 execution fixture.

This fixture intentionally does not load Defaults Profile, the bundled catalog,
or any legacy registry.  It contains a tiny Base, a presentation Shell, and one
Normal Pack exposing one pure Contract Operation.  The target Pack is compiled
from a checked-in root; Base and Shell remain metadata-only fixture artifacts so
the test does not introduce a second platform-supervisor project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core_runtime.authority.v4 import AuthorityScope, FunctionPrincipal
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.profile_scope import normalize_requested_scope_template

from tobkiri_host.backends import BackendStatus
from tobkiri_host.contracts import OperationRoute, ResolvedOperationBinding
from tobkiri_host.effects import EffectDisposition, ProviderOutcome
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
    RequestContext,
    RuntimeEvidence,
)
from tobkiri_host.composition import AuthorityCeilings, HostV4Composition
from tobkiri_host.broker import RequestEnvelope

MINIMAL_PROFILE_ID = "conformance.minimal"
MINIMAL_BASE_ID = "conformance.minimal.base"
MINIMAL_SHELL_ID = "conformance.minimal.shell"
MINIMAL_PACK_ID = "conformance.minimal.echo"
MINIMAL_CONTRACT_ID = "io.tobkiri.conformance.echo.v1"
MINIMAL_OPERATION_ID = "echo"
MINIMAL_CALLER_FUNCTION_ID = "conformance.profile-caller"
MINIMAL_FUNCTION_ID = "conformance.echo"
MINIMAL_VARIANT_ID = "conformance.echo.wasm"
MINIMAL_BACKEND_ID = "conformance.minimal.backend"
MINIMAL_DOMAIN_ID = "domain:conformance.minimal.echo"


def _digest(label: str) -> str:
    """Return a deterministic digest for fixture identity, never runtime code."""

    return canonical_digest({"fixture": "conformance.minimal", "label": label})


MINIMAL_PROFILE_AUTHORITY_DIGEST = _digest("profile-authority-snapshot")
MINIMAL_CATALOG_REVISION = _digest("catalog")
MINIMAL_EXECUTABLE_CATALOG_DIGEST = (
    "sha256:14cad34416da183ae98671ec04c6d1cd87bf827367eee917f6b3a02c1e5af8a2"
)
MINIMAL_BUNDLE_DIGEST = _digest("bundle")
MINIMAL_PROFILE_DEFINITION_DIGEST = _digest("profile-definition")
MINIMAL_CONSTRAINTS_DIGEST = _digest("constraints")
MINIMAL_BASE_DIGEST = _digest("base-artifact")
MINIMAL_SHELL_DIGEST = _digest("shell-artifact")
MINIMAL_PACK_DIGEST = "sha256:7ea147ccb4770b0d912aaafb961b1b29f41f6a9449b966aeceaa204dc86462cc"
MINIMAL_CALLER_IMPLEMENTATION_DIGEST = _digest("profile-caller")
MINIMAL_FUNCTION_IMPLEMENTATION_DIGEST = (
    "sha256:db83893c95d81c118325596551b3c2a60263dcbffa80da4ea7610d95ac518073"
)
MINIMAL_BASE_DEFINITION_DIGEST = _digest("base-definition")
MINIMAL_SHELL_DEFINITION_DIGEST = _digest("shell-definition")
MINIMAL_CONTRACT_REVISION_DIGEST = _digest("echo-contract")
MINIMAL_BACKEND_DIGEST = _digest("conformance-backend")
MINIMAL_PACK_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "conformance_minimal_echo_pack"
)

_ECHO_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"message": {"type": "string", "maxLength": 256}},
    "required": ["message"],
}


def _provenance(source_path: str, source_digest: str) -> dict[str, Any]:
    return {
        "schema": "io.tobkiri.provenance.v1",
        "source_kind": "generated",
        "source_path": source_path,
        "source_digest": source_digest,
        "repository_commit": "working-tree",
        "repository_tree": "0" * 64,
        "generator": "tobkiri.conformance.minimal_profile",
        "generator_version": "1.0.0",
        "normative": False,
        "evidence": [
            {
                "path": source_path,
                "rule_id": "minimal-profile-conformance-fixture",
                "digest": source_digest,
            }
        ],
    }


def _operation(
    *,
    contract_id: str,
    operation_id: str,
    revision_digest: str,
    schema: Mapping[str, Any],
) -> ContractOperation:
    return ContractOperation(
        contract_id=contract_id,
        contract_version="1.0.0",
        revision_digest=revision_digest,
        operation_id=operation_id,
        input_schema=schema,
        output_schema=schema,
    )


def _empty_artifact(
    *,
    pack_id: str,
    digest: str,
    package_kind: PackageKind,
) -> PackArtifact:
    """Create a metadata-only Base or Shell artifact for headless conformance."""

    return PackArtifact(
        pack_id=pack_id,
        version="1.0.0",
        digest=digest,
        publisher_lineage="tobkiri.conformance",
        package_kind=package_kind,
        functions=(),
        variants=(),
    )


def _base_artifact() -> PackArtifact:
    caller_operation = _operation(
        contract_id="io.tobkiri.conformance.profile.v1",
        operation_id="profile-caller",
        revision_digest=_digest("profile-caller-contract"),
        schema={"type": "object"},
    )
    caller = FunctionArtifact(
        function_id=MINIMAL_CALLER_FUNCTION_ID,
        implementation_digest=MINIMAL_CALLER_IMPLEMENTATION_DIGEST,
        variant_id="conformance.profile-caller.wasm",
        operations=(caller_operation,),
    )
    variant = ArtifactVariant(
        variant_id="conformance.profile-caller.wasm",
        digest=MINIMAL_CALLER_IMPLEMENTATION_DIGEST,
        execution_kind=ExecutionKind.WASM,
        os="conformance",
        architecture="portable",
        runtime_abi="component-v1",
        backend=MINIMAL_BACKEND_ID,
    )
    return PackArtifact(
        pack_id=MINIMAL_BASE_ID,
        version="1.0.0",
        digest=MINIMAL_BASE_DIGEST,
        publisher_lineage="tobkiri.conformance",
        package_kind=PackageKind.NORMAL,
        functions=(caller,),
        variants=(variant,),
    )


def _echo_artifact() -> PackArtifact:
    operation = _operation(
        contract_id=MINIMAL_CONTRACT_ID,
        operation_id=MINIMAL_OPERATION_ID,
        revision_digest=MINIMAL_CONTRACT_REVISION_DIGEST,
        schema=_ECHO_SCHEMA,
    )
    function = FunctionArtifact(
        function_id=MINIMAL_FUNCTION_ID,
        implementation_digest=MINIMAL_FUNCTION_IMPLEMENTATION_DIGEST,
        variant_id=MINIMAL_VARIANT_ID,
        operations=(operation,),
    )
    variant = ArtifactVariant(
        variant_id=MINIMAL_VARIANT_ID,
        digest=MINIMAL_FUNCTION_IMPLEMENTATION_DIGEST,
        execution_kind=ExecutionKind.WASM,
        os="conformance",
        architecture="portable",
        runtime_abi="component-v1",
        backend=MINIMAL_BACKEND_ID,
    )
    return PackArtifact(
        pack_id=MINIMAL_PACK_ID,
        version="1.0.0",
        digest=MINIMAL_PACK_DIGEST,
        publisher_lineage="tobkiri.conformance",
        package_kind=PackageKind.NORMAL,
        functions=(function,),
        variants=(variant,),
    )


@dataclass(frozen=True)
class MinimalProfile:
    """All records and identities required for one minimal active Profile."""

    base: PackArtifact
    shell: PackArtifact
    pack: PackArtifact
    profile: Mapping[str, Any]
    lock: Mapping[str, Any]
    plan: Mapping[str, Any]
    activation: Mapping[str, Any]
    route: OperationRoute
    caller_principal: FunctionPrincipal
    target_principal: FunctionPrincipal
    authority_ceilings: Mapping[tuple[str, str], AuthorityCeilings]

    @property
    def artifacts(self) -> tuple[PackArtifact, ...]:
        """Return the exact artifact inventory required by ProfileLock."""

        return (self.base, self.shell, self.pack)

    @property
    def artifact_inventory(self) -> Mapping[str, str]:
        """Return the digest-pinned identity inventory for this Profile."""

        return {artifact.pack_id: artifact.digest for artifact in self.artifacts}

    @property
    def pack_root(self) -> Path:
        """Return the exact filesystem root compiled for the target Pack."""

        return MINIMAL_PACK_ROOT

    def composition(self) -> HostV4Composition:
        """Capture and validate the complete minimal v4 composition."""

        return HostV4Composition.capture(
            profile=self.profile,
            lock=self.lock,
            plan=self.plan,
            activation=self.activation,
            artifacts=self.artifacts,
            routes=(self.route,),
            authority_ceilings=self.authority_ceilings,
            effective_artifacts=self.artifact_inventory,
        )


def minimal_profile() -> MinimalProfile:
    """Build one Defaults-independent Base/Shell/Normal Pack Profile."""

    base = _base_artifact()
    shell = _empty_artifact(
        pack_id=MINIMAL_SHELL_ID,
        digest=MINIMAL_SHELL_DIGEST,
        package_kind=PackageKind.RUNTIME_TCB,
    )
    pack = _echo_artifact()
    caller = FunctionPrincipal(
        parent_artifact_digest=base.digest,
        function_implementation_digest=MINIMAL_CALLER_IMPLEMENTATION_DIGEST,
        function_id=MINIMAL_CALLER_FUNCTION_ID,
        contract_revision_digest=_digest("profile-caller-contract"),
        operation_id="profile-caller",
    )
    target = FunctionPrincipal(
        parent_artifact_digest=pack.digest,
        function_implementation_digest=MINIMAL_FUNCTION_IMPLEMENTATION_DIGEST,
        function_id=MINIMAL_FUNCTION_ID,
        contract_revision_digest=MINIMAL_CONTRACT_REVISION_DIGEST,
        operation_id=MINIMAL_OPERATION_ID,
    )
    authority_reference = "authority-ref:conformance.minimal-edge"
    profile: dict[str, Any] = {
        "profile_api_version": "io.tobkiri.profile.v5",
        "profile_id": MINIMAL_PROFILE_ID,
        "state": "resolved",
        "mode": "interactive",
        "catalog_revision": MINIMAL_CATALOG_REVISION,
        "display_name": "Minimal Pack v4 conformance profile",
        "base": {
            "pack_id": base.pack_id,
            "artifact_digest": base.digest,
            "definition_revision": MINIMAL_BASE_DEFINITION_DIGEST,
            "resolution": "verified",
        },
        "shell": {
            "provider_id": "conformance.minimal.shell-provider",
            "pack_id": shell.pack_id,
            "artifact_digest": shell.digest,
            "executable_artifact_digest": shell.digest,
            "definition_revision": MINIMAL_SHELL_DEFINITION_DIGEST,
            "contract_id": "app.shell.v1",
            "platform": "linux",
            "architecture": "x86_64",
        },
        "packs": [{"pack_id": pack.pack_id, "artifact_digest": pack.digest, "role": "provider"}],
        "requested_edges": [
            {
                "caller_function_id": caller.function_id,
                "target_provider_id": target.function_id,
                "contract_id": MINIMAL_CONTRACT_ID,
                "operation_id": MINIMAL_OPERATION_ID,
                "requested_scope_template": normalize_requested_scope_template(
                    {},
                    contract_id=MINIMAL_CONTRACT_ID,
                    operation_id=MINIMAL_OPERATION_ID,
                    semantics_digest=target.contract_revision_digest,
                ),
                "authority_reference": authority_reference,
            }
        ],
        "authority_references": [authority_reference],
        "profile_authority_snapshot_digest": MINIMAL_PROFILE_AUTHORITY_DIGEST,
        "provenance": _provenance(
            "tests/conformance_support/minimal_profile.py",
            _digest("minimal-profile-source"),
        ),
    }
    profile_revision = canonical_digest(profile)
    effective_set = [
        {"role": "base", "identity": base.pack_id, "artifact_digest": base.digest},
        {"role": "shell", "identity": shell.pack_id, "artifact_digest": shell.digest},
        {"role": "pack", "identity": pack.pack_id, "artifact_digest": pack.digest},
    ]
    requested_edges_digest = canonical_digest(profile["requested_edges"])
    closure_digest = canonical_digest(effective_set)
    provenance_digest = canonical_digest(profile["provenance"])
    binding = {
        "caller_function_id": caller.function_id,
        "pack_id": pack.pack_id,
        "artifact_digest": pack.digest,
        "function_principal": target.to_dict(),
        "contract_id": MINIMAL_CONTRACT_ID,
        "operation_id": MINIMAL_OPERATION_ID,
        "domain_kind": "wasm_component",
        "executable_catalog_digest": MINIMAL_EXECUTABLE_CATALOG_DIGEST,
        "variant_id": MINIMAL_VARIANT_ID,
        "platform": "conformance",
        "architecture": "portable",
        "runtime_abi": "component-v1",
        "backend": MINIMAL_BACKEND_ID,
        "execution_kind": "wasm",
        "authority_reference": authority_reference,
        "requested_scope_digest": canonical_digest(
            profile["requested_edges"][0]["requested_scope_template"]
        ),
        "provider_authority_digest": _digest("provider-authority"),
        "adapter_digests": [],
    }
    plan_without_digest: dict[str, Any] = {
        "plan_api_version": "io.tobkiri.resolved-plan.v2",
        "profile_id": MINIMAL_PROFILE_ID,
        "profile_revision": profile_revision,
        "profile_definition_digest": MINIMAL_PROFILE_DEFINITION_DIGEST,
        "catalog_revision": MINIMAL_CATALOG_REVISION,
        "bundle_digest": MINIMAL_BUNDLE_DIGEST,
        "profile_authority_snapshot_digest": MINIMAL_PROFILE_AUTHORITY_DIGEST,
        "security_epoch": 1,
        "base": {
            "pack_id": base.pack_id,
            "artifact_digest": base.digest,
            "definition_digest": MINIMAL_BASE_DEFINITION_DIGEST,
        },
        "shell": {
            "provider_id": "conformance.minimal.shell-provider",
            "pack_id": shell.pack_id,
            "artifact_digest": shell.digest,
            "executable_artifact_digest": shell.digest,
            "contract_id": "app.shell.v1",
            "definition_digest": MINIMAL_SHELL_DEFINITION_DIGEST,
        },
        "application": None,
        "effective_set": effective_set,
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": MINIMAL_CONSTRAINTS_DIGEST,
        "closure_digest": closure_digest,
        "provenance_digest": provenance_digest,
        "bindings": [binding],
    }
    plan = {
        **plan_without_digest,
        "plan_digest": canonical_digest(plan_without_digest),
    }
    lock_without_digest: dict[str, Any] = {
        "lock_api_version": "io.tobkiri.profile-lock.v5",
        "profile_id": MINIMAL_PROFILE_ID,
        "profile_revision": profile_revision,
        "profile_definition_digest": MINIMAL_PROFILE_DEFINITION_DIGEST,
        "catalog_revision": MINIMAL_CATALOG_REVISION,
        "bundle_digest": MINIMAL_BUNDLE_DIGEST,
        "security_epoch": 1,
        "base": {
            "pack_id": base.pack_id,
            "artifact_digest": base.digest,
            "definition_revision": MINIMAL_BASE_DEFINITION_DIGEST,
        },
        "shell": {
            "provider_id": "conformance.minimal.shell-provider",
            "pack_id": shell.pack_id,
            "artifact_digest": shell.digest,
            "executable_artifact_digest": shell.digest,
            "definition_revision": MINIMAL_SHELL_DEFINITION_DIGEST,
            "contract_id": "app.shell.v1",
            "platform": "linux",
            "architecture": "x86_64",
        },
        "application": None,
        "effective_set": effective_set,
        "variant_pins": [
            {
                "pack_id": pack.pack_id,
                "artifact_digest": pack.digest,
                "executable_catalog_digest": MINIMAL_EXECUTABLE_CATALOG_DIGEST,
                "variant_id": MINIMAL_VARIANT_ID,
                "platform": "conformance",
                "architecture": "portable",
                "runtime_abi": "component-v1",
                "backend": MINIMAL_BACKEND_ID,
                "execution_kind": "wasm",
                "domain_kind": "wasm_component",
            }
        ],
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": MINIMAL_CONSTRAINTS_DIGEST,
        "closure_digest": closure_digest,
        "provenance_digest": provenance_digest,
        "plan_digest": plan["plan_digest"],
        "profile_authority_snapshot_digest": MINIMAL_PROFILE_AUTHORITY_DIGEST,
    }
    lock = {
        **lock_without_digest,
        "lock_digest": canonical_digest(lock_without_digest),
    }
    activation = {
        "activation_api_version": "io.tobkiri.activation-record.v2",
        "profile_id": MINIMAL_PROFILE_ID,
        "profile_revision": profile_revision,
        "activation_id": "activation:conformance-minimal-1",
        "state": "active",
        "state_generation": 0,
        "catalog_revision": MINIMAL_CATALOG_REVISION,
        "bundle_digest": MINIMAL_BUNDLE_DIGEST,
        "lock_digest": lock["lock_digest"],
        "plan_digest": plan["plan_digest"],
        "closure_digest": closure_digest,
        "profile_authority_snapshot_digest": MINIMAL_PROFILE_AUTHORITY_DIGEST,
        "security_epoch": 1,
        "fencing_token": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "committed_at": "2026-01-01T00:00:00Z",
    }
    route = OperationRoute(
        contract_id=MINIMAL_CONTRACT_ID,
        operation_id=MINIMAL_OPERATION_ID,
        artifact_digest=pack.digest,
        function_id=target.function_id,
        variant_id=MINIMAL_VARIANT_ID,
        execution_domain_profile="conformance.minimal.wasm.v1",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef(target.principal_id),
    )
    scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest=MINIMAL_CONTRACT_REVISION_DIGEST,
        dimensions={
            "contract": (MINIMAL_CONTRACT_ID,),
            "operation": (MINIMAL_OPERATION_ID,),
        },
    )
    return MinimalProfile(
        base=base,
        shell=shell,
        pack=pack,
        profile=profile,
        lock=lock,
        plan=plan,
        activation=activation,
        route=route,
        caller_principal=caller,
        target_principal=target,
        authority_ceilings={
            (caller.principal_id, target.principal_id): AuthorityCeilings(scope, scope, scope)
        },
    )


@dataclass
class MinimalConformanceBackend:
    """Deterministic in-process backend reserved for conformance tests."""

    status: BackendStatus = field(
        default_factory=lambda: BackendStatus(
            backend_id=MINIMAL_BACKEND_ID,
            execution_kind=ExecutionKind.WASM,
            platform="conformance-portable",
            backend_digest=MINIMAL_BACKEND_DIGEST,
            production_enabled=False,
            conformance_only=True,
        )
    )
    materializations: int = 0
    invocations: int = 0
    cancelled: list[str] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        if (
            binding.artifact.pack_id != MINIMAL_PACK_ID
            or binding.operation.contract_id != MINIMAL_CONTRACT_ID
            or binding.operation.operation_id != MINIMAL_OPERATION_ID
            or not reservation_id
        ):
            raise ValueError("minimal backend received an unexpected binding")
        self.materializations += 1
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(MINIMAL_DOMAIN_ID),
            executable_digest=binding.function.implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, request: object) -> ProviderOutcome:
        if not isinstance(request, RequestEnvelope):
            raise TypeError("minimal backend requires a Host RequestEnvelope")
        if (
            request.target_domain.value != MINIMAL_DOMAIN_ID
            or request.contract_id != MINIMAL_CONTRACT_ID
            or request.operation_id != MINIMAL_OPERATION_ID
        ):
            raise ValueError("minimal backend received an unexpected envelope")
        message = request.payload.get("message")
        if not isinstance(message, str):
            raise TypeError("minimal Pack message must be a string")
        self.invocations += 1
        return ProviderOutcome({"message": message}, EffectDisposition.COMPLETED)

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)

    def terminate(self, domain_id: str) -> None:
        self.terminated.append(domain_id)


def minimal_context(backend: MinimalConformanceBackend | None = None) -> RequestContext:
    """Return Host-authenticated context bound to the minimal active Profile."""

    selected = minimal_profile()
    backend_digest = (backend or MinimalConformanceBackend()).status.backend_digest
    return RequestContext(
        request_id="request:conformance-minimal-1",
        trace_id="trace:conformance-minimal-1",
        caller_principal=OpaqueAuthorityRef(selected.caller_principal.principal_id),
        profile_id=MINIMAL_PROFILE_ID,
        activation_id=selected.activation["activation_id"],
        activation_digest=canonical_digest(selected.activation),
        plan_digest=selected.plan["plan_digest"],
        security_epoch=1,
        caller_session_id="session:conformance-minimal",
        caller_domain_id="domain:conformance.minimal.caller",
        caller_boot_epoch=1,
        target_domain_id=MINIMAL_DOMAIN_ID,
        target_boot_epoch=1,
        target_backend_digest=backend_digest,
        profile_authority_digest=MINIMAL_PROFILE_AUTHORITY_DIGEST,
        fencing_token=1,
        handle_namespace="handles:conformance.minimal",
    )


__all__ = [
    "MINIMAL_BACKEND_ID",
    "MINIMAL_CONTRACT_ID",
    "MINIMAL_OPERATION_ID",
    "MINIMAL_PACK_ROOT",
    "MinimalConformanceBackend",
    "MinimalProfile",
    "minimal_context",
    "minimal_profile",
]
