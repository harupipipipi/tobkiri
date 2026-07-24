"""Contract tests for the Wave 0 pack manifest and registry foundation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
import core_runtime.global_contracts.manifest as manifest_module

from core_runtime.global_contracts import (
    ActionClient,
    Cardinality,
    ContractDescriptor,
    ContractRegistry,
    ContractRequirement,
    ContractStatus,
    FailureSemantics,
    LegacyProjectionRule,
    LegacyRegistryProjection,
    LifecycleMetadata,
    ProviderDescriptor,
    SecurityClassification,
    canonical_json,
    content_identity,
    load_manifest,
)
from core_runtime.interface_registry import InterfaceRegistry

ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "schemas" / "pack_manifest_v3.schema.json"
EXAMPLE = ROOT / "examples" / "pack_v3" / "minimal_service.json"
FIXTURES = ROOT / "tests" / "fixtures" / "pack_v3"


def _provider(
    instance_id: str,
    *,
    cardinality: Cardinality = Cardinality.ONE,
    version: str = "1.2.0",
    priority: int = 0,
    instance_key: str | None = None,
    before: tuple[str, ...] = (),
    after: tuple[str, ...] = (),
) -> ProviderDescriptor:
    descriptor = ContractDescriptor(
        contract_id="rumi.service.example.echo.v1",
        version=version,
        cardinality=cardinality,
        security=SecurityClassification.INTERNAL,
        failure=FailureSemantics.FAIL_CLOSED,
        lifecycle=LifecycleMetadata(introduced="1.0.0"),
    )
    return ProviderDescriptor(
        contract=descriptor,
        provider_instance_id=instance_id,
        source_pack_id=f"test.{instance_id}",
        source_pack_version="1.0.0",
        content_hash=content_identity({"instance": instance_id}),
        build_identity="test",
        trust_class="untrusted",
        isolation="in_process",
        priority=priority,
        instance_key=instance_key,
        before=before,
        after=after,
    )


def test_schema_is_valid_and_example_passes() -> None:
    """The production schema and complete example must validate."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(manifest))


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value.update({"unknown": True}), "Additional properties"),
        (lambda value: value["pack"].update({"version": "latest"}), "does not match"),
        (
            lambda value: value["contracts"]["provides"][0].update(
                {"cardinality": "sometimes"}
            ),
            "is not one of",
        ),
        (
            lambda value: value["provenance"].update({"trust_class": "trusted"}),
            "is not one of",
        ),
    ],
)
def test_invalid_manifest_fails_closed(mutation: object, expected: str) -> None:
    """Unknown or unsafe production fields must fail with diagnostics."""
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    mutation(manifest)  # type: ignore[operator]
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    messages = [error.message for error in Draft202012Validator(schema).iter_errors(manifest)]
    assert any(expected in message for message in messages)


def test_invalid_fixed_fixture_returns_invalid_manifest_status() -> None:
    """The checked-in invalid fixture must return the shared result status."""
    result = load_manifest(FIXTURES / "invalid_unknown_field.json")
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert result.value is None
    assert result.diagnostics


