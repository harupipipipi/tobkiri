"""Regression tests for independently authored migration release evidence."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.quality.migration_release_evidence import (
    MigrationReleaseEvidenceError,
    canonical_digest,
    load_curated_reviews,
    load_runtime_receipts,
    release_evidence_errors,
)
from tests import test_complete_v4_migration_gate as complete_gate


def _with_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result[field] = canonical_digest(result)
    return result


def _admission_only_review(pack_id: str, target_digest: str) -> dict[str, Any]:
    semantic = _with_digest(
        {
            "kind": "admission-only",
            "status": "verified",
            "equivalent": True,
            "method": "curated-admission-only-review.v1",
            "operation_inventory": {"legacy_count": 0, "v4_count": 0},
            "operation_mappings": [],
            "no_operations_reason": "This Pack declares data only and has no executable operation.",
            "behavior_review": {"status": "verified", "scope": "declared-data"},
            "authority_review": {"status": "verified", "scope": "admission-only"},
        },
        "semantic_record_digest",
    )
    return _with_digest(
        {
            "pack_id": pack_id,
            "source_kind": "independent-curated",
            "reviewer_id": "reviewer:test",
            "reviewed_at": "2026-09-16T00:00:00Z",
            "source_digest": None,
            "target_digest": target_digest,
            "semantic_record": semantic,
            "semantic_record_digest": semantic["semantic_record_digest"],
            "review_basis": {
                "method": "independent-file-by-file-review.v1",
                "evidence_paths": ["a", "b", "c", "d", "e"],
                "verified_claims": ["zero-operation semantics"],
                "excluded_claims": ["release readiness"],
            },
        },
        "review_attestation_digest",
    )


def _zero_operation_review(
    pack_id: str,
    source_digest: str,
    target_digest: str,
) -> dict[str, Any]:
    semantic = _with_digest(
        {
            "kind": "zero-operation",
            "status": "verified",
            "equivalent": True,
            "method": "curated-zero-operation-review.v1",
            "operation_inventory": {"legacy_count": 0, "v4_count": 0},
            "operation_mappings": [],
            "no_operations_reason": (
                "The committed legacy manifest declares zero entrypoints and "
                "the v4 catalog declares zero executable operations; the Pack "
                "is surface/content-only."
            ),
            "behavior_review": {"status": "verified", "scope": "declared-content-surface"},
            "authority_review": {"status": "verified", "scope": "zero-operation-authority"},
        },
        "semantic_record_digest",
    )
    return _with_digest(
        {
            "pack_id": pack_id,
            "source_kind": "independent-curated",
            "reviewer_id": "reviewer:test",
            "reviewed_at": "2026-09-16T00:00:00Z",
            "source_digest": source_digest,
            "target_digest": target_digest,
            "semantic_record": semantic,
            "semantic_record_digest": semantic["semantic_record_digest"],
            "review_basis": {
                "method": "independent-file-by-file-review.v1",
                "evidence_paths": ["a", "b", "c", "d", "e"],
                "verified_claims": ["zero-operation semantics"],
                "excluded_claims": ["release readiness"],
            },
        },
        "review_attestation_digest",
    )


def _operation_mapping_review(
    pack_id: str,
    source_digest: str,
    target_digest: str,
    v4_count: int = 2,
) -> dict[str, Any]:
    semantic = _with_digest(
        {
            "kind": "operation-mapping",
            "status": "verified",
            "equivalent": True,
            "method": "curated-operation-mapping-review.v1",
            "operation_inventory": {
                "legacy_count": v4_count,
                "v4_count": v4_count,
            },
            "operation_mappings": [
                {
                    "legacy_operation_id": f"legacy-op-{index}",
                    "function_id": "function:test",
                    "v4_contract_id": "contract:test",
                    "v4_operation_id": f"v4-op-{index}",
                }
                for index in range(v4_count)
            ],
        },
        "semantic_record_digest",
    )
    return _with_digest(
        {
            "pack_id": pack_id,
            "source_kind": "independent-curated",
            "reviewer_id": "reviewer:test",
            "reviewed_at": "2026-09-16T00:00:00Z",
            "source_digest": source_digest,
            "target_digest": target_digest,
            "semantic_record": semantic,
            "semantic_record_digest": semantic["semantic_record_digest"],
            "review_basis": {
                "method": "independent-file-by-file-review.v1",
                "evidence_paths": ["a", "b", "c", "d", "e"],
                "verified_claims": ["operation mapping reviewed"],
                "excluded_claims": ["release readiness"],
            },
        },
        "review_attestation_digest",
    )


def _digest_only_review(
    pack_id: str,
    source_digest: str,
    target_digest: str,
) -> dict[str, Any]:
    return _with_digest(
        {
            "pack_id": pack_id,
            "source_kind": "independent-curated",
            "reviewer_id": "reviewer:test",
            "reviewed_at": "2026-09-16T00:00:00Z",
            "source_digest": source_digest,
            "target_digest": target_digest,
            "semantic_record_digest": canonical_digest({"generated": True}),
            "review_basis": {
                "method": "independent-file-by-file-review.v1",
                "evidence_paths": ["a", "b", "c", "d", "e"],
                "verified_claims": ["generated semantics accepted"],
                "excluded_claims": ["release readiness"],
            },
        },
        "review_attestation_digest",
    )


def _rechain_receipt(record: dict[str, Any]) -> dict[str, Any]:
    """Recompute the admission→install→conformance→release digest chain."""

    record = copy.deepcopy(record)
    admission = record["admission"]
    install = record["install"]
    isolated = record["isolated-conformance"]
    admission["receipt_digest"] = canonical_digest(
        {k: v for k, v in admission.items() if k != "receipt_digest"}
    )
    install["admission_receipt_digest"] = admission["receipt_digest"]
    install["receipt_digest"] = canonical_digest(
        {k: v for k, v in install.items() if k != "receipt_digest"}
    )
    isolated["install_receipt_digest"] = install["receipt_digest"]
    isolated["receipt_digest"] = canonical_digest(
        {k: v for k, v in isolated.items() if k != "receipt_digest"}
    )
    record["release_receipt_digest"] = canonical_digest(
        {k: v for k, v in record.items() if k != "release_receipt_digest"}
    )
    return record


def _runtime_receipt(
    pack_id: str,
    target_digest: str,
    review: dict[str, Any],
    *,
    operation_count: int = 0,
    isolated_status: str = "not-applicable",
    with_executions: bool = False,
) -> dict[str, Any]:
    admission = _with_digest(
        {
            "kind": "admission",
            "status": "verified",
            "pack_id": pack_id,
            "target_digest": target_digest,
            "host_instance_id": "host:test",
            "observed_at": "2026-09-16T00:01:00Z",
            "admission_id": "admission:test",
            "policy_digest": "sha256:" + "1" * 64,
            "reservation_journal_digest": "sha256:" + "2" * 64,
        },
        "receipt_digest",
    )
    install = _with_digest(
        {
            "kind": "install",
            "status": "verified",
            "pack_id": pack_id,
            "target_digest": target_digest,
            "host_instance_id": "host:test",
            "observed_at": "2026-09-16T00:02:00Z",
            "installation_id": "installation:test",
            "admission_receipt_digest": admission["receipt_digest"],
            "content_digest": "sha256:" + "3" * 64,
            "install_record_digest": "sha256:" + "4" * 64,
            "catalog_revision": "sha256:" + "5" * 64,
            "approval_record_digest": "sha256:" + "6" * 64,
        },
        "receipt_digest",
    )
    isolated = {
        "kind": "isolated-conformance",
        "status": isolated_status,
        "pack_id": pack_id,
        "target_digest": target_digest,
        "host_instance_id": "host:test",
        "observed_at": "2026-09-16T00:03:00Z",
        "install_receipt_digest": install["receipt_digest"],
        "probe_result_digest": "sha256:" + "7" * 64,
        "operation_count": operation_count,
    }
    if isolated_status == "verified":
        isolated["test_suite_digest"] = "sha256:" + "8" * 64
        isolated["result_digest"] = isolated["probe_result_digest"]
    else:
        isolated["reason"] = "Admission-only Pack has no executable operation."
    if with_executions:
        # Synthetic unit-test measurements, never published as Host receipts.
        isolated["operation_executions"] = [
            {
                "method": "host-operation-invocation.v1",
                "pack_id": pack_id,
                "target_digest": target_digest,
                "host_instance_id": isolated["host_instance_id"],
                "function_id": "function:test",
                "contract_id": "contract:test",
                "operation_id": f"v4-op-{index}",
                "invocation_id": f"invocation:test:{index}",
                "observed_at": "2026-09-16T00:02:30Z",
                "outcome": "passed",
                "input_digest": canonical_digest({"test-input": index}),
                "output_digest": canonical_digest({"test-output": index}),
            }
            for index in range(operation_count)
        ]
    isolated = _with_digest(isolated, "receipt_digest")
    return _with_digest(
        {
            "pack_id": pack_id,
            "target_digest": target_digest,
            "semantic_record_digest": review["semantic_record_digest"],
            "review_attestation_digest": review["review_attestation_digest"],
            "admission": admission,
            "install": install,
            "isolated-conformance": isolated,
        },
        "release_receipt_digest",
    )


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_generator_status_edit_cannot_claim_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rehashing a generated status edit cannot turn it into attestation."""

    payload = complete_gate._load_json(complete_gate.MIGRATION_PROOF_PATH)
    pack_id = next(
        key
        for key, value in payload["packs"].items()
        if value["status"] == "semantically-reviewed"
    )
    payload["packs"][pack_id]["status"] = "release-verified"
    unsigned = copy.deepcopy(payload)
    unsigned["source"].pop("observed_head_sha")
    unsigned["source"].pop("content_digest")
    payload["source"]["content_digest"] = complete_gate._proof_digest(unsigned)
    path = tmp_path / "pack_migration_proof.v1.json"
    _write(path, payload)
    monkeypatch.setattr(complete_gate, "MIGRATION_PROOF_PATH", path)

    proof, findings = complete_gate._load_independent_migration_proof()

    assert not proof
    assert [finding["rule"] for finding in findings] == [
        "independent_migration_proof_invalid"
    ]


