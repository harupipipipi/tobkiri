from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core_runtime.global_contracts.models import (
    Cardinality,
    ContractDescriptor,
    ContractRequirement,
    FailureSemantics,
    LifecycleMetadata,
    ProviderDescriptor,
    SecurityClassification,
)
from core_runtime.global_contracts.registry import ContractRegistry
from core_runtime.pack_repair import (
    PackRepairError,
    PackRepairManager,
    build_pack_conflict_report,
)
from tobkiri_protocol.validation import validate_document


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _packs() -> list[dict[str, str]]:
    return [
        {
            "pack_id": "fixture.alpha",
            "version": "1.2.0",
            "artifact_hash": _digest("a"),
            "provider_instance_id": "provider.alpha",
        },
        {
            "pack_id": "fixture.beta",
            "version": "2.0.0",
            "artifact_hash": _digest("b"),
            "provider_instance_id": "provider.beta",
        },
    ]


def _report(kind: str = "ambiguous_one_provider") -> dict[str, Any]:
    return build_pack_conflict_report(
        kind=kind,
        profile_id="fixture.profile",
        profile_fingerprint=_digest("c"),
        involved_packs=_packs(),
        affected_contracts=("rumi.action.fixture.v1",),
        schemas=({"type": "object", "properties": {"value": {"type": "string"}}},),
        diagnostics=("fixture conflict",),
    )


def _manager(tmp_path: Path) -> PackRepairManager:
    return PackRepairManager(tmp_path / "repair.sqlite", tmp_path / "generated")


def _selection_output(**extra: Any) -> dict[str, Any]:
    return {
        "display_name": "Fixture selection repair",
        "repair": {
            "kind": "provider_selection",
            "selected_provider_instance_id": "provider.alpha",
        },
        "resources": {},
        **extra,
    }


def _generated_selection(
    manager: PackRepairManager,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    report = manager.register_conflict(_report())
    plan = manager.plan(
        report["conflict_id"],
        repair_kind="provider_selection",
        generation_run_id="fake.generator.run",
    )
    generated = manager.generate(plan["plan_id"], lambda request: _selection_output())
    return report, plan, generated


def test_conflict_report_is_stable_schema_valid_and_source_free() -> None:
    first = _report()
    second = _report()

    assert first == second
    assert first["conflict_id"].startswith("pcf_")
    assert first["safe_repair_kinds"] == ["provider_selection"]
    assert validate_document(first, "pack_conflict_report") == first
    serialized = json.dumps(first, sort_keys=True)
    assert "source_path" not in serialized
    assert "secret" not in serialized.casefold()


def test_ambiguous_provider_fake_ai_lifecycle_is_explicit_and_removable(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    report, _plan, generated = _generated_selection(manager)

    assert generated["state"] == "generated"
    assert generated["approval"] is None
    assert generated["installed"] is False
    assert generated["active"] is False
    pack_root = Path(generated["pack_root"])
    assert (pack_root / "pack.v4.json").is_file()
    repair = validate_document(
        (pack_root / "repair.v1.json").read_bytes(),
        "generated_repair_pack",
    )
    assert repair["generated_for"]["packs"] == _packs()
    assert repair["permissions"] == []

    validated = manager.validate(
        generated["repair_id"],
        [*_packs(), {"pack_id": "unrelated.pack", "artifact_hash": _digest("d")}],
    )
    assert validated["validation"]["dry_run"]["resolved"] is True
    with pytest.raises(PackRepairError, match="generator cannot approve"):
        manager.approve(
            generated["repair_id"],
            actor_id="fake.generator.run",
            artifact_hash=generated["artifact_hash"],
        )
    approved = manager.approve(
        generated["repair_id"],
        actor_id="reviewer.one",
        artifact_hash=generated["artifact_hash"],
    )
    assert approved["state"] == "approved"
    assert manager.install(generated["repair_id"], _packs())["state"] == "installed"
    assert manager.activate(generated["repair_id"])["state"] == "active"
    assert manager.resolution_status(report["conflict_id"])["resolved"] is True

    removed = manager.remove(generated["repair_id"])
    assert removed["state"] == "removed"
    assert pack_root.is_dir()
    assert manager.resolution_status(report["conflict_id"])["resolved"] is False


def test_fixture_schema_adapter_resolves_only_after_proof(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    report = manager.register_conflict(_report("compatible_schema_version"))
    plan = manager.plan(
        report["conflict_id"],
        repair_kind="schema_adapter",
        generation_run_id="fake.adapter.run",
    )
    generated = manager.generate(
        plan["plan_id"],
        lambda request: {
            "repair": {"kind": "schema_adapter", "schema_compatibility": "fixture_proven"},
            "resources": {
                "resources/adapter.fixture.json": {
                    "from": "rumi.action.fixture.v1",
                    "to": "rumi.action.fixture.v1",
                    "fixture": "compatible",
                }
            },
        },
    )

    validated = manager.validate(generated["repair_id"], _packs())
    assert validated["state"] == "validated"
    assert validated["validation"]["executed_entrypoints"] is False


def test_semantic_conflict_stays_blocked_with_manual_resolution(tmp_path: Path) -> None:
    report = _report("incompatible_semantic")
    assert report["repairable"] is False
    assert report["safe_repair_kinds"] == []
    manager = _manager(tmp_path)
    registered = manager.register_conflict(report)
    with pytest.raises(PackRepairError) as error:
        manager.plan(
            registered["conflict_id"],
            repair_kind="schema_adapter",
            generation_run_id="fake.generator.run",
        )
    assert error.value.code == "MANUAL_RESOLUTION_REQUIRED"


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"requested_capabilities": ["network"]}, "CAPABILITY_EXPANSION_FORBIDDEN"),
        ({"api_key": "not-allowed"}, "SENSITIVE_DATA_FORBIDDEN"),
    ],
)
def test_malicious_generator_fields_are_rejected_without_artifact(
    tmp_path: Path,
    extra: dict[str, Any],
    code: str,
) -> None:
    manager = _manager(tmp_path)
    report = manager.register_conflict(_report())
    plan = manager.plan(
        report["conflict_id"],
        repair_kind="provider_selection",
        generation_run_id="fake.generator.run",
    )

    with pytest.raises(PackRepairError) as error:
        manager.generate(plan["plan_id"], lambda request: _selection_output(**extra))
    assert error.value.code == code
    assert list((tmp_path / "generated").iterdir()) == []


