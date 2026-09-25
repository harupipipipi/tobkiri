"""Validate curated Pack migration reviews and exact runtime receipts.

The deterministic migration proof generator is intentionally unable to author
either ledger consumed here.  A repository hash can demonstrate integrity, but
it cannot attest that a person reviewed semantics or that the Host admitted,
installed, and exercised an exact Pack artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


CURATED_REVIEW_SCHEMA = "io.tobkiri.quality.pack-migration-reviews.v1"
RUNTIME_RECEIPT_SCHEMA = "io.tobkiri.quality.pack-migration-runtime-receipts.v1"
PROOF_GENERATOR_ID = "tobkiri.quality.migration-proof-generator"
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class MigrationReleaseEvidenceError(ValueError):
    """Raised when curated or runtime migration evidence is malformed."""


def canonical_digest(value: Any) -> str:
    """Return the canonical SHA-256 digest used by the evidence ledgers."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _load_object(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationReleaseEvidenceError(f"cannot read evidence ledger: {path}") from exc
    if not isinstance(value, Mapping):
        raise MigrationReleaseEvidenceError("evidence ledger must be an object")
    return value


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and DIGEST_PATTERN.fullmatch(value) is not None


def _exact_digest(record: Mapping[str, Any], field: str) -> bool:
    digest = record.get(field)
    if not _is_digest(digest):
        return False
    unsigned = {key: value for key, value in record.items() if key != field}
    return digest == canonical_digest(unsigned)


def _valid_inventory(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("legacy_count"), int)
        and not isinstance(value.get("legacy_count"), bool)
        and isinstance(value.get("v4_count"), int)
        and not isinstance(value.get("v4_count"), bool)
        and value["legacy_count"] >= 0
        and value["v4_count"] >= 0
    )


def _validate_semantic_record(record: Any) -> None:
    if not isinstance(record, Mapping) or not _exact_digest(record, "semantic_record_digest"):
        raise MigrationReleaseEvidenceError("semantic record digest is invalid")
    inventory = record.get("operation_inventory")
    mappings = record.get("operation_mappings")
    if (
        record.get("status") != "verified"
        or record.get("equivalent") is not True
        or not _valid_inventory(inventory)
        or not isinstance(mappings, list)
        or inventory["legacy_count"] != inventory["v4_count"]
    ):
        raise MigrationReleaseEvidenceError("semantic record is incomplete")
    if record.get("kind") == "operation-mapping":
        if not mappings or len(mappings) != inventory["v4_count"]:
            raise MigrationReleaseEvidenceError("operation semantic mapping is incomplete")
        return
    if record.get("kind") not in {"admission-only", "zero-operation"}:
        raise MigrationReleaseEvidenceError("semantic record kind is invalid")
    expected_method = {
        "admission-only": "curated-admission-only-review.v1",
        "zero-operation": "curated-zero-operation-review.v1",
    }[record["kind"]]
    if (
        record.get("method") != expected_method
        or inventory != {"legacy_count": 0, "v4_count": 0}
        or mappings
    ):
        raise MigrationReleaseEvidenceError(
            f"{record['kind']} review must have zero operations"
        )
    if (
        not isinstance(record.get("no_operations_reason"), str)
        or not record["no_operations_reason"].strip()
        or not isinstance(record.get("behavior_review"), Mapping)
        or record["behavior_review"].get("status") != "verified"
        or not isinstance(record.get("authority_review"), Mapping)
        or record["authority_review"].get("status") != "verified"
    ):
        raise MigrationReleaseEvidenceError(
            f"{record['kind']} semantic review is incomplete"
        )