def test_curated_review_rejects_generator_as_reviewer(tmp_path: Path) -> None:
    """The deterministic proof generator cannot attest its own output."""

    review = _admission_only_review("pack-a", "sha256:" + "a" * 64)
    review["reviewer_id"] = "tobkiri.quality.migration-proof-generator"
    review.pop("review_attestation_digest")
    review["review_attestation_digest"] = canonical_digest(review)
    path = tmp_path / "reviews.json"
    _write(
        path,
        {
            "schema": "io.tobkiri.quality.pack-migration-reviews.v1",
            "authority": "curated-review",
            "generator_id": None,
            "reviews": {"pack-a": review},
        },
    )

    with pytest.raises(MigrationReleaseEvidenceError, match="curated review is invalid"):
        load_curated_reviews(path)


def test_admission_only_review_and_exact_receipts_are_valid(tmp_path: Path) -> None:
    """A zero-operation Pack has a truthful path without invented mappings."""

    target_digest = "sha256:" + "b" * 64
    review = _admission_only_review("pack-a", target_digest)
    runtime = _runtime_receipt("pack-a", target_digest, review)
    review_path = tmp_path / "reviews.json"
    runtime_path = tmp_path / "runtime.json"
    _write(
        review_path,
        {
            "schema": "io.tobkiri.quality.pack-migration-reviews.v1",
            "authority": "curated-review",
            "generator_id": None,
            "reviews": {"pack-a": review},
        },
    )
    _write(
        runtime_path,
        {
            "schema": "io.tobkiri.quality.pack-migration-runtime-receipts.v1",
            "authority": "host-runtime-receipts",
            "generator_id": None,
            "receipts": {"pack-a": runtime},
        },
    )

    assert load_curated_reviews(review_path) == {"pack-a": review}
    assert load_runtime_receipts(runtime_path) == {"pack-a": runtime}
    entry = {
        "status": "release-verified",
        "source": {"status": "missing", "pack_id": "pack-a", "digest": None},
        "target": {"pack_id": "pack-a", "digest": target_digest},
        "semantic_comparison": {"status": "unverified"},
    }
    assert not complete_gate._pack_release_proof_errors(
        "pack-a",
        entry,
        curated_reviews={"pack-a": review},
        runtime_receipts={"pack-a": runtime},
    )


