"""Contract, adapter, backend, and Base/Shell v4 core tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib

import pytest

from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.contracts import (
    AdapterPlanner,
    OperationCatalog,
    OperationRoute,
    StructuralAdapter,
    schema_digest,
)
from tobkiri_host.errors import (
    AdapterError,
    BackendUnavailableError,
    InvalidArtifactError,
    ResolutionError,
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
from tobkiri_host.shells import (
    BaseDefinition,
    BaseShellResolver,
    PresentationContribution,
    PresentationFamily,
    ShellDefinition,
)

pytestmark = pytest.mark.contract


def digest(character: str) -> str:
    return f"sha256:{hashlib.sha256(character.encode()).hexdigest()}"


INPUT_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "integer"}},
    "required": ["result"],
    "additionalProperties": False,
}


def artifact() -> PackArtifact:
    operation = ContractOperation(
        contract_id="io.tobkiri.math.v1",
        contract_version="1.2.0",
        revision_digest=digest("c"),
        operation_id="increment",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
    )
    function = FunctionArtifact(
        function_id="math.increment",
        implementation_digest=digest("f"),
        variant_id="macos.arm64",
        operations=(operation,),
    )
    variant = ArtifactVariant(
        variant_id="macos.arm64",
        digest=digest("v"),
        execution_kind=ExecutionKind.WASM,
        os="macos",
        architecture="arm64",
        runtime_abi="component-v1",
        backend="wasmtime",
    )
    return PackArtifact(
        pack_id="math.pack",
        version="1.0.0",
        digest=digest("a"),
        publisher_lineage="publisher.main",
        package_kind=PackageKind.NORMAL,
        functions=(function,),
        variants=(variant,),
    )


def route(**changes: object) -> OperationRoute:
    base = OperationRoute(
        contract_id="io.tobkiri.math.v1",
        operation_id="increment",
        artifact_digest=digest("a"),
        function_id="math.increment",
        variant_id="macos.arm64",
        execution_domain_profile="wasm.no-authority.v1",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef("authority:math-increment"),
    )
    return replace(base, **changes)


def test_pack_artifact_inventory_rejects_unknown_variant() -> None:
    item = artifact()
    function = replace(item.functions[0], variant_id="missing")
    with pytest.raises(InvalidArtifactError, match="unknown variants"):
        replace(item, functions=(function,))


def test_pack_artifact_inventory_can_include_explicit_private_function() -> None:
    item = artifact()
    private = FunctionArtifact(
        function_id="math.internal.helper",
        implementation_digest=digest("h"),
        variant_id="macos.arm64",
        operations=(),
        exported=False,
    )
    updated = replace(item, functions=(*item.functions, private))
    assert updated.function("math.internal.helper") == private


def test_operation_catalog_routes_exact_inventoried_operation() -> None:
    catalog = OperationCatalog((artifact(),), (route(),))
    binding = catalog.resolve(
        "io.tobkiri.math.v1",
        "increment",
        ">=1.0,<2.0",
    )
    assert binding.function.function_id == "math.increment"
    assert binding.principal_ref.value == "authority:math-increment"
    catalog.validate_input(binding, {"value": 3})
    catalog.validate_output(binding, {"result": 4})


def test_operation_catalog_host_owned_resolution_uses_exact_pinned_version() -> None:
    """Host internal projection uses the plan pin, not a global major default."""

    catalog = OperationCatalog((artifact(),), (route(),))
    assert catalog.pinned_version_range("io.tobkiri.math.v1", "increment") == "==1.2.0"
    assert (
        catalog.resolve_pinned("io.tobkiri.math.v1", "increment").operation.contract_version
        == "1.2.0"
    )
    assert (
        catalog.resolve("io.tobkiri.math.v1", "increment", None).operation.contract_version
        == "1.2.0"
    )
    with pytest.raises(ResolutionError, match="incompatible"):
        catalog.resolve("io.tobkiri.math.v1", "increment", ">=2,<3")


def test_operation_catalog_invalid_pinned_version_fails_closed() -> None:
    """An invalid executable Contract pin is never treated as unconstrained."""

    item = artifact()
    operation = replace(item.functions[0].operations[0], contract_version="not-a-version")
    function = replace(item.functions[0], operations=(operation,))
    catalog = OperationCatalog((replace(item, functions=(function,)),), (route(),))
    with pytest.raises(ResolutionError, match="invalid pinned Contract version"):
        catalog.resolve_pinned("io.tobkiri.math.v1", "increment")


def test_operation_catalog_never_discovers_unpinned_provider() -> None:
    catalog = OperationCatalog((artifact(),), ())
    with pytest.raises(ResolutionError, match="not pinned"):
        catalog.resolve("io.tobkiri.math.v1", "increment", ">=1")


def test_operation_catalog_rejects_route_inventory_mismatch() -> None:
    with pytest.raises(ResolutionError, match="not in Function inventory"):
        OperationCatalog((artifact(),), (route(operation_id="decrement"),))


class AdapterRuntime:
    def execute(
        self,
        adapter: StructuralAdapter,
        payload: dict[str, object],
    ) -> dict[str, object]:
        return {"result": int(payload["value"]) + 1}


def structural_adapter(**changes: object) -> StructuralAdapter:
    base = StructuralAdapter(
        adapter_id="math.input-to-output",
        artifact_digest=digest("d"),
        source_schema_digest=schema_digest(INPUT_SCHEMA),
        target_schema_digest=schema_digest(OUTPUT_SCHEMA),
        source_schema=INPUT_SCHEMA,
        target_schema=OUTPUT_SCHEMA,
    )
    return replace(base, **changes)


def test_structural_adapter_validates_every_hop_schema() -> None:
    adapter = structural_adapter()
    planner = AdapterPlanner((adapter,))
    plan = planner.plan((adapter.adapter_id,))
    assert planner.execute(plan, {"value": 1}, AdapterRuntime()) == {"result": 2}
    with pytest.raises(ResolutionError, match="adapter input"):
        planner.execute(plan, {"value": "bad"}, AdapterRuntime())


@pytest.mark.parametrize(
    "change",
    [
        {"network": True},
        {"secrets": True},
        {"stateful": True},
        {"external_effect": True},
        {"execution_kind": "python"},
    ],
)
def test_effectful_or_non_wasm_adapter_is_rejected(change: dict[str, object]) -> None:
    adapter = structural_adapter(**change)
    planner = AdapterPlanner((adapter,))
    with pytest.raises(AdapterError, match="not structural"):
        planner.plan((adapter.adapter_id,))


def test_lossy_adapter_requires_explicit_profile_opt_in() -> None:
    adapter = structural_adapter(lossy=True)
    planner = AdapterPlanner((adapter,))
    with pytest.raises(AdapterError, match="explicit Profile opt-in"):
        planner.plan((adapter.adapter_id,))
    assert planner.plan((adapter.adapter_id,), allow_lossy=True) == (adapter,)


class FakeBackend:
    def __init__(self, status: BackendStatus) -> None:
        self.status = status


def test_backend_remains_unreachable_until_every_gate_is_ready() -> None:
    binding = OperationCatalog((artifact(),), (route(),)).resolve(
        "io.tobkiri.math.v1",
        "increment",
        ">=1",
    )
    status = BackendStatus(
        backend_id="wasmtime",
        execution_kind=ExecutionKind.WASM,
        platform="macos-arm64",
        backend_digest=digest("b"),
        production_enabled=True,
        conformance_only=False,
        satisfied_gates=REQUIRED_PRODUCTION_GATES - {"audit"},
    )
    registry = BackendRegistry((FakeBackend(status),))
    with pytest.raises(BackendUnavailableError, match="audit"):
        registry.select(binding)
    ready = replace(status, satisfied_gates=REQUIRED_PRODUCTION_GATES)
    assert BackendRegistry((FakeBackend(ready),)).select(binding).status == ready


def test_base_shell_resolution_is_capability_not_technology_based() -> None:
    base = BaseDefinition(
        pack_id="defaults.base",
        artifact_digest=digest("1"),
        definition_revision=digest("b"),
        policy_digest=digest("p"),
        dependency_artifacts=(),
        required_shell_capabilities=frozenset({"navigation", "commands"}),
        permitted_families=frozenset({PresentationFamily.GRAPHICAL, PresentationFamily.TERMINAL}),
    )
    shell = ShellDefinition(
        provider_id="shell.cli.default",
        pack_id="shell.cli.pack",
        artifact_digest=digest("2"),
        definition_revision=digest("s"),
        contract_id="app.shell.v1",
        family=PresentationFamily.TERMINAL,
        capabilities=frozenset({"navigation", "commands", "table"}),
        local_auth_protocol="io.tobkiri.local-auth.v1",
        local_auth_audience="runtime-profile",
        technology="rust",
    )
    contributions = (
        PresentationContribution(
            "settings.cli",
            digest("3"),
            "cli.command.contribution.v1",
            PresentationFamily.TERMINAL,
        ),
        PresentationContribution(
            "settings.gui",
            digest("4"),
            "ui.route.contribution.v1",
            PresentationFamily.GRAPHICAL,
        ),
    )
    binding = BaseShellResolver().resolve(base, shell, contributions)
    assert [item.contribution_id for item in binding.contributions] == ["settings.cli"]
    assert binding.binding_revision.startswith("sha256:")


def test_base_shell_resolution_does_not_silently_change_family() -> None:
    base = BaseDefinition(
        pack_id="headless.base",
        artifact_digest=digest("5"),
        definition_revision=digest("b"),
        policy_digest=digest("p"),
        dependency_artifacts=(),
        required_shell_capabilities=frozenset(),
        permitted_families=frozenset({PresentationFamily.HEADLESS}),
    )
    shell = ShellDefinition(
        provider_id="shell.gui",
        pack_id="shell.gui.pack",
        artifact_digest=digest("6"),
        definition_revision=digest("s"),
        contract_id="app.shell.v1",
        family=PresentationFamily.GRAPHICAL,
        capabilities=frozenset(),
        local_auth_protocol="io.tobkiri.local-auth.v1",
        local_auth_audience="runtime-profile",
    )
    with pytest.raises(ResolutionError, match="not permitted"):
        BaseShellResolver().resolve(base, shell, ())