def test_missing_jsonschema_fails_closed_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The minimal desktop runtime isolates v3 metadata if validation is absent."""
    monkeypatch.setattr(manifest_module, "Draft202012Validator", None)
    monkeypatch.setattr(manifest_module, "FormatChecker", None)

    result = manifest_module.load_manifest(EXAMPLE)

    assert result.status is ContractStatus.INVALID_MANIFEST
    assert result.value is None
    assert "unavailable" in result.diagnostics[0]


def test_migration_fixture_declares_one_way_projection_and_rollback() -> None:
    """Migration evidence must name ownership, sunset, and rollback."""
    migration = json.loads(
        (FIXTURES / "legacy_migration.json").read_text(encoding="utf-8")
    )
    assert migration["projection"] == "legacy_to_v3_read_only"
    assert migration["owner"] == "core_runtime.interface_registry"
    assert migration["removal_wave"] == 10
    assert migration["rollback"]


def test_canonical_identity_fixed_vector() -> None:
    """Canonical JSON identity must remain stable across key order and Unicode."""
    value = {"z": 1, "a": "ルミ", "nested": {"b": False}}
    assert canonical_json(value) == b'{"a":"\xe3\x83\xab\xe3\x83\x9f","nested":{"b":false},"z":1}'
    assert content_identity({}) == (
        "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )


def test_manifest_discovery_does_not_import_entrypoint(tmp_path: Path) -> None:
    """Discovery treats an entrypoint as data and never imports its module."""
    sentinel = tmp_path / "was_imported"
    module = tmp_path / "dangerous_provider.py"
    module.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n")
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    manifest["contracts"]["provides"][0]["id"] = "rumi.service.dangerous.v1"
    manifest["entrypoints"][0]["contract_id"] = "rumi.service.dangerous.v1"
    manifest["entrypoints"][0]["module"] = "dangerous_provider"
    path = tmp_path / "ecosystem.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded = load_manifest(path)
    assert loaded.status is ContractStatus.OK
    assert loaded.value is not None
    assert loaded.diagnostics == ()
    assert not sentinel.exists()


def test_manifest_semantic_validation_rejects_unprovided_entrypoint(
    tmp_path: Path,
) -> None:
    """Entrypoints may only activate contracts declared by the same manifest."""
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    manifest["entrypoints"][0]["contract_id"] = "rumi.service.missing.v1"
    path = tmp_path / "ecosystem.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = load_manifest(path)
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert "not provided" in result.diagnostics[0]


def test_manifest_semantic_validation_rejects_major_version_mismatch(
    tmp_path: Path,
) -> None:
    """A v1 contract identifier must not claim a version with major 2."""
    manifest = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    manifest["contracts"]["provides"][0]["version"] = "2.0.0"
    path = tmp_path / "ecosystem.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = load_manifest(path)
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert "major" in result.diagnostics[0]


def test_one_resolution_rejects_equal_priority_ambiguity() -> None:
    """Filesystem or pack ID order must not decide an ambiguous one contract."""
    registry = ContractRegistry()
    registry.register(_provider("provider-b"))
    registry.register(_provider("provider-a"))
    result = registry.resolve(
        ContractRequirement(
            "rumi.service.example.echo.v1", "^1.0.0", Cardinality.ONE
        )
    )
    assert result.status is ContractStatus.INCOMPATIBLE
    assert "provider-a, provider-b" in result.diagnostics[0]


def test_opaque_action_client_invokes_without_source_path() -> None:
    """Consumers invoke an opaque instance handle without provider source data."""
    registry = ContractRegistry()
    provider = _provider("echo-primary", priority=10)
    registry.register(provider, lambda operation, payload: {operation: payload["value"]})
    client = ActionClient[dict[str, str]](
        provider.provider_instance_id,
        provider.contract.contract_id,
        provider.contract.version,
        registry,
    )
    result = client.call("echo", {"value": "ok"})
    assert result.status is ContractStatus.OK
    assert result.value == {"echo": "ok"}
    assert not hasattr(client, "source_path")


def test_legacy_projection_is_read_only_and_deterministic() -> None:
    """Legacy registrations project in one direction without v3 dual-write."""
    legacy = InterfaceRegistry()
    legacy.register("defaults.echo", object(), {"_source_pack_id": "legacy.pack"})
    before = legacy.list(include_meta=True)
    projection = LegacyRegistryProjection(
        legacy,
        (
            LegacyProjectionRule(
                "defaults.",
                "rumi.service.example.echo.v1",
            ),
        ),
    )
    first = projection.snapshot()
    second = projection.snapshot()
    assert first == second
    assert legacy.list(include_meta=True) == before
    assert first[0].contract.lifecycle.data_owner == "core_runtime.interface_registry"


def test_stale_registry_revision_is_explicit() -> None:
    """A changed provider set must not silently reuse a stale resolution."""
    registry = ContractRegistry()
    stale_revision = registry.resolution_identity()
    registry.register(_provider("new-provider", priority=1))
    result = registry.resolve(
        ContractRequirement(
            "rumi.service.example.echo.v1", "^1.0.0", Cardinality.ONE
        ),
        expected_revision=stale_revision,
    )
    assert result.status is ContractStatus.STALE_RESOLUTION


def test_chain_resolution_orders_dependencies_and_rejects_cycle() -> None:
    """Chain dependencies must resolve topologically and fail on cycles."""
    registry = ContractRegistry()
    registry.register(
        _provider("guard", cardinality=Cardinality.CHAIN, before=("execute",))
    )
    registry.register(
        _provider("execute", cardinality=Cardinality.CHAIN, before=("audit",))
    )
    registry.register(_provider("audit", cardinality=Cardinality.CHAIN))
    requirement = ContractRequirement(
        "rumi.service.example.echo.v1", "^1.0.0", Cardinality.CHAIN
    )
    resolved = registry.resolve(requirement)
    assert [item.provider_instance_id for item in resolved.value or ()] == [
        "guard",
        "execute",
        "audit",
    ]

    cyclic = ContractRegistry()
    cyclic.register(_provider("a", cardinality=Cardinality.CHAIN, after=("b",)))
    cyclic.register(_provider("b", cardinality=Cardinality.CHAIN, after=("a",)))
    assert cyclic.resolve(requirement).status is ContractStatus.INCOMPATIBLE


@pytest.mark.parametrize(
    "cardinality",
    [Cardinality.MANY, Cardinality.FANOUT],
)
def test_multi_provider_cardinalities_are_stably_ordered(
    cardinality: Cardinality,
) -> None:
    """Multi-provider resolution uses priority and stable instance identity."""
    registry = ContractRegistry()
    registry.register(_provider("second", cardinality=cardinality, priority=1))
    registry.register(_provider("first", cardinality=cardinality, priority=2))
    result = registry.resolve(
        ContractRequirement(
            "rumi.service.example.echo.v1", "^1.0.0", cardinality
        )
    )
    assert [item.provider_instance_id for item in result.value or ()] == [
        "first",
        "second",
    ]


def test_keyed_and_optional_absence_semantics_are_explicit() -> None:
    """Keyed selection and optional absence must never collapse to false values."""
    registry = ContractRegistry()
    registry.register(
        _provider(
            "keyed-one",
            cardinality=Cardinality.KEYED,
            instance_key="primary",
        )
    )
    keyed = registry.resolve(
        ContractRequirement(
            "rumi.service.example.echo.v1",
            "^1.0.0",
            Cardinality.KEYED,
            instance_key="primary",
        )
    )
    assert keyed.status is ContractStatus.OK
    assert keyed.value and keyed.value[0].provider_instance_id == "keyed-one"

    optional = ContractRegistry().resolve(
        ContractRequirement(
            "rumi.service.example.missing.v1",
            "^1.0.0",
            Cardinality.OPTIONAL,
            optional=True,
        )
    )
    assert optional.status is ContractStatus.NOT_CONFIGURED
    assert optional.value is None