def test_private_source_coupling_is_rejected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    report = manager.register_conflict(_report())
    plan = manager.plan(
        report["conflict_id"],
        repair_kind="provider_selection",
        generation_run_id="fake.generator.run",
    )
    output = _selection_output(
        resources={
            "resources/bridge.json": {
                "kind": "import",
                "target": "ecosystem.fixture.alpha.private.module",
            }
        }
    )

    with pytest.raises(PackRepairError) as error:
        manager.generate(plan["plan_id"], lambda request: output)
    assert error.value.code == "PRIVATE_SOURCE_COUPLING"
    assert list((tmp_path / "generated").iterdir()) == []


def test_source_change_after_approval_revokes_and_marks_stale(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _report_value, _plan, generated = _generated_selection(manager)
    manager.validate(generated["repair_id"], _packs())
    manager.approve(
        generated["repair_id"],
        actor_id="reviewer.one",
        artifact_hash=generated["artifact_hash"],
    )
    changed = [dict(item) for item in _packs()]
    changed[0]["artifact_hash"] = _digest("e")

    stale = manager.validate(generated["repair_id"], changed)
    assert stale["state"] == "stale"
    assert stale["approval"] is None
    assert stale["installed"] is False
    assert stale["active"] is False


def test_artifact_change_after_approval_invalidates_approval(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _report_value, _plan, generated = _generated_selection(manager)
    manager.validate(generated["repair_id"], _packs())
    manager.approve(
        generated["repair_id"],
        actor_id="reviewer.one",
        artifact_hash=generated["artifact_hash"],
    )
    (Path(generated["pack_root"]) / "tampered.txt").write_text("changed", encoding="utf-8")

    modified = manager.validate(generated["repair_id"], _packs())
    assert modified["state"] == "modified"
    assert modified["approval"] is None
    events = manager.audit_events(generated["conflict_id"])
    assert events[-1]["event_type"] == "repair.approval_invalidated"


@pytest.mark.parametrize("failure", [RuntimeError("offline"), None])
def test_generator_failure_keeps_conflict_and_sources_unchanged(
    tmp_path: Path,
    failure: Exception | None,
) -> None:
    source_roots = [tmp_path / "alpha", tmp_path / "beta"]
    for index, root in enumerate(source_roots):
        root.mkdir()
        (root / "source.txt").write_text(f"source-{index}", encoding="utf-8")
    before = [(root / "source.txt").read_bytes() for root in source_roots]
    manager = _manager(tmp_path)
    report = manager.register_conflict(_report())
    plan = manager.plan(
        report["conflict_id"],
        repair_kind="provider_selection",
        generation_run_id="fake.generator.run",
    )

    def generator(request: dict[str, Any]) -> dict[str, Any]:
        if failure is not None:
            raise failure
        return {"repair": {"kind": "unknown"}}

    with pytest.raises(PackRepairError):
        manager.generate(plan["plan_id"], generator)
    assert manager.get_conflict(report["conflict_id"]) == report
    assert manager.resolution_status(report["conflict_id"])["resolved"] is False
    assert before == [(root / "source.txt").read_bytes() for root in source_roots]
    assert list((tmp_path / "generated").iterdir()) == []


def test_vendor_neutral_operations_keep_generation_and_activation_separate(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    report = manager.register_conflict(_report())
    assert manager.dispatch("pack.conflicts.list", {}) == [report]
    plan = manager.dispatch(
        "pack.repair.plan",
        {
            "conflict_id": report["conflict_id"],
            "repair_kind": "provider_selection",
            "generation_run_id": "fake.generator.run",
        },
    )
    with pytest.raises(PackRepairError) as unavailable:
        manager.dispatch("pack.repair.generate", {"plan_id": plan["plan_id"]})
    assert unavailable.value.code == "GENERATOR_UNAVAILABLE"

    generated = manager.dispatch(
        "pack.repair.generate",
        {"plan_id": plan["plan_id"]},
        generator=lambda request: _selection_output(),
    )
    assert generated["state"] == "generated"
    assert manager.dispatch(
        "pack.repair.status", {"conflict_id": report["conflict_id"]}
    )["resolved"] is False


def test_secret_values_are_rejected_before_ai_input_or_audit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    report = _report()
    report["diagnostics"] = ["Bearer " + "x" * 32]

    with pytest.raises(PackRepairError) as error:
        manager.register_conflict(report)
    assert error.value.code == "SENSITIVE_DATA_FORBIDDEN"
    assert manager.list_conflicts() == []


def test_contract_registry_emits_repairable_machine_report() -> None:
    registry = ContractRegistry()
    descriptor = ContractDescriptor(
        contract_id="rumi.action.fixture.v1",
        version="1.0.0",
        cardinality=Cardinality.ONE,
        security=SecurityClassification.PUBLIC,
        failure=FailureSemantics.FAIL_CLOSED,
        lifecycle=LifecycleMetadata(introduced="1.0.0"),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    for suffix, digest in (("alpha", "a"), ("beta", "b")):
        result = registry.register(
            ProviderDescriptor(
                contract=descriptor,
                provider_instance_id=f"provider.{suffix}",
                source_pack_id=f"fixture.{suffix}",
                source_pack_version="1.0.0",
                content_hash=_digest(digest),
                build_identity=f"build.{suffix}",
                trust_class="verified",
                isolation="process",
            )
        )
        assert result.ok

    resolved = registry.resolve(
        ContractRequirement(
            contract_id=descriptor.contract_id,
            version_range=">=1.0.0 <2.0.0",
            cardinality=Cardinality.ONE,
        )
    )

    assert not resolved.ok
    conflict = resolved.metadata["conflict_report"]
    assert conflict["kind"] == "ambiguous_one_provider"
    assert conflict["safe_repair_kinds"] == ["provider_selection"]
    assert conflict == registry.resolve(
        ContractRequirement(
            contract_id=descriptor.contract_id,
            version_range=">=1.0.0 <2.0.0",
            cardinality=Cardinality.ONE,
        )
    ).metadata["conflict_report"]
