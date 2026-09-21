"""Focused contract tests for the Wave-0 pack foundation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
from jsonschema import Draft202012Validator

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
    is_compatible,
    load_manifest,
    parse_version,
    validate_version_range,
)
from core_runtime.interface_registry import InterfaceRegistry

ROOT = Path(__file__).parents[1]
PACK_SCHEMA = ROOT / "schemas" / "pack_manifest_v3.schema.json"
RESULT_SCHEMA = ROOT / "schemas" / "global_contract_types.schema.json"
EXAMPLE = ROOT / "examples" / "pack_v3" / "minimal_service.json"
CONTRACT_EXAMPLES = ROOT / "examples" / "pack_v3" / "contract_examples.json"
FIXTURES = ROOT / "tests" / "fixtures" / "pack_v3"
ECHO_CONTRACT = "rumi.service.example.echo.v1"


def _manifest() -> dict[str, Any]:
    """Return an isolated copy of the checked-in example manifest."""
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _write_manifest(tmp_path: Path, manifest: Any) -> Path:
    """Write one temporary manifest for a loader boundary test."""
    path = tmp_path / "ecosystem.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _provider(
    instance_id: str,
    *,
    contract_id: str = ECHO_CONTRACT,
    cardinality: Cardinality = Cardinality.ONE,
    version: str = "1.2.0",
    priority: int = 0,
    instance_key: str | None = None,
    before: tuple[str, ...] = (),
    after: tuple[str, ...] = (),
) -> ProviderDescriptor:
    """Build deterministic provider metadata for focused registry tests."""
    descriptor = ContractDescriptor(
        contract_id=contract_id,
        version=version,
        cardinality=cardinality,
        security=SecurityClassification.INTERNAL,
        failure=FailureSemantics.FAIL_CLOSED,
        lifecycle=LifecycleMetadata(introduced="1.0.0"),
    )
    safe_instance = instance_id.lower().replace(":", "-")
    return ProviderDescriptor(
        contract=descriptor,
        provider_instance_id=instance_id,
        source_pack_id=f"test.{safe_instance}",
        source_pack_version="1.0.0",
        content_hash=content_identity({"instance": instance_id}),
        build_identity="focused-test",
        trust_class="untrusted",
        isolation="in_process",
        priority=priority,
        instance_key=instance_key,
        before=before,
        after=after,
    )


def _requirement(
    cardinality: Cardinality = Cardinality.ONE,
    *,
    contract_id: str = ECHO_CONTRACT,
    version_range: str = "^1.0.0",
    optional: bool = False,
    instance_key: str | None = None,
) -> ContractRequirement:
    """Build one typed registry requirement."""
    return ContractRequirement(
        contract_id=contract_id,
        version_range=version_range,
        cardinality=cardinality,
        optional=optional,
        instance_key=instance_key,
    )


def test_schemas_are_valid_and_example_returns_portable_result() -> None:
    """Both production schemas and the complete example validate."""
    pack_schema = json.loads(PACK_SCHEMA.read_text(encoding="utf-8"))
    result_schema = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(pack_schema)
    Draft202012Validator.check_schema(result_schema)
    assert not list(
        Draft202012Validator(pack_schema).iter_errors(_manifest())
    )

    result = load_manifest(EXAMPLE)
    assert result.status is ContractStatus.OK
    assert result.value is not None
    assert result.value["content_identity"].startswith("sha256:")
    assert not list(
        Draft202012Validator(result_schema).iter_errors(result.to_dict())
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unknown": True}),
        lambda value: value["pack"].update({"version": "latest"}),
        lambda value: value["contracts"]["provides"][0].update(
            {"cardinality": "sometimes"}
        ),
        lambda value: value["provenance"].update({"trust_class": "trusted"}),
    ],
)
def test_invalid_manifest_fields_fail_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    """Unknown and unsafe manifest values return typed diagnostics."""
    manifest = _manifest()
    mutation(manifest)
    result = load_manifest(_write_manifest(tmp_path, manifest))
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert result.value is None
    assert result.diagnostics


@pytest.mark.parametrize(
    ("raw_json", "diagnostic"),
    [
        (
            '{"pack_api_version":"rumi.pack.v3",'
            '"pack_api_version":"rumi.pack.v3"}',
            "duplicate JSON object key",
        ),
        ('{"pack_api_version":NaN}', "non-finite JSON number"),
    ],
)
def test_manifest_parser_rejects_ambiguous_json(
    tmp_path: Path,
    raw_json: str,
    diagnostic: str,
) -> None:
    """Duplicate keys and non-finite numbers never reach schema validation."""
    path = tmp_path / "ecosystem.json"
    path.write_text(raw_json, encoding="utf-8")
    result = load_manifest(path)
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert diagnostic in result.diagnostics[0]


def test_fixed_negative_and_migration_fixtures_are_actionable() -> None:
    """Checked-in fixtures cover fail-closed and one-way migration behavior."""
    invalid = load_manifest(FIXTURES / "invalid_unknown_field.json")
    assert invalid.status is ContractStatus.INVALID_MANIFEST
    assert invalid.diagnostics

    migration = json.loads(
        (FIXTURES / "legacy_migration.json").read_text(encoding="utf-8")
    )
    assert migration["projection"] == "legacy_to_v3_read_only"
    assert migration["owner"] == "core_runtime.interface_registry"
    assert migration["removal_wave"] == 10
    assert migration["rollback"]


def test_contract_examples_cover_the_frozen_cardinalities() -> None:
    """The data-only examples enumerate every Wave-0 resolution mode."""
    examples = json.loads(CONTRACT_EXAMPLES.read_text(encoding="utf-8"))
    cardinalities = {item["cardinality"] for item in examples}
    assert {"one", "many", "keyed", "chain", "fanout"} <= cardinalities
    assert all(item["id"].startswith("rumi.") for item in examples)


def test_canonical_identity_is_stable_and_rejects_non_json_values() -> None:
    """Canonical bytes preserve Unicode and reject non-finite numbers."""
    first = {"z": 1, "a": "トブキリ", "nested": {"b": False}}
    second = {"nested": {"b": False}, "a": "トブキリ", "z": 1}
    assert canonical_json(first) == canonical_json(second)
    assert canonical_json(first).decode("utf-8").startswith('{"a":"トブキリ"')
    assert content_identity({}) == (
        "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    with pytest.raises(ValueError, match="canonical JSON"):
        canonical_json(float("nan"))


def test_semver_ranges_are_deterministic_and_fail_closed() -> None:
    """Exact, caret, tilde, and comparator ranges use strict SemVer."""
    assert parse_version("1.2.3+build.5") == parse_version("1.2.3+other")
    assert is_compatible("1.9.0", "^1.2.0")
    assert not is_compatible("2.0.0", "^1.2.0")
    assert is_compatible("1.2.9", "~1.2.0")
    assert not is_compatible("1.3.0", "~1.2.0")
    assert is_compatible("1.5.0", ">=1.0.0 <2.0.0")
    validate_version_range(">=1.0.0 <2.0.0")
    with pytest.raises(ValueError, match="leading zeros"):
        parse_version("1.0.0-01")
    with pytest.raises(ValueError, match="invalid semantic version"):
        validate_version_range("^latest")


def test_manifest_rejects_later_provided_version_with_leading_zero(
    tmp_path: Path,
) -> None:
    """Every provided version is validated at the loader boundary."""
    manifest = _manifest()
    later = json.loads(json.dumps(manifest["contracts"]["provides"][0]))
    later["provider_instance_id"] = "echo.remote"
    later["version"] = "1.0.0-01"
    manifest["contracts"]["provides"].append(later)

    result = load_manifest(_write_manifest(tmp_path, manifest))

    assert result.status is ContractStatus.INVALID_MANIFEST
    assert result.value is None
    assert result.diagnostics == (
        "$['contracts']['provides'][1]['version']: "
        "numeric prerelease identifiers cannot have leading zeros",
    )


def test_manifest_rejects_requirement_range_with_leading_zero(
    tmp_path: Path,
) -> None:
    """Every required range is validated at the loader boundary."""
    manifest = _manifest()
    manifest["contracts"]["requires"].append(
        {
            "id": ECHO_CONTRACT,
            "version_range": ">=1.0.0-01 <2.0.0",
            "cardinality": "one",
            "optional": False,
        }
    )

    result = load_manifest(_write_manifest(tmp_path, manifest))

    assert result.status is ContractStatus.INVALID_MANIFEST
    assert result.value is None
    assert result.diagnostics == (
        "$['contracts']['requires'][0]['version_range']: "
        "numeric prerelease identifiers cannot have leading zeros",
    )


def test_manifest_accepts_valid_prerelease_and_comparator_range(
    tmp_path: Path,
) -> None:
    """Strict prerelease versions and comparator ranges remain valid."""
    manifest = _manifest()
    manifest["contracts"]["provides"][0]["version"] = "1.0.0-rc.1"
    manifest["contracts"]["requires"].append(
        {
            "id": ECHO_CONTRACT,
            "version_range": ">=1.0.0-rc.1 <2.0.0",
            "cardinality": "one",
            "optional": False,
        }
    )

    result = load_manifest(_write_manifest(tmp_path, manifest))

    assert result.status is ContractStatus.OK
    assert result.value is not None
    assert result.diagnostics == ()


def test_manifest_discovery_does_not_import_entrypoint(tmp_path: Path) -> None:
    """Discovery treats entrypoints as data and never imports their modules."""
    sentinel = tmp_path / "was_imported"
    module = tmp_path / "dangerous_provider.py"
    module.write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).touch()\n",
        encoding="utf-8",
    )
    manifest = _manifest()
    manifest["contracts"]["provides"][0]["id"] = (
        "rumi.service.dangerous.v1"
    )
    manifest["entrypoints"][0]["contract_id"] = (
        "rumi.service.dangerous.v1"
    )
    manifest["entrypoints"][0]["module"] = "dangerous_provider"
    loaded = load_manifest(_write_manifest(tmp_path, manifest))
    assert loaded.status is ContractStatus.OK
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        (
            lambda value: value["entrypoints"][0].update(
                {"contract_id": "rumi.service.missing.v1"}
            ),
            "not provided",
        ),
        (
            lambda value: value["contracts"]["provides"][0].update(
                {"version": "2.0.0"}
            ),
            "major",
        ),
    ],
)
def test_manifest_cross_field_conflicts_fail_closed(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    diagnostic: str,
) -> None:
    """Entrypoint and version identities must agree across the manifest."""
    manifest = _manifest()
    mutation(manifest)
    result = load_manifest(_write_manifest(tmp_path, manifest))
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert any(diagnostic in item for item in result.diagnostics)


def test_manifest_rejects_duplicate_provider_identity(tmp_path: Path) -> None:
    """One opaque provider identity cannot be declared twice."""
    manifest = _manifest()
    duplicate = dict(manifest["contracts"]["provides"][0])
    duplicate["version"] = "1.1.0"
    manifest["contracts"]["provides"].append(duplicate)
    result = load_manifest(_write_manifest(tmp_path, manifest))
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert any("duplicate provider" in item for item in result.diagnostics)


def test_manifest_allows_multiple_consistent_providers(tmp_path: Path) -> None:
    """Many-provider manifests may repeat a contract with unique instances."""
    manifest = _manifest()
    first = manifest["contracts"]["provides"][0]
    first["cardinality"] = "many"
    second = json.loads(json.dumps(first))
    second["provider_instance_id"] = "echo.remote"
    manifest["contracts"]["provides"].append(second)
    result = load_manifest(_write_manifest(tmp_path, manifest))
    assert result.status is ContractStatus.OK


def test_manifest_rejects_inconsistent_provider_cardinality(
    tmp_path: Path,
) -> None:
    """Providers for one contract cannot disagree about cardinality."""
    manifest = _manifest()
    second = json.loads(
        json.dumps(manifest["contracts"]["provides"][0])
    )
    second["provider_instance_id"] = "echo.remote"
    second["cardinality"] = "many"
    manifest["contracts"]["provides"].append(second)
    result = load_manifest(_write_manifest(tmp_path, manifest))
    assert result.status is ContractStatus.INVALID_MANIFEST
    assert any("share cardinality" in item for item in result.diagnostics)


def test_typed_models_reject_invalid_selection_metadata() -> None:
    """Keyed and chain-only fields fail before registry mutation."""
    with pytest.raises(ValueError, match="requires instance_key"):
        _requirement(Cardinality.KEYED)
    with pytest.raises(ValueError, match="only for keyed requirements"):
        _requirement(Cardinality.ONE, instance_key="unexpected")
    with pytest.raises(ValueError, match="only for chain providers"):
        _provider("not-chain", before=("other",))
    with pytest.raises(ValueError, match="cannot depend on itself"):
        _provider(
            "self",
            cardinality=Cardinality.CHAIN,
            before=("self",),
        )


def test_one_resolution_rejects_equal_priority_ambiguity() -> None:
    """Filesystem or pack order never decides an ambiguous one contract."""
    registry = ContractRegistry()
    registry.register(_provider("provider-b"))
    registry.register(_provider("provider-a"))
    result = registry.resolve(_requirement())
    assert result.status is ContractStatus.INCOMPATIBLE
    assert "provider-a, provider-b" in result.diagnostics[0]


def test_one_resolution_uses_unique_highest_priority() -> None:
    """A single highest-priority one provider resolves deterministically."""
    registry = ContractRegistry()
    registry.register(_provider("fallback", priority=1))
    registry.register(_provider("primary", priority=2))
    result = registry.resolve(_requirement())
    assert result.status is ContractStatus.OK
    assert result.value and result.value[0].provider_instance_id == "primary"


@pytest.mark.parametrize(
    "cardinality",
    [Cardinality.MANY, Cardinality.FANOUT],
)
def test_multi_provider_cardinalities_are_stably_ordered(
    cardinality: Cardinality,
) -> None:
    """Many and fanout use priority then stable opaque identity."""
    registry = ContractRegistry()
    registry.register(
        _provider("second", cardinality=cardinality, priority=1)
    )
    registry.register(
        _provider("first", cardinality=cardinality, priority=2)
    )
    result = registry.resolve(_requirement(cardinality))
    assert [item.provider_instance_id for item in result.value or ()] == [
        "first",
        "second",
    ]


def test_keyed_and_optional_absence_semantics_are_explicit() -> None:
    """Keyed selection and optional absence never collapse to false values."""
    registry = ContractRegistry()
    registry.register(
        _provider(
            "keyed-one",
            cardinality=Cardinality.KEYED,
            instance_key="primary",
        )
    )
    keyed = registry.resolve(
        _requirement(Cardinality.KEYED, instance_key="primary")
    )
    assert keyed.status is ContractStatus.OK
    assert keyed.value and keyed.value[0].provider_instance_id == "keyed-one"

    optional = ContractRegistry().resolve(
        _requirement(Cardinality.OPTIONAL, optional=True)
    )
    assert optional.status is ContractStatus.NOT_CONFIGURED
    assert optional.value is None


def test_chain_resolution_orders_dependencies_and_rejects_cycle() -> None:
    """Chain dependencies resolve topologically and fail on cycles."""
    registry = ContractRegistry()
    registry.register(
        _provider(
            "guard",
            cardinality=Cardinality.CHAIN,
            before=("execute",),
        )
    )
    registry.register(
        _provider(
            "execute",
            cardinality=Cardinality.CHAIN,
            before=("audit",),
        )
    )
    registry.register(_provider("audit", cardinality=Cardinality.CHAIN))
    resolved = registry.resolve(_requirement(Cardinality.CHAIN))
    assert [item.provider_instance_id for item in resolved.value or ()] == [
        "guard",
        "execute",
        "audit",
    ]

    cyclic = ContractRegistry()
    cyclic.register(
        _provider("a", cardinality=Cardinality.CHAIN, after=("b",))
    )
    cyclic.register(
        _provider("b", cardinality=Cardinality.CHAIN, after=("a",))
    )
    assert (
        cyclic.resolve(_requirement(Cardinality.CHAIN)).status
        is ContractStatus.INCOMPATIBLE
    )


def test_chain_resolution_rejects_unknown_dependency() -> None:
    """A chain cannot silently omit a named dependency."""
    registry = ContractRegistry()
    registry.register(
        _provider(
            "guard",
            cardinality=Cardinality.CHAIN,
            before=("missing",),
        )
    )
    result = registry.resolve(_requirement(Cardinality.CHAIN))
    assert result.status is ContractStatus.INCOMPATIBLE
    assert "unknown chain target" in result.diagnostics[0]


def test_stale_registry_revision_is_explicit() -> None:
    """A changed provider set never silently reuses a stale resolution."""
    registry = ContractRegistry()
    stale_revision = registry.resolution_identity()
    registry.register(_provider("new-provider", priority=1))
    result = registry.resolve(
        _requirement(),
        expected_revision=stale_revision,
    )
    assert result.status is ContractStatus.STALE_RESOLUTION
    assert result.metadata["revision"] == registry.resolution_identity()


def test_opaque_action_client_binds_contract_identity() -> None:
    """Opaque clients invoke without source paths and reject forged identity."""
    registry = ContractRegistry()
    provider = _provider("echo-primary", priority=10)
    registry.register(
        provider,
        lambda operation, payload: {operation: payload["value"]},
    )
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

    forged = ActionClient[dict[str, str]](
        provider.provider_instance_id,
        "rumi.service.example.other.v1",
        provider.contract.version,
        registry,
    )
    assert forged.call("echo", {"value": "no"}).status is (
        ContractStatus.INCOMPATIBLE
    )


def test_duplicate_opaque_identity_cannot_replace_operation() -> None:
    """A duplicate instance ID in another contract cannot replace a handler."""
    registry = ContractRegistry()
    original = _provider("shared")
    duplicate = _provider(
        "shared",
        contract_id="rumi.action.example.echo.v1",
    )
    assert registry.register(
        original, lambda operation, payload: "original"
    ).ok
    rejected = registry.register(
        duplicate, lambda operation, payload: "replacement"
    )
    assert rejected.status is ContractStatus.INCOMPATIBLE

    client = ActionClient[str](
        original.provider_instance_id,
        original.contract.contract_id,
        original.contract.version,
        registry,
    )
    assert client.call("read", {}).value == "original"


def test_provider_failures_return_non_lossy_statuses() -> None:
    """Permission and provider failures retain distinct typed statuses."""
    denied_registry = ContractRegistry()
    denied = _provider("denied")

    def deny(_operation: str, _payload: dict[str, Any]) -> None:
        raise PermissionError("approval required")

    denied_registry.register(denied, deny)
    denied_client = ActionClient[Any](
        denied.provider_instance_id,
        denied.contract.contract_id,
        denied.contract.version,
        denied_registry,
    )
    assert denied_client.call("write", {}).status is ContractStatus.DENIED

    failed_registry = ContractRegistry()
    failed = _provider("failed")

    def fail(_operation: str, _payload: dict[str, Any]) -> None:
        raise RuntimeError("sensitive detail")

    failed_registry.register(failed, fail)
    failed_client = ActionClient[Any](
        failed.provider_instance_id,
        failed.contract.contract_id,
        failed.contract.version,
        failed_registry,
    )
    result = failed_client.call("read", {})
    assert result.status is ContractStatus.UNAVAILABLE
    assert result.diagnostics == (
        "provider operation failed: RuntimeError",
    )


def test_legacy_projection_is_read_only_and_deterministic() -> None:
    """Legacy registration projects one way without v3 dual-write."""
    legacy = InterfaceRegistry()
    legacy.register(
        "defaults.echo",
        object(),
        {
            "_source_pack_id": "legacy.pack",
            "_source_pack_version": "1.2.0",
        },
    )
    before = legacy.list(include_meta=True)
    projection = LegacyRegistryProjection(
        legacy,
        (LegacyProjectionRule("defaults.", ECHO_CONTRACT),),
    )
    first = projection.snapshot()
    second = projection.snapshot()
    assert first == second
    assert legacy.list(include_meta=True) == before
    assert first[0].contract.lifecycle.data_owner == (
        "core_runtime.interface_registry"
    )
    assert first[0].provider_instance_id.startswith("legacy:")
    assert not hasattr(first[0], "source_path")


def test_registry_identity_excludes_executable_operation() -> None:
    """Registry revision is derived only from deterministic provider metadata."""
    first = ContractRegistry()
    second = ContractRegistry()
    provider = _provider("same-metadata")
    first.register(provider, lambda operation, payload: "first")
    second.register(provider, lambda operation, payload: "second")
    assert first.resolution_identity() == second.resolution_identity()