def test_zero_operation_review_requires_real_source_and_zero_inventory(
    tmp_path: Path,
) -> None:
    """A sourced zero-operation Pack still binds its exact legacy digest."""

    source_digest = "sha256:" + "a" * 64
    target_digest = "sha256:" + "b" * 64
    review = _zero_operation_review("pack-a", source_digest, target_digest)
    review_path = tmp_path / "reviews.json"
    _write(
        review_path,
        {
            "schema": "io.tobkiri.quality.pack-migration-reviews.v1",
            "authority": "curated-review",
            "generator_id": None,
            "reviews": {"pack-a": review},
        },
    )
    assert load_curated_reviews(review_path) == {"pack-a": review}

    dropped_source = copy.deepcopy(review)
    dropped_source.pop("source_digest")
    dropped_source["review_attestation_digest"] = canonical_digest(
        {k: v for k, v in dropped_source.items() if k != "review_attestation_digest"}
    )
    _write(
        review_path,
        {
            "schema": "io.tobkiri.quality.pack-migration-reviews.v1",
            "authority": "curated-review",
            "generator_id": None,
            "reviews": {"pack-a": dropped_source},
        },
    )
    with pytest.raises(MigrationReleaseEvidenceError, match="source digest is invalid"):
        load_curated_reviews(review_path)

    nonzero = copy.deepcopy(review)
    nonzero["semantic_record"]["operation_inventory"]["legacy_count"] = 1
    nonzero["semantic_record"]["operation_inventory"]["v4_count"] = 1
    nonzero["semantic_record"]["semantic_record_digest"] = canonical_digest(
        {k: v for k, v in nonzero["semantic_record"].items() if k != "semantic_record_digest"}
    )
    nonzero["semantic_record_digest"] = nonzero["semantic_record"]["semantic_record_digest"]
    nonzero["review_attestation_digest"] = canonical_digest(
        {k: v for k, v in nonzero.items() if k != "review_attestation_digest"}
    )
    _write(
        review_path,
        {
            "schema": "io.tobkiri.quality.pack-migration-reviews.v1",
            "authority": "curated-review",
            "generator_id": None,
            "reviews": {"pack-a": nonzero},
        },
    )
    with pytest.raises(MigrationReleaseEvidenceError, match="must have zero operations"):
        load_curated_reviews(review_path)


