"""Fail-closed admission tests for production Host Provider hook imports."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core_runtime.authority.v4 import AuthorityDenied, FunctionPrincipal
from core_runtime.bootstrap import production_v4
from tobkiri_host.contracts import OperationRoute, ResolvedOperationBinding
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _binding(
    *,
    package_kind: PackageKind = PackageKind.HOST_EXTENSION,
    mixed_variant: bool = False,
) -> ResolvedOperationBinding:
    operation = ContractOperation(
        contract_id="test.host-extension.v4",
        contract_version="4.0.0",
        revision_digest=_digest("3"),
        operation_id="invoke",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    function = FunctionArtifact(
        function_id="test.host-extension.provider",
        implementation_digest=_digest("2"),
        variant_id="test.host-extension.variant",
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
    variants = (variant,)
    if mixed_variant:
        variants += (
            ArtifactVariant(
                variant_id="test.host-extension.mixed",
                digest=_digest("4"),
                execution_kind=ExecutionKind.PACK_VM,
                os="any",
                architecture="any",
                runtime_abi="python-v4",
                backend="tobkiri.python-pack-v4",
            ),
        )
    artifact = PackArtifact(
        pack_id="test_host_extension_pack",
        version="1.0.0",
        digest=_digest("1"),
        publisher_lineage="test.publisher",
        package_kind=package_kind,
        functions=(function,),
        variants=variants,
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
    return ResolvedOperationBinding(
        artifact=artifact,
        function=function,
        variant=variant,
        operation=operation,
        route=route,
        principal_ref=route.target_principal_ref,
    )


def test_normal_pack_is_rejected_before_host_provider_loader(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malicious Normal Pack cannot execute top-level import code."""

    binding = _binding(package_kind=PackageKind.NORMAL)
    loader_called = False

    def malicious_loader(*_args: object) -> object:
        nonlocal loader_called
        loader_called = True
        raise AssertionError("malicious top-level import executed")

    monkeypatch.setattr(production_v4, "load_host_provider_factory", malicious_loader)
    with pytest.raises(AuthorityDenied, match="Host Extension package"):
        production_v4._load_verified_host_provider_factory(
            tmp_path,
            binding.function.function_id,
            (binding,),
        )
    assert loader_called is False


def test_valid_host_extension_reaches_exact_loader(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding()
    factory = object()
    observed: list[ResolvedOperationBinding] = []

    def exact_loader(_root, selected_binding):
        observed.append(selected_binding)
        return factory

    monkeypatch.setattr(production_v4, "load_host_provider_factory", exact_loader)
    loaded, backend_id = production_v4._load_verified_host_provider_factory(
        tmp_path,
        binding.function.function_id,
        (binding,),
    )
    assert loaded is factory
    assert backend_id == "tobkiri.python-host-v4"
    assert observed == [binding]


def test_mixed_host_extension_variant_inventory_is_rejected_before_loader(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(mixed_variant=True)
    loader_called = False

    def loader(*_args: object) -> object:
        nonlocal loader_called
        loader_called = True
        return object()

    monkeypatch.setattr(production_v4, "load_host_provider_factory", loader)
    with pytest.raises(AuthorityDenied, match="artifact boundary"):
        production_v4._load_verified_host_provider_factory(
            tmp_path,
            binding.function.function_id,
            (binding,),
        )
    assert loader_called is False


def test_host_extension_principal_identity_mismatch_is_rejected_before_loader(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = replace(
        _binding(),
        principal_ref=OpaqueAuthorityRef(_digest("9")),
    )
    loader_called = False

    def loader(*_args: object) -> object:
        nonlocal loader_called
        loader_called = True
        return object()

    monkeypatch.setattr(production_v4, "load_host_provider_factory", loader)
    with pytest.raises(AuthorityDenied, match="verified identity"):
        production_v4._load_verified_host_provider_factory(
            tmp_path,
            binding.function.function_id,
            (binding,),
        )
    assert loader_called is False
