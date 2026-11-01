"""Local inference capture follows exact contracts, not provider Pack names."""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied, AuthorityStore, FunctionPrincipal
from core_runtime.bootstrap import production_v4
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.local_model_authority import (
    LOCAL_REGISTRY_CONTRACT,
    LocalModelRequest,
    capture_local_model_binding,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from tests import test_live_production_v4_dispatch as production_support
from tests.test_live_production_v4_dispatch import (
    _deny_production_test_internet,  # noqa: F401
)
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.contracts import (
    OperationCatalog,
    OperationRoute,
    ResolvedOperationBinding,
)
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
)


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _principal(binding: ResolvedOperationBinding) -> FunctionPrincipal:
    return FunctionPrincipal(
        parent_artifact_digest=binding.artifact.digest,
        function_implementation_digest=binding.function.implementation_digest,
        function_id=binding.function.function_id,
        contract_revision_digest=binding.operation.revision_digest,
        operation_id=binding.operation.operation_id,
    )


def _generic_binding(
    *,
    contract_id: str,
    identity: str,
    contract_version: str = "1.0.0",
) -> ResolvedOperationBinding:
    """Build complete unit-test inventory with independently named providers."""
    operation = ContractOperation(
        contract_id=contract_id,
        contract_version=contract_version,
        revision_digest=_digest(identity + ".contract"),
        operation_id=identity + ".invoke",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    function = FunctionArtifact(
        function_id=identity + ".function",
        implementation_digest=_digest(identity + ".implementation"),
        variant_id=identity + ".variant",
        operations=(operation,),
    )
    variant = ArtifactVariant(
        variant_id=function.variant_id,
        digest=function.implementation_digest,
        execution_kind=ExecutionKind.HOST_EXTENSION,
        os="any",
        architecture="any",
        runtime_abi="python-v4",
        backend="tobkiri.python-host-v4",
    )
    artifact = PackArtifact(
        pack_id=identity + "_pack",
        version="1.0.0",
        digest=_digest(identity + ".artifact"),
        publisher_lineage="independent.publisher",
        package_kind=PackageKind.HOST_EXTENSION,
        functions=(function,),
        variants=(variant,),
    )
    principal = FunctionPrincipal(
        parent_artifact_digest=artifact.digest,
        function_implementation_digest=function.implementation_digest,
        function_id=function.function_id,
        contract_revision_digest=operation.revision_digest,
        operation_id=operation.operation_id,
    )
    route = OperationRoute(
        contract_id=operation.contract_id,
        operation_id=operation.operation_id,
        artifact_digest=artifact.digest,
        function_id=function.function_id,
        variant_id=variant.variant_id,
        execution_domain_profile="dedicated-process",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef(principal.principal_id),
    )
    return OperationCatalog((artifact,), (route,)).resolve_pinned(
        operation.contract_id, operation.operation_id,
    )


def _bindings(
    kind: str = "generate",
) -> tuple[ResolvedOperationBinding, ResolvedOperationBinding]:
    return (
        _generic_binding(
            contract_id=f"tobkiri.service.ai.provider.{kind}.v1",
            identity="independent_text_provider",
        ),
        _generic_binding(
            contract_id=LOCAL_REGISTRY_CONTRACT,
            identity="independent_connection_registry",
        ),
    )


@pytest.mark.parametrize("kind", ["generate", "stream"])
def test_capture_accepts_independently_named_exact_text_provider(
    kind: str,
) -> None:
    provider, registry = _bindings(kind)

    captured = capture_local_model_binding(provider, registry)

    assert captured.principal == _principal(provider)
    assert captured.registry_principal == _principal(registry)
    assert captured.contract_id == provider.operation.contract_id
    assert captured.contract_version == provider.operation.contract_version
    assert captured.registry_operation_id == registry.operation.operation_id
    assert captured.principal.parent_artifact_digest == provider.artifact.digest
    assert (
        captured.principal.contract_revision_digest
        == provider.operation.revision_digest
    )


@pytest.mark.parametrize(
    "contract_id",
    [
        "tobkiri.service.ai.provider.embedding.v1",
        "tobkiri.service.ai.provider.image.v1",
        "tobkiri.service.ai.generate.v1",
    ],
)
def test_capture_rejects_other_provider_contracts(contract_id: str) -> None:
    _, registry = _bindings()
    provider = _generic_binding(
        contract_id=contract_id, identity="independent_text_provider",
    )

    with pytest.raises(PermissionError, match="contract binding"):
        capture_local_model_binding(provider, registry)


@pytest.mark.parametrize(
    "contract_id",
    [
        "tobkiri.action.ai.provider.registry.manage.v1",
        "tobkiri.resource.ai.model.registry.v1",
        "independent.resource.registry.v1",
    ],
)
def test_capture_rejects_other_registry_contracts(contract_id: str) -> None:
    provider, _ = _bindings()
    registry = _generic_binding(
        contract_id=contract_id, identity="independent_connection_registry",
    )

    with pytest.raises(PermissionError, match="contract binding"):
        capture_local_model_binding(provider, registry)


@pytest.mark.parametrize("target", ["provider", "registry"])
@pytest.mark.parametrize("version", ["1.0.1", "2.0.0"])
def test_capture_rejects_uncaptured_contract_versions(
    target: str, version: str,
) -> None:
    provider, registry = _bindings()
    if target == "provider":
        provider = _generic_binding(
            contract_id=provider.operation.contract_id,
            identity="independent_text_provider",
            contract_version=version,
        )
    else:
        registry = _generic_binding(
            contract_id=LOCAL_REGISTRY_CONTRACT,
            identity="independent_connection_registry",
            contract_version=version,
        )

    with pytest.raises(PermissionError, match="contract binding"):
        capture_local_model_binding(provider, registry)


@pytest.mark.parametrize("target", ["provider", "registry"])
def test_capture_rejects_substituted_principal_references(target: str) -> None:
    provider, registry = _bindings()
    foreign = OpaqueAuthorityRef(_digest("substituted-principal"))
    if target == "provider":
        provider = replace(provider, principal_ref=foreign)
    else:
        registry = replace(registry, principal_ref=foreign)

    with pytest.raises(PermissionError, match="principal binding"):
        capture_local_model_binding(provider, registry)


@pytest.mark.parametrize("kind", ["generate", "stream", "embedding", "image"])
def test_verified_adapter_factory_declares_only_text_local_inference(
    kind: str,
) -> None:
    """Load actual declarations only after compiler and artifact verification."""
    root = (
        Path(__file__).resolve().parents[1]
        / "ecosystem"
        / "rumi_provider_adapters_pack"
    )
    compiled = compile_pack_root(root)
    function = compiled.artifact.function(
        "rumi_provider_adapters_pack.provider.compatibility." + kind,
    )
    assert len(function.operations) == 1
    operation = function.operations[0]
    principal = FunctionPrincipal(
        parent_artifact_digest=compiled.artifact.digest,
        function_implementation_digest=function.implementation_digest,
        function_id=function.function_id,
        contract_revision_digest=operation.revision_digest,
        operation_id=operation.operation_id,
    )
    route = OperationRoute(
        contract_id=operation.contract_id,
        operation_id=operation.operation_id,
        artifact_digest=compiled.artifact.digest,
        target_principal_ref=OpaqueAuthorityRef(principal.principal_id),
        **compiled.routes[(operation.contract_id, operation.operation_id)],
    )
    binding = OperationCatalog((compiled.artifact,), (route,)).resolve_pinned(
        operation.contract_id, operation.operation_id,
    )

    factory, backend_id = production_v4._load_verified_host_provider_factory(
        root, function.function_id, (binding,),
    )

    assert factory is not None
    assert factory.function_id == function.function_id
    assert backend_id == binding.variant.backend
    declaration = factory.local_model_request
    if kind in {"embedding", "image"}:
        assert declaration is None
    else:
        assert type(declaration) is LocalModelRequest
        assert declaration.contract_id == operation.contract_id
        assert declaration.registry_operation_id == (
            "rumi_provider_registry_pack.provider-registry-resource." + kind
        )


def test_local_model_capture_and_request_are_immutable() -> None:
    provider, registry = _bindings()
    captured = capture_local_model_binding(provider, registry)
    request = LocalModelRequest(
        provider.operation.contract_id, registry.operation.operation_id,
    )

    with pytest.raises(FrozenInstanceError):
        captured.contract_id = "independent.substituted.v1"
    with pytest.raises(FrozenInstanceError):
        request.registry_operation_id = "independent.substituted.invoke"


@pytest.mark.parametrize(
    "mutation", ["foreign_caller", "missing_dependency", "wrong_contract"],
)
def test_production_capture_rejects_unbound_verified_factory_declarations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Keep signed fixtures intact and alter only an already verified claim."""
    user_data = tmp_path / "local-model-capture-rejection"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation(),
    )
    generate_function = (
        "rumi_provider_adapters_pack.provider.compatibility.generate"
    )
    stream_function = "rumi_provider_adapters_pack.provider.compatibility.stream"
    stream_registry_operation = (
        "rumi_provider_registry_pack.provider-registry-resource.stream"
    )
    if mutation == "foreign_caller":
        # The target really exists in the signed Plan, but belongs to the
        # stream principal rather than this generate principal.
        assert any(
            item["caller_function_id"] == stream_function
            and item["contract_id"] == LOCAL_REGISTRY_CONTRACT
            and item["operation_id"] == stream_registry_operation
            for item in active.resolved.plan["bindings"]
        )
        assert not any(
            item["caller_function_id"] == generate_function
            and item["contract_id"] == LOCAL_REGISTRY_CONTRACT
            and item["operation_id"] == stream_registry_operation
            for item in active.resolved.plan["bindings"]
        )
    original_loader = production_v4._load_verified_host_provider_factory
    verified_declarations: list[LocalModelRequest] = []
    local_transport_attempts: list[dict[str, Any]] = []

    def declared_factory(
        pack_root: Path,
        function_id: str,
        bindings: tuple[ResolvedOperationBinding, ...],
    ) -> tuple[Any, str]:
        # Call the complete production verification path before changing the
        # factory declaration. No catalog, Profile, digest or seal is replaced.
        factory, backend_id = original_loader(pack_root, function_id, bindings)
        if function_id == generate_function:
            assert factory is not None
            declaration = factory.local_model_request
            assert type(declaration) is LocalModelRequest
            verified_declarations.append(declaration)
            if mutation == "foreign_caller":
                replacement = replace(
                    declaration, registry_operation_id=stream_registry_operation,
                )
            elif mutation == "missing_dependency":
                replacement = replace(
                    declaration,
                    registry_operation_id="fixture.local-model-registry.absent",
                )
            else:
                replacement = replace(
                    declaration,
                    contract_id="tobkiri.service.ai.provider.stream.v1",
                )
            factory.local_model_request = replacement
        return factory, backend_id

    def deny_local_transport(**kwargs: Any) -> Any:
        local_transport_attempts.append(kwargs)
        pytest.fail("rejected capture must not construct local inference transport")

    monkeypatch.setattr(
        production_v4, "_load_verified_host_provider_factory", declared_factory,
    )
    monkeypatch.setattr(
        production_v4, "create_local_model_transport", deny_local_transport,
    )
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as store:
        with pytest.raises(
            AuthorityDenied, match="Host local model dependency binding is invalid",
        ):
            session = production_v4.capture_production_dispatch(
                active,
                bundle_root=production_support._bundle_root(),
                ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
                authority_store=store,
                activation_snapshot_loader=defaultspack_activation_snapshot_loader,
                runtime_surface_factory=create_runtime_surface_services,
                backends=BackendRegistry(()),
            )
            session.close()
    assert len(verified_declarations) == 1
    assert local_transport_attempts == []