def test_runtime_receipt_rejects_target_digest_substitution(tmp_path: Path) -> None:
    """A receipt copied to another artifact fails even if its outer JSON is rehashed."""

    target_digest = "sha256:" + "c" * 64
    review = _admission_only_review("pack-a", target_digest)
    runtime = _runtime_receipt("pack-a", target_digest, review)
    runtime["target_digest"] = "sha256:" + "d" * 64
    runtime.pop("release_receipt_digest")
    runtime["release_receipt_digest"] = canonical_digest(runtime)
    path = tmp_path / "runtime.json"
    _write(
        path,
        {
            "schema": "io.tobkiri.quality.pack-migration-runtime-receipts.v1",
            "authority": "host-runtime-receipts",
            "generator_id": None,
            "receipts": {"pack-a": runtime},
        },
    )

    with pytest.raises(MigrationReleaseEvidenceError, match="runtime receipt is invalid"):
        load_runtime_receipts(path)


def test_checked_in_curated_reviews_bind_exact_generated_semantics() -> None:
    """Historical reviews promote only when their exact current bindings match."""

    reviews = load_curated_reviews(complete_gate.MIGRATION_REVIEW_PATH)
    proof, findings = complete_gate._load_independent_migration_proof()

    assert not findings
    assert set(reviews) == set(proof)
    for pack_id, review in reviews.items():
        entry = {**proof[pack_id], "status": "generated-draft"}
        effective = complete_gate._entry_with_curated_semantics(entry, review)
        bindings_match = (
            review["source_digest"] == entry["source"]["digest"]
            and review["target_digest"] == entry["target"]["digest"]
            and (
                review.get("semantic_record") is not None
                or review["semantic_record_digest"] == canonical_digest(entry["semantic_comparison"])
            )
        )
        if bindings_match:
            assert effective["status"] == "semantically-reviewed"
        else:
            assert effective == entry
        if review.get("semantic_record") is None:
            tampered_review = {
                **review,
                "semantic_record_digest": "sha256:" + "f" * 64,
            }
        else:
            tampered_review = {
                **review,
                "target_digest": "sha256:" + "f" * 64,
            }
        assert complete_gate._entry_with_curated_semantics(
            entry, tampered_review
        )["status"] == "generated-draft"


