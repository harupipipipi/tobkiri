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


def _runtime_receipt(
    pack_id: str,
    target_digest: str,
    review: dict[str, Any],
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
        },
        "receipt_digest",
    )
    isolated = _with_digest(
        {
            "kind": "isolated-conformance",
            "status": "not-applicable",
            "pack_id": pack_id,
            "target_digest": target_digest,
            "host_instance_id": "host:test",
            "observed_at": "2026-09-16T00:03:00Z",
            "reason": "Admission-only Pack has no executable operation.",
        },
        "receipt_digest",
    )
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
    """All checked-in reviews bind exact source, target, and semantic digests."""

    reviews = load_curated_reviews(complete_gate.MIGRATION_REVIEW_PATH)
    proof, findings = complete_gate._load_independent_migration_proof()

    assert not findings
    assert len(reviews) == 78
    for pack_id, review in reviews.items():
        entry = {**proof[pack_id], "status": "generated-draft"}
        effective = complete_gate._entry_with_curated_semantics(entry, review)
        assert effective["status"] == "semantically-reviewed"
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
