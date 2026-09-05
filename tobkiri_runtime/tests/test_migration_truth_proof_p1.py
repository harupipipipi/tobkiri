"""P1 regression tests for truthful, relocation-stable migration evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.quality import run_independent_migration_proof as proof_generator
from tests import test_complete_v4_migration_gate as complete_gate
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.models import ExecutionKind, PackageKind


def test_profile_transaction_receipt_is_checkout_path_independent(
    tmp_path: Path,
) -> None:
    """Relocating identical legacy inputs cannot change the transaction proof."""

    source = proof_generator._load_json(proof_generator.PROFILE_FIXTURE)
    identity = proof_generator._identity_proof(source)
    receipts = []
    for checkout_name in ("checkout-a", "checkout-b"):
        workspace_root = tmp_path / checkout_name / "legacy_profile_bundle"
        shutil.copytree(proof_generator.PROFILE_WORKSPACE_FIXTURE, workspace_root)
        receipts.append(
            proof_generator._run_profile_transaction_proof(
                source,
                identity,
                tmp_path / f"transaction-{checkout_name}",
                workspace_root=workspace_root,
            )
        )

    assert receipts[0] == receipts[1]


def test_repository_labels_are_stable_after_checkout_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence paths are relative labels, never relocated absolute paths."""

    labels = []
    for checkout_name in ("checkout-a", "checkout-b"):
        checkout = tmp_path / checkout_name
        source = checkout / "tobkiri_runtime" / "legacy.json"
        source.parent.mkdir(parents=True)
        source.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(proof_generator, "REPOSITORY_ROOT", checkout)
        labels.append(proof_generator._label(source))

    assert labels == ["tobkiri_runtime/legacy.json"] * 2
    assert str(tmp_path) not in labels[0]


def test_complete_gate_runs_generator_check_and_rejects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checked-in proof edit is caught by the generator invoked by the gate."""

    payload = json.loads(complete_gate.MIGRATION_PROOF_PATH.read_text(encoding="utf-8"))
    payload["packs"]["defaults"]["semantic_comparison"]["reason"] = "drifted"
    drifted = tmp_path / "pack_migration_proof.v1.json"
    drifted.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(complete_gate, "MIGRATION_PROOF_PATH", drifted)

    findings = complete_gate._migration_proof_generator_findings()

    assert [finding["rule"] for finding in findings] == ["migration_proof_generator_drift"]


def test_generic_pack_receipt_reuse_is_rejected() -> None:
    """One generic migration receipt cannot certify two different Packs."""

    receipt = "sha256:" + "a" * 64
    proof = {
        "pack-a": {
            "status": "release-verified",
            "migration_receipt_digest": receipt,
        },
        "pack-b": {
            "status": "release-verified",
            "migration_receipt_digest": receipt,
        },
    }

    findings = complete_gate._generic_release_receipt_findings(proof)

    assert [finding["rule"] for finding in findings] == ["generic_pack_migration_receipt_reused"]
    assert findings[0]["pack_ids"] == ["pack-a", "pack-b"]


def test_release_status_requires_pack_specific_source_target_and_mappings() -> None:
    """A release label fails closed without source, target, and semantic maps."""

    entry = {
        "status": "release-verified",
        "source": {
            "status": "missing",
            "pack_id": "pack-a",
            "digest": None,
        },
        "target": {
            "status": "artifact-integrity-verified",
            "pack_id": "pack-a",
            "digest": "sha256:" + "b" * 64,
        },
        "semantic_comparison": {
            "status": "unverified",
            "equivalent": None,
            "method": None,
            "operation_mappings": [],
        },
        "migration_receipt_digest": "sha256:" + "c" * 64,
    }

    errors = complete_gate._pack_release_proof_errors("pack-a", entry)

    assert "pack_specific_legacy_source_missing" in errors
    assert "pack_specific_semantic_comparison_unverified" in errors
    assert "pack_specific_operation_mapping_missing" in errors
    assert "pack_specific_migration_receipt_invalid" in errors


def test_pack_feasibility_audit_is_specific_and_separate_from_profile_receipt() -> None:
    """The audit promotes exact comparisons and enumerates unresolved categories."""

    proof = proof_generator.build_proof(observed_head_sha="a" * 40)
    profile_receipt = proof["source"]["profile_collection_proof"]["transaction"]["receipt_digest"]
    serialized_packs = json.dumps(proof["packs"], sort_keys=True)

    assert profile_receipt not in serialized_packs
    statuses = {
        status: sum(entry["status"] == status for entry in proof["packs"].values())
        for status in ("semantically-reviewed", "generated-draft")
    }
    assert statuses == {"semantically-reviewed": 41, "generated-draft": 99}
    assert proof["source"]["unproved_pack_count"] == len(proof["packs"])
    assert proof["source"]["semantic_unproved_pack_count"] == 99
    unresolved = proof["source"]["feasibility_audit"]["unresolved"]
    assert unresolved["non_executable_pack_semantics_unmodeled"]["count"] == 50
    assert unresolved["pack_specific_legacy_source_missing"]["count"] == 41
    assert unresolved["pack_specific_authority_source_missing"]["count"] == 4
    assert unresolved["legacy_artifact_role_missing"]["count"] == 40
    assert unresolved["parameter_schema_mapping_mismatch"]["count"] == 1
    assert all(
        entry["target"]["pack_id"] == pack_id and entry["source"]["pack_id"] == pack_id
        for pack_id, entry in proof["packs"].items()
    )


def test_model_catalog_pack_compiles_as_a_host_extension() -> None:
    """The host-brokered model catalog must never be admitted to PackVM."""

    pack_root = proof_generator.ECOSYSTEM / "rumi_model_catalog_pack"
    compiled = compile_pack_root(pack_root)

    assert compiled.artifact.package_kind is PackageKind.HOST_EXTENSION
    assert compiled.artifact.variants
    assert {
        variant.execution_kind for variant in compiled.artifact.variants
    } == {ExecutionKind.HOST_EXTENSION}
    assert {
        route["execution_kind"] for route in compiled.routes.values()
    } == {ExecutionKind.HOST_EXTENSION.value}