def test_corrected_shared_contract_owners_are_independently_reviewed() -> None:
    """Corrected ownership only counts once an independent review exists."""

    reviews = load_curated_reviews(complete_gate.MIGRATION_REVIEW_PATH)
    corrected = {
        "rumi_connector_turn_adapter_pack",
        "rumi_email_connector_pack",
        "rumi_generic_webhook_connector_pack",
    }

    assert corrected.issubset(reviews)
    for pack_id in corrected:
        contracts = complete_gate._load_json(
            complete_gate.ECOSYSTEM / pack_id / "contracts.v4.json"
        )["contracts"]
        assert all(contract["owner"] == pack_id for contract in contracts)


def _release_entry(pack_id: str, source_digest: str, target_digest: str) -> dict[str, Any]:
    return {
        "status": "release-verified",
        "source": {"status": "available", "pack_id": pack_id, "digest": source_digest},
        "target": {"pack_id": pack_id, "digest": target_digest},
        "semantic_comparison": {"status": "verified"},
    }


def _runtime_ledger(path: Path, pack_id: str, record: dict[str, Any]) -> Path:
    _write(
        path,
        {
            "schema": "io.tobkiri.quality.pack-migration-runtime-receipts.v1",
            "authority": "host-runtime-receipts",
            "generator_id": None,
            "receipts": {pack_id: record},
        },
    )
    return path


def test_verified_receipt_is_valid_with_full_execution_anchors(tmp_path: Path) -> None:
    """Release validation accepts a complete synthetic execution test fixture."""

    source_digest = "sha256:" + "a" * 64
    target_digest = "sha256:" + "b" * 64
    review = _operation_mapping_review(
        "pack-a", source_digest, target_digest, v4_count=2
    )
    runtime = _runtime_receipt(
        "pack-a",
        target_digest,
        review,
        operation_count=2,
        isolated_status="verified",
        with_executions=True,
    )
    path = _runtime_ledger(tmp_path / "runtime.json", "pack-a", runtime)

    assert load_runtime_receipts(path) == {"pack-a": runtime}
    assert not release_evidence_errors(
        "pack-a",
        _release_entry("pack-a", source_digest, target_digest),
        review,
        runtime,
    )


@pytest.mark.parametrize(
    ("step", "field"),
    [
        ("admission", "reservation_journal_digest"),
        ("install", "content_digest"),
        ("install", "install_record_digest"),
        ("install", "catalog_revision"),
        ("install", "approval_record_digest"),
        ("isolated-conformance", "probe_result_digest"),
        ("isolated-conformance", "operation_count"),
        ("isolated-conformance", "install_receipt_digest"),
    ],
)
def test_receipt_steps_require_real_execution_anchors(
    tmp_path: Path, step: str, field: str
) -> None:
    """A self-hashed JSON ledger cannot omit the Host artifacts it claims."""

    target_digest = "sha256:" + "c" * 64
    review = _admission_only_review("pack-a", target_digest)
    runtime = _runtime_receipt("pack-a", target_digest, review)
    runtime[step].pop(field)
    if step == "isolated-conformance" and field == "install_receipt_digest":
        # ``_rechain_receipt`` repairs the install binding itself; drop the
        # anchor after the chain so the missing field is really under test.
        isolated = runtime["isolated-conformance"]
        isolated["receipt_digest"] = canonical_digest(
            {k: v for k, v in isolated.items() if k != "receipt_digest"}
        )
        runtime["release_receipt_digest"] = canonical_digest(
            {k: v for k, v in runtime.items() if k != "release_receipt_digest"}
        )
    else:
        runtime = _rechain_receipt(runtime)
    path = _runtime_ledger(tmp_path / "runtime.json", "pack-a", runtime)

    with pytest.raises(MigrationReleaseEvidenceError):
        load_runtime_receipts(path)


def test_not_applicable_conformance_cannot_report_operations(tmp_path: Path) -> None:
    """A waived conformance probe must have verified an empty inventory."""

    target_digest = "sha256:" + "d" * 64
    review = _admission_only_review("pack-a", target_digest)
    runtime = _runtime_receipt("pack-a", target_digest, review)
    runtime["isolated-conformance"]["operation_count"] = 3
    runtime = _rechain_receipt(runtime)
    path = _runtime_ledger(tmp_path / "runtime.json", "pack-a", runtime)

    with pytest.raises(
        MigrationReleaseEvidenceError,
        match="cannot report exercised operations",
    ):
        load_runtime_receipts(path)


