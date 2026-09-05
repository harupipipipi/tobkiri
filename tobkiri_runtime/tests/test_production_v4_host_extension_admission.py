"""Fail-closed admission tests for production Host Provider hook imports."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityScope,
    FunctionPrincipal,
)
from core_runtime.bootstrap import production_v4
from core_runtime.interactive_effect_coordinator import (
    INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID,
    INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID,
    INTERACTIVE_EFFECT_SPECS,
)
from core_runtime import host_provider_hooks_v4
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


def _effect_edge(
    *,
    operation_contract: str,
    operation_id: str,
    authority_mode: str,
    target: str,
) -> object:
    """Build one minimal captured edge for coordinator route validation."""

    scope = AuthorityScope(
        capability="effect.execute",
        semantics_digest=_digest("6"),
    )
    return SimpleNamespace(
        caller=SimpleNamespace(principal_id="coordinator-principal"),
        target=SimpleNamespace(principal_id=target),
        resolved_binding=SimpleNamespace(
            operation=SimpleNamespace(
                contract_id=operation_contract,
                operation_id=operation_id,
            ),
            artifact=SimpleNamespace(package_kind=PackageKind.HOST_EXTENSION),
            variant=SimpleNamespace(execution_kind=ExecutionKind.HOST_EXTENSION),
        ),
        authority_mode=authority_mode,
        ceilings=SimpleNamespace(caller_effect=scope),
    )


def test_interactive_effect_routes_require_a_signed_prepare_and_interactive_execute() -> None:
    """A coordinator cannot infer or widen either half of its future effect."""

    spec = INTERACTIVE_EFFECT_SPECS["shell_execute"]
    prepare = _effect_edge(
        operation_contract=spec.prepare_contract_id,
        operation_id=spec.prepare_operation_id,
        authority_mode="profile_grant",
        target="prepare-principal",
    )
    execute = _effect_edge(
        operation_contract=spec.execute_contract_id,
        operation_id=spec.execute_operation_id,
        authority_mode="interactive_only",
        target="execute-principal",
    )
    domains = {
        (
            spec.execute_contract_id,
            spec.execute_operation_id,
            "execute-principal",
        ): "domain-execute"
    }

    routes = production_v4._captured_interactive_effect_routes(
        (prepare, execute),  # type: ignore[arg-type]
        coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
        dynamic_domain_ids=domains,
    )

    assert len(routes) == 1
    assert routes[0].execute_target_principal.value == "execute-principal"


@pytest.mark.parametrize(
    "edges",
    [
        "missing_execute",
        "wrong_execute_mode",
        "duplicate_execute",
    ],
)
def test_interactive_effect_route_misconfiguration_fails_closed(edges: str) -> None:
    """Missing, noninteractive, or ambiguous execute routes reject activation."""

    spec = INTERACTIVE_EFFECT_SPECS["shell_execute"]
    prepare = _effect_edge(
        operation_contract=spec.prepare_contract_id,
        operation_id=spec.prepare_operation_id,
        authority_mode="profile_grant",
        target="prepare-principal",
    )
    execute = _effect_edge(
        operation_contract=spec.execute_contract_id,
        operation_id=spec.execute_operation_id,
        authority_mode=("profile_grant" if edges == "wrong_execute_mode" else "interactive_only"),
        target="execute-principal",
    )
    selected: tuple[object, ...] = (
        (prepare,)
        if edges == "missing_execute"
        else (prepare, execute, execute)
        if edges == "duplicate_execute"
        else (prepare, execute)
    )

    with pytest.raises(AuthorityDenied, match="interactive effect route"):
        production_v4._captured_interactive_effect_routes(
            selected,  # type: ignore[arg-type]
            coordinator_principal=OpaqueAuthorityRef("coordinator-principal"),
            dynamic_domain_ids={
                (
                    spec.execute_contract_id,
                    spec.execute_operation_id,
                    "execute-principal",
                ): "domain-execute"
            },
        )


def test_interactive_effect_capture_allows_only_one_exact_coordinator_factory() -> None:
    """A different or duplicate Host Provider cannot obtain the late-bound port."""

    function_id = "test.interactive-effect-coordinator"
    binding = _binding()
    exact_binding = replace(
        binding,
        function=replace(
            binding.function,
            function_id=function_id,
        ),
        operation=replace(
            binding.operation,
            contract_id=INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID,
            operation_id=INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID,
        ),
    )
    factory = SimpleNamespace(requires_interactive_effect_port=True)
    exact = (
        function_id,
        (exact_binding,),
        factory,
        "host-backend",
    )

    assert production_v4._interactive_effect_coordinator_factory((exact,)) == exact
    with pytest.raises(AuthorityDenied, match="ambiguous"):
        production_v4._interactive_effect_coordinator_factory((exact, exact))


def test_nested_host_provider_session_is_stable_across_requests_but_panel_bound() -> None:
    """Prepare/status/resume share an owner session without sharing other panels."""

    def envelope(request_id: str, panel_session: str) -> object:
        return SimpleNamespace(
            context=SimpleNamespace(
                request_id=request_id,
                caller_session_id=panel_session,
                profile_id="profile-1",
                activation_id="activation-1",
                plan_digest=_digest("8"),
            ),
            target_principal=OpaqueAuthorityRef("coordinator-principal"),
        )

    prepare = production_v4._nested_host_provider_session_id(
        envelope("request.prepare", "panel-session-a")
    )
    resume = production_v4._nested_host_provider_session_id(
        envelope("request.resume", "panel-session-a")
    )
    foreign_panel = production_v4._nested_host_provider_session_id(
        envelope("request.resume", "panel-session-b")
    )

    assert prepare == resume
    assert prepare != foreign_panel


def test_interactive_effect_recovery_completes_before_the_port_can_be_bound() -> None:
    """Production activation fails closed rather than exposing crash-left effects."""

    recovered: list[bool] = []

    class Controller:
        def recover(self) -> tuple[object, ...]:
            recovered.append(True)
            return ()

    production_v4._recover_interactive_effect_controller(Controller())  # type: ignore[arg-type]
    assert recovered == [True]

    class BrokenController:
        def recover(self) -> tuple[object, ...]:
            raise OSError("store unavailable")

    with pytest.raises(AuthorityDenied, match="recovery"):
        production_v4._recover_interactive_effect_controller(
            BrokenController()  # type: ignore[arg-type]
        )


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


def test_factory_mapping_may_omit_an_unrelated_function(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source file can expose hooks for only some of its Functions."""

    implementation_path = "runtime/provider.py"
    source = b'HOST_PROVIDER_FACTORY = {"another.function": object()}\n'
    path = tmp_path / implementation_path
    path.parent.mkdir(parents=True)
    path.write_bytes(source)
    captured = SimpleNamespace(
        files=(SimpleNamespace(path=implementation_path, content=source),),
        implementation_path=implementation_path,
        materialization_digest=_digest("8"),
    )
    monkeypatch.setattr(
        host_provider_hooks_v4,
        "capture_materialized_artifact",
        lambda *_args: captured,
    )

    assert host_provider_hooks_v4.load_host_provider_factory(
        tmp_path,
        _binding(),
    ) is None