def load_curated_reviews(path: Path) -> dict[str, Mapping[str, Any]]:
    """Load reviews that cannot identify the deterministic generator as author."""

    payload = _load_object(path)
    reviews = payload.get("reviews")
    if (
        payload.get("schema") != CURATED_REVIEW_SCHEMA
        or payload.get("authority") != "curated-review"
        or payload.get("generator_id") is not None
        or not isinstance(reviews, Mapping)
    ):
        raise MigrationReleaseEvidenceError("curated review ledger header is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for pack_id, review in reviews.items():
        if not isinstance(pack_id, str) or not pack_id or not isinstance(review, Mapping):
            raise MigrationReleaseEvidenceError("curated review identity is invalid")
        semantic = review.get("semantic_record")
        if semantic is not None:
            _validate_semantic_record(semantic)
        review_basis = review.get("review_basis")
        if (
            review.get("pack_id") != pack_id
            or review.get("source_kind") != "independent-curated"
            or review.get("reviewer_id") in {None, "", PROOF_GENERATOR_ID}
            or not isinstance(review.get("reviewed_at"), str)
            or not review["reviewed_at"].strip()
            or not _is_digest(review.get("target_digest"))
            or not _is_digest(review.get("semantic_record_digest"))
            or (
                isinstance(semantic, Mapping)
                and review.get("semantic_record_digest")
                != semantic.get("semantic_record_digest")
            )
            or not isinstance(review_basis, Mapping)
            or review_basis.get("method") != "independent-file-by-file-review.v1"
            or not isinstance(review_basis.get("evidence_paths"), list)
            or len(review_basis["evidence_paths"]) < 5
            or not isinstance(review_basis.get("verified_claims"), list)
            or not review_basis["verified_claims"]
            or not isinstance(review_basis.get("excluded_claims"), list)
            or "release readiness" not in review_basis["excluded_claims"]
            or not _exact_digest(review, "review_attestation_digest")
        ):
            raise MigrationReleaseEvidenceError(f"curated review is invalid: {pack_id}")
        source_digest = review.get("source_digest")
        semantic_kind = semantic.get("kind") if isinstance(semantic, Mapping) else None
        if semantic_kind != "admission-only" and not _is_digest(source_digest):
            raise MigrationReleaseEvidenceError(f"review source digest is invalid: {pack_id}")
        if semantic_kind == "admission-only" and source_digest is not None:
            raise MigrationReleaseEvidenceError(
                f"admission-only review must not invent a legacy source: {pack_id}"
            )
        result[pack_id] = review
    return result


def _validate_runtime_step(
    pack_id: str,
    step_name: str,
    step: Any,
    *,
    target_digest: str,
) -> None:
    if (
        not isinstance(step, Mapping)
        or step.get("kind") != step_name
        or step.get("pack_id") != pack_id
        or step.get("target_digest") != target_digest
        or step.get("status") not in {"verified", "not-applicable"}
        or not isinstance(step.get("host_instance_id"), str)
        or not step["host_instance_id"].strip()
        or not isinstance(step.get("observed_at"), str)
        or not step["observed_at"].strip()
        or not _exact_digest(step, "receipt_digest")
    ):
        raise MigrationReleaseEvidenceError(
            f"{step_name} runtime receipt is invalid: {pack_id}"
        )
    if step["status"] == "not-applicable" and (
        step_name != "isolated-conformance"
        or not isinstance(step.get("reason"), str)
        or not step["reason"].strip()
    ):
        raise MigrationReleaseEvidenceError(
            f"runtime receipt cannot be not-applicable: {pack_id}:{step_name}"
        )
    if step_name == "admission" and (
        not isinstance(step.get("admission_id"), str)
        or not step["admission_id"].strip()
        or not _is_digest(step.get("policy_digest"))
    ):
        raise MigrationReleaseEvidenceError(f"admission receipt is incomplete: {pack_id}")
    if step_name == "install" and (
        not isinstance(step.get("installation_id"), str)
        or not step["installation_id"].strip()
        or not _is_digest(step.get("admission_receipt_digest"))
    ):
        raise MigrationReleaseEvidenceError(f"install receipt is incomplete: {pack_id}")
    if step_name == "isolated-conformance" and step["status"] == "verified" and (
        not _is_digest(step.get("test_suite_digest"))
        or not _is_digest(step.get("result_digest"))
        or not _is_digest(step.get("install_receipt_digest"))
    ):
        raise MigrationReleaseEvidenceError(
            f"isolated conformance receipt is incomplete: {pack_id}"
        )


def load_runtime_receipts(path: Path) -> dict[str, Mapping[str, Any]]:
    """Load Host/runtime receipts bound to exact reviewed artifact identities."""

    payload = _load_object(path)
    receipts = payload.get("receipts")
    if (
        payload.get("schema") != RUNTIME_RECEIPT_SCHEMA
        or payload.get("authority") != "host-runtime-receipts"
        or payload.get("generator_id") is not None
        or not isinstance(receipts, Mapping)
    ):
        raise MigrationReleaseEvidenceError("runtime receipt ledger header is invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for pack_id, record in receipts.items():
        if not isinstance(pack_id, str) or not pack_id or not isinstance(record, Mapping):
            raise MigrationReleaseEvidenceError("runtime receipt identity is invalid")
        target_digest = record.get("target_digest")
        if (
            record.get("pack_id") != pack_id
            or not _is_digest(target_digest)
            or not _is_digest(record.get("semantic_record_digest"))
            or not _is_digest(record.get("review_attestation_digest"))
        ):
            raise MigrationReleaseEvidenceError(f"runtime receipt binding is invalid: {pack_id}")
        for name in ("admission", "install", "isolated-conformance"):
            _validate_runtime_step(
                pack_id,
                name,
                record.get(name),
                target_digest=target_digest,
            )
        admission = record["admission"]
        install = record["install"]
        isolated = record["isolated-conformance"]
        if install.get("admission_receipt_digest") != admission.get("receipt_digest"):
            raise MigrationReleaseEvidenceError(
                f"install receipt does not bind admission: {pack_id}"
            )
        if (
            isolated.get("status") == "verified"
            and isolated.get("install_receipt_digest") != install.get("receipt_digest")
        ):
            raise MigrationReleaseEvidenceError(
                f"conformance receipt does not bind install: {pack_id}"
            )
        if not _exact_digest(record, "release_receipt_digest"):
            raise MigrationReleaseEvidenceError(f"release receipt digest is invalid: {pack_id}")
        result[pack_id] = record
    return result


def release_evidence_errors(
    pack_id: str,
    entry: Mapping[str, Any],
    review: Mapping[str, Any] | None,
    runtime: Mapping[str, Any] | None,
) -> list[str]:
    """Return exact-binding errors for one release claim."""

    if review is None:
        return ["curated_semantic_review_missing"]
    errors: list[str] = []
    source = entry.get("source")
    target = entry.get("target")
    source_digest = source.get("digest") if isinstance(source, Mapping) else None
    target_digest = target.get("digest") if isinstance(target, Mapping) else None
    if review.get("source_digest") != source_digest:
        errors.append("curated_review_source_digest_mismatch")
    if review.get("target_digest") != target_digest:
        errors.append("curated_review_target_digest_mismatch")
    if runtime is None:
        errors.append("runtime_release_receipts_missing")
        return errors
    bindings = {
        "target_digest": target_digest,
        "semantic_record_digest": review.get("semantic_record_digest"),
        "review_attestation_digest": review.get("review_attestation_digest"),
    }
    for field, expected in bindings.items():
        if runtime.get(field) != expected:
            errors.append(f"runtime_receipt_{field}_mismatch")
    isolated = runtime.get("isolated-conformance")
    semantic = review.get("semantic_record")
    if (
        isinstance(isolated, Mapping)
        and isolated.get("status") == "not-applicable"
        and (not isinstance(semantic, Mapping) or semantic.get("kind") != "admission-only")
    ):
        errors.append("isolated_conformance_not_verified")
    return errors