def test_receipt_steps_must_be_one_ordered_execution(tmp_path: Path) -> None:
    """Steps timestamped out of order were not a single executed chain."""

    target_digest = "sha256:" + "e" * 64
    review = _admission_only_review("pack-a", target_digest)
    runtime = _runtime_receipt("pack-a", target_digest, review)
    runtime["install"]["observed_at"] = "2026-09-15T00:00:00Z"
    runtime = _rechain_receipt(runtime)
    path = _runtime_ledger(tmp_path / "runtime.json", "pack-a", runtime)

    with pytest.raises(
        MigrationReleaseEvidenceError,
        match="not one ordered execution",
    ):
        load_runtime_receipts(path)


def test_release_evidence_binds_executed_operations_to_review() -> None:
    """The executed operation inventory must equal the reviewed v4 count."""

    source_digest = "sha256:" + "a" * 64
    target_digest = "sha256:" + "b" * 64
    review = _operation_mapping_review(
        "pack-a", source_digest, target_digest, v4_count=2
    )
    entry = _release_entry("pack-a", source_digest, target_digest)

    matching = _runtime_receipt(
        "pack-a",
        target_digest,
        review,
        operation_count=2,
        isolated_status="verified",
        with_executions=True,
    )
    assert not release_evidence_errors("pack-a", entry, review, matching)

    inflated = _runtime_receipt(
        "pack-a",
        target_digest,
        review,
        operation_count=3,
        isolated_status="verified",
    )
    assert "runtime_receipt_operation_count_mismatch" in release_evidence_errors(
        "pack-a", entry, review, inflated
    )

    shrunk = _runtime_receipt(
        "pack-a",
        target_digest,
        review,
        operation_count=1,
        isolated_status="verified",
    )
    assert "runtime_receipt_operation_count_mismatch" in release_evidence_errors(
        "pack-a", entry, review, shrunk
    )


def test_verified_conformance_must_exercise_a_nonempty_operation_set() -> None:
    """A digest-only review cannot be paired with a zero-operation run."""

    source_digest = "sha256:" + "a" * 64
    target_digest = "sha256:" + "b" * 64
    review = _digest_only_review("pack-a", source_digest, target_digest)
    entry = _release_entry("pack-a", source_digest, target_digest)

    empty_run = _runtime_receipt(
        "pack-a",
        target_digest,
        review,
        operation_count=0,
        isolated_status="verified",
    )
    assert "runtime_receipt_operations_not_exercised" in release_evidence_errors(
        "pack-a", entry, review, empty_run
    )

    real_run = _runtime_receipt(
        "pack-a",
        target_digest,
        review,
        operation_count=1,
        isolated_status="verified",
    )
    assert "runtime_receipt_operation_execution_missing" in release_evidence_errors(
        "pack-a", entry, review, real_run
    )


def test_admission_only_review_rejects_verified_conformance_claim() -> None:
    """A Pack reviewed as having no operations cannot claim a verified run."""

    source_digest = "sha256:" + "a" * 64
    target_digest = "sha256:" + "b" * 64
    review = _admission_only_review("pack-a", target_digest)
    entry = _release_entry("pack-a", source_digest, target_digest)
    runtime = _runtime_receipt(
        "pack-a",
        target_digest,
        review,
        operation_count=0,
        isolated_status="verified",
    )

    errors = release_evidence_errors("pack-a", entry, review, runtime)

    assert "isolated_conformance_verified_for_admission_only" in errors


def test_inventory_only_receipt_remains_readable_but_ineligible(tmp_path: Path) -> None:
    """Backward compatibility does not preserve an unsupported release claim."""

    source = "sha256:" + "a" * 64
    target = "sha256:" + "b" * 64
    review = _operation_mapping_review("pack-a", source, target)
    runtime = _runtime_receipt(
        "pack-a", target, review, operation_count=2, isolated_status="verified"
    )
    path = _runtime_ledger(tmp_path / "old.json", "pack-a", runtime)
    assert load_runtime_receipts(path) == {"pack-a": runtime}
    assert release_evidence_errors(
        "pack-a", _release_entry("pack-a", source, target), review, runtime
    ) == ["runtime_receipt_operation_execution_missing"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", "runtime_receipt_operation_execution_missing"),
        ("empty", "runtime_receipt_operation_execution_missing"),
        ("duplicate", "runtime_receipt_operation_execution_duplicate"),
        ("reused-invocation", "runtime_receipt_operation_execution_duplicate"),
        ("failed", "runtime_receipt_operation_execution_failed"),
        ("omitted", "runtime_receipt_operation_execution_set_mismatch"),
        ("foreign-operation", "runtime_receipt_operation_execution_set_mismatch"),
        ("foreign-contract", "runtime_receipt_operation_execution_set_mismatch"),
        ("foreign-function", "runtime_receipt_operation_execution_set_mismatch"),
        ("foreign-pack", "runtime_receipt_operation_execution_identity_mismatch"),
        ("foreign-target", "runtime_receipt_operation_execution_identity_mismatch"),
        ("foreign-host", "runtime_receipt_operation_execution_identity_mismatch"),
        ("later-observation", "runtime_receipt_operation_execution_observation_mismatch"),
        ("stale-observation", "runtime_receipt_operation_execution_observation_mismatch"),
        ("invalid-observation", "runtime_receipt_operation_execution_observation_mismatch"),
        ("naive-observation", "runtime_receipt_operation_execution_observation_mismatch"),
        ("underflow-observation", "runtime_receipt_operation_execution_observation_mismatch"),
        ("overflow-observation", "runtime_receipt_operation_execution_observation_mismatch"),
        ("missing-input", "runtime_receipt_operation_execution_invalid"),
        ("missing-output", "runtime_receipt_operation_execution_invalid"),
        ("missing-invocation", "runtime_receipt_operation_execution_invalid"),
        ("inventory-method", "runtime_receipt_operation_execution_invalid"),
        ("malformed", "runtime_receipt_operation_execution_invalid"),
    ],
)
def test_release_rejects_unmeasured_or_substituted_operation_execution(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    """Rehashing incomplete or foreign records does not establish execution."""

    source = "sha256:" + "a" * 64
    target = "sha256:" + "b" * 64
    review = _operation_mapping_review("pack-a", source, target)
    runtime = _runtime_receipt(
        "pack-a",
        target,
        review,
        operation_count=2,
        isolated_status="verified",
        with_executions=True,
    )
    isolated = runtime["isolated-conformance"]
    executions = isolated["operation_executions"]
    if mutation == "missing":
        isolated.pop("operation_executions")
    elif mutation == "empty":
        isolated["operation_executions"] = []
    elif mutation == "duplicate":
        executions[1] = copy.deepcopy(executions[0])
    elif mutation == "reused-invocation":
        executions[1]["invocation_id"] = executions[0]["invocation_id"]
    elif mutation == "failed":
        executions[1]["outcome"] = "failed"
    elif mutation == "omitted":
        executions.pop()
    elif mutation.startswith("foreign-"):
        field = {
            "operation": "operation_id",
            "contract": "contract_id",
            "function": "function_id",
            "pack": "pack_id",
            "target": "target_digest",
            "host": "host_instance_id",
        }[mutation.removeprefix("foreign-")]
        executions[1][field] = (
            "sha256:" + "f" * 64 if field == "target_digest" else "foreign"
        )
    elif mutation.endswith("-observation"):
        executions[1]["observed_at"] = {
            "later-observation": "2026-09-17T00:00:00Z",
            "stale-observation": "2026-09-15T00:00:00Z",
            "invalid-observation": "not-a-timestamp",
            "naive-observation": "2026-09-16T00:02:30",
            "underflow-observation": "0001-01-01T00:00:00+01:00",
            "overflow-observation": "9999-12-31T23:59:59-01:00",
        }[mutation]
    elif mutation.startswith("missing-"):
        executions[1].pop(
            mutation.removeprefix("missing-") + "_digest"
            if mutation != "missing-invocation"
            else "invocation_id"
        )
    elif mutation == "inventory-method":
        executions[1]["method"] = "operation-inventory"
    elif mutation == "malformed":
        executions[1] = None
    runtime = _rechain_receipt(runtime)
    # Historical/diagnostic records remain structurally loadable; eligibility
    # performs the stricter measured-execution checks.
    path = _runtime_ledger(tmp_path / "runtime.json", "pack-a", runtime)
    assert load_runtime_receipts(path) == {"pack-a": runtime}
    assert expected in release_evidence_errors(
        "pack-a", _release_entry("pack-a", source, target), review, runtime
    )


def test_digest_only_review_resolves_only_exact_bound_operation_identities() -> None:
    """Generated mappings are usable only through the review's exact digest."""

    source = "sha256:" + "a" * 64
    target = "sha256:" + "b" * 64
    review = _operation_mapping_review("pack-a", source, target)
    semantic = review.pop("semantic_record")
    semantic.pop("semantic_record_digest")
    review["semantic_record_digest"] = canonical_digest(semantic)
    review = _with_digest(
        {k: v for k, v in review.items() if k != "review_attestation_digest"},
        "review_attestation_digest",
    )
    runtime = _runtime_receipt(
        "pack-a",
        target,
        review,
        operation_count=2,
        isolated_status="verified",
        with_executions=True,
    )
    entry = _release_entry("pack-a", source, target)
    entry["semantic_comparison"] = semantic
    assert not release_evidence_errors("pack-a", entry, review, runtime)
    entry["semantic_comparison"] = copy.deepcopy(semantic)
    entry["semantic_comparison"]["operation_mappings"][0]["v4_operation_id"] = "foreign"
    assert "runtime_receipt_reviewed_operation_identities_missing" in (
        release_evidence_errors("pack-a", entry, review, runtime)
    )


def test_all_historical_receipts_load_but_inventory_only_claims_cannot_release() -> (
    None
):
    """Preserve all 143 records while withdrawing the 95 execution claims."""

    reviews = load_curated_reviews(complete_gate.MIGRATION_REVIEW_PATH)
    receipts = load_runtime_receipts(complete_gate.MIGRATION_RUNTIME_RECEIPT_PATH)
    proof, findings = complete_gate._load_independent_migration_proof()
    assert not findings
    assert len(receipts) == 143
    rejected = []
    for pack_id, runtime in receipts.items():
        errors = release_evidence_errors(
            pack_id, proof[pack_id], reviews[pack_id], runtime
        )
        expected_errors = set()
        if runtime["isolated-conformance"]["operation_count"]:
            expected_errors.add("runtime_receipt_operation_execution_missing")
            rejected.append(pack_id)
        # Historical inventories may also be stale after a legitimate source
        # repair. Require precisely the recorded binding failures, not arbitrary
        # extra errors that could hide a broken eligible zero-operation path.
        for field in ("source", "target"):
            if reviews[pack_id][f"{field}_digest"] != proof[pack_id][field]["digest"]:
                expected_errors.add(f"curated_review_{field}_digest_mismatch")
        if runtime["target_digest"] != proof[pack_id]["target"]["digest"]:
            expected_errors.add("runtime_receipt_target_digest_mismatch")
        for field in ("semantic_record_digest", "review_attestation_digest"):
            if runtime[field] != reviews[pack_id][field]:
                expected_errors.add(f"runtime_receipt_{field}_mismatch")
        assert set(errors) == expected_errors
    assert len(rejected) == 95


@pytest.mark.parametrize("kind", ["admission-only", "zero-operation"])
def test_empty_reviews_reject_phantom_execution_records(kind: str) -> None:
    """The empty/admission-only paths cannot conceal claimed executions."""

    target = "sha256:" + "b" * 64
    source = "sha256:" + "a" * 64
    if kind == "admission-only":
        review = _admission_only_review("pack-a", target)
        source = None
        status = "not-applicable"
    else:
        review = _zero_operation_review("pack-a", source, target)
        status = "verified"
    runtime = _runtime_receipt("pack-a", target, review, isolated_status=status)
    runtime["isolated-conformance"]["operation_executions"] = [{"outcome": "passed"}]
    runtime = _rechain_receipt(runtime)
    entry = {"source": {"digest": source}, "target": {"digest": target}}
    assert (
        "runtime_receipt_operation_execution_set_mismatch"
        in release_evidence_errors("pack-a", entry, review, runtime)
    )


def test_execution_observation_compares_actual_timezone_instants() -> None:
    """Timezone offsets cannot falsely reject an in-range measured run."""

    source = "sha256:" + "a" * 64
    target = "sha256:" + "b" * 64
    review = _operation_mapping_review("pack-a", source, target)
    runtime = _runtime_receipt(
        "pack-a",
        target,
        review,
        operation_count=2,
        isolated_status="verified",
        with_executions=True,
    )
    for execution in runtime["isolated-conformance"]["operation_executions"]:
        execution["observed_at"] = "2026-09-16T01:02:30+01:00"
    runtime = _rechain_receipt(runtime)
    assert not release_evidence_errors(
        "pack-a", _release_entry("pack-a", source, target), review, runtime
    )
