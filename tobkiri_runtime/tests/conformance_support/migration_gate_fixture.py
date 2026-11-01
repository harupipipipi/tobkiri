"""Synthetic-only migration fixture proposal; never publish as Host evidence.

All records are independently authored test data, written only below pytest tmp_path.
They must never be copied into production evidence or used as execution proof.
The snapshot context isolates unrelated scanner inputs; its migration evaluator
and review/runtime receipt validators remain real.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


def build_fixture(root: Path, gate: Any) -> SimpleNamespace:
    """Create one independently authored, synthetic operation Pack in tmp storage."""
    digest = gate._proof_digest

    def sealed(record: dict[str, Any], field: str) -> dict[str, Any]:
        result = deepcopy(record)
        result.pop(field, None)
        result[field] = digest(result)
        return result

    def write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")

    # This independently authored synthetic Application root also carries the
    # mandatory bundled Profile input; it is never copied into production.
    pack_id = "defaultspack"
    runtime_root = root / "tobkiri_runtime"
    ecosystem = runtime_root / "ecosystem"
    pack_dir = ecosystem / pack_id
    source_digest = digest({"synthetic-test-source": pack_id})
    target_digest = digest({"synthetic-test-target": pack_id})
    schema_digest = digest({"synthetic-test-schema": "payload"})
    mapping = {
        "legacy_operation_id": "legacy.execute",
        "function_id": f"{pack_id}.execute",
        "v4_contract_id": "synthetic.contract.v1",
        "v4_operation_id": f"{pack_id}.execute",
        "parameter_mapping": {
            "status": "verified",
            "method": "canonical-json-schema-equality",
            "legacy_schema_digest": schema_digest,
            "v4_schema_digest": schema_digest,
            "rules": [],
        },
        "authority_mapping": {"status": "verified", "legacy": {}, "v4": {}},
    }
    generated = {
        "status": "verified",
        "equivalent": True,
        "method": "legacy-to-v4-semantic-comparator.v1",
        "operation_inventory": {"legacy_count": 1, "v4_count": 1},
        "operation_mappings": [mapping],
    }
    entry = {
        "status": "semantically-reviewed",
        "source": {
            "pack_id": pack_id,
            "status": "available",
            "digest": source_digest,
            "files": [{"path": "fixtures/synthetic-source.json", "digest": source_digest}],
        },
        "target": {
            "pack_id": pack_id,
            "status": "artifact-integrity-verified",
            "digest": target_digest,
            "artifact_verification": {
                "pack_id": pack_id,
                "artifact_set_digest": target_digest,
            },
        },
        "semantic_comparison": generated,
    }
    entry["migration_receipt_digest"] = digest({
        "pack_id": pack_id,
        "source_digest": source_digest,
        "target_digest": target_digest,
        "semantic_comparison": generated,
    })
    semantic = deepcopy(generated)
    semantic.update(kind="operation-mapping", method="curated-operation-mapping-review.v1")
    semantic = sealed(semantic, "semantic_record_digest")
    review = sealed({
        "pack_id": pack_id,
        "source_kind": "independent-curated",
        "reviewer_id": "reviewer:synthetic-unit-test",
        "reviewed_at": "2026-09-16T00:00:00Z",
        "source_digest": source_digest,
        "target_digest": target_digest,
        "semantic_record": semantic,
        "semantic_record_digest": semantic["semantic_record_digest"],
        "review_basis": {
            "method": "independent-file-by-file-review.v1",
            "evidence_paths": [f"fixtures/synthetic-{index}.json" for index in range(5)],
            "verified_claims": ["synthetic unit-test operation mapping"],
            "excluded_claims": ["release readiness", "production Host execution"],
        },
    }, "review_attestation_digest")
    shared = {
        "pack_id": pack_id,
        "target_digest": target_digest,
        "host_instance_id": "host:synthetic-unit-test",
        "status": "verified",
    }
    admission = sealed({
        **shared,
        "kind": "admission",
        "observed_at": "2026-09-16T00:01:00Z",
        "admission_id": "admission:synthetic-unit-test",
        "policy_digest": digest({"synthetic": "policy"}),
        "reservation_journal_digest": digest({"synthetic": "journal"}),
    }, "receipt_digest")
    install = sealed({
        **shared,
        "kind": "install",
        "observed_at": "2026-09-16T00:02:00Z",
        "installation_id": "install:synthetic-unit-test",
        "admission_receipt_digest": admission["receipt_digest"],
        "content_digest": digest({"synthetic": "content"}),
        "install_record_digest": digest({"synthetic": "install-record"}),
        "catalog_revision": digest({"synthetic": "catalog"}),
        "approval_record_digest": digest({"synthetic": "approval"}),
    }, "receipt_digest")
    result_digest = digest({"synthetic-test-result": "passed"})
    isolated = sealed({
        **shared,
        "kind": "isolated-conformance",
        "observed_at": "2026-09-16T00:03:00Z",
        "install_receipt_digest": install["receipt_digest"],
        "probe_result_digest": result_digest,
        "result_digest": result_digest,
        "test_suite_digest": digest({"synthetic": "test-suite"}),
        "operation_count": 1,
        "operation_executions": [{
            "method": "host-operation-invocation.v1",
            "pack_id": pack_id,
            "target_digest": target_digest,
            "host_instance_id": shared["host_instance_id"],
            "function_id": mapping["function_id"],
            "contract_id": mapping["v4_contract_id"],
            "operation_id": mapping["v4_operation_id"],
            "invocation_id": "invocation:synthetic-unit-test:1",
            "observed_at": "2026-09-16T00:02:30Z",
            "outcome": "passed",
            "input_digest": digest({"synthetic-test-input": 1}),
            "output_digest": digest({"synthetic-test-output": 1}),
        }],
    }, "receipt_digest")
    receipt = sealed({
        "pack_id": pack_id,
        "target_digest": target_digest,
        "semantic_record_digest": review["semantic_record_digest"],
        "review_attestation_digest": review["review_attestation_digest"],
        "admission": admission,
        "install": install,
        "isolated-conformance": isolated,
    }, "release_receipt_digest")
    identity = sealed({
        "profile_ids": ["profile:synthetic-test"],
        "workspace_ids": ["workspace:synthetic-test"],
        "conversation_ids": ["conversation:synthetic-test"],
        "settings_ids": ["settings:synthetic-test"],
        "credential_ids": ["credential:synthetic-test"],
        "defaults_collapsed": False,
        "all_ids_distinct": True,
        "profile_names": {"profile:synthetic-test": "Synthetic fixture"},
    }, "digest")
    transaction = sealed({
        "algorithm": "profile-definition-store.import_legacy_collection.v1",
        "lossless": True,
        "restart_verified": True,
        "replay_rejected_without_mutation": True,
        "identity_proof_digest": identity["digest"],
        "source_digest": digest({"synthetic": "profile-input"}),
        "failure_injection": {
            name: {"raised": True, "committed_state": False}
            for name in ("symlink_preflight", "state_write")
        },
    }, "receipt_digest")
    inputs = [
        {"kind": "synthetic-test-input", "path": f"fixtures/{name}",
         "digest": digest({"synthetic": name})}
        for name in ("legacy_profile_bundle.v1.json", "legacy_executable_sources.v1.json")
    ]
    source = {
        "kind": "repository-generated-evidence",
        "generator_id": "test:synthetic-fixture",
        "authority": "evidence-only",
        "attestation": "none",
        "freshness_basis": "exact-input-digests-and-deterministic-recomputation",
        "pack_count": 1,
        "input_digest": digest(inputs),
        "inputs": inputs,
        "input_paths": [item["path"] for item in inputs],
        "profile_collection_proof": {"identity_proof": identity, "transaction": transaction},
    }
    document = {
        "schema": "io.tobkiri.quality.pack-migration-proof.v2",
        "source": source,
        "packs": {pack_id: entry},
    }
    content_digest = digest(document)
    source.update(observed_head_sha="a" * 40, content_digest=content_digest)
    proof_path = root / "fixtures" / "synthetic-proof.json"
    review_path = root / "fixtures" / "synthetic-reviews.json"
    receipt_path = root / "fixtures" / "synthetic-runtime-receipts.json"
    write(proof_path, document)
    write(review_path, {
        "schema": "io.tobkiri.quality.pack-migration-reviews.v1",
        "authority": "curated-review", "generator_id": None,
        "reviews": {pack_id: review},
    })
    write(receipt_path, {
        "schema": "io.tobkiri.quality.pack-migration-runtime-receipts.v1",
        "authority": "host-runtime-receipts", "generator_id": None,
        "receipts": {pack_id: receipt},
    })
    for name in gate.PACK_ARTIFACTS:
        write(pack_dir / name, {"pack": {"artifact_digest": target_digest}}
              if name == "pack.v4.json" else {"synthetic-test-only": True})
    write(pack_dir / "ecosystem.json", {"synthetic-test-only": True})
    write(runtime_root / "schemas" / "manifest_authority.v1.json",
          {"packs": {pack_id: "v4-authoritative"}})
    write(runtime_root / "schemas" / "pack_v4_catalog.v1.json", {
        "pack_ids": [pack_id], "excluded_packs": [],
        "packs": [{"pack_id": pack_id, "authority": "v4-authoritative"}],
    })
    write(runtime_root / "schemas" / "executable_sources.v1.json", {"packs": {}})
    write(pack_dir / "v4/defaults.profile.v5.json", {"packs": []})
    staged_records = []
    for source_id in (
        "tobkiri_surface_renderer_pack", "tobkiri_voice_agent_pack"
    ):
        source_dir = ecosystem / source_id
        source_dir.mkdir()
        source = b"Synthetic preserved Source; not an installed Pack or Host receipt.\n"
        (source_dir / "README.md").write_bytes(source)
        staged_records.append({
            "source_id": source_id,
            "source_root": f"ecosystem/{source_id}",
            "state": "staged_unadmitted",
            "runtime_authority": False,
            "installed_claim": False,
            "profile_selection": False,
            "files": [{
                "path": "README.md",
                "sha256": "sha256:" + hashlib.sha256(source).hexdigest(),
            }],
        })
    write(runtime_root / "schemas" / "staged_source_inventory.v1.json", {
        "schema": "io.tobkiri.staged-source-inventory.v1", "records": staged_records,
    })
    (runtime_root / "schemas/staged_source_inventory_v1.schema.json").write_bytes(
        (gate.RUNTIME / "schemas/staged_source_inventory_v1.schema.json").read_bytes()
    )
    return SimpleNamespace(
        root=root, runtime_root=runtime_root, ecosystem=ecosystem,
        pack_id=pack_id, pack_dir=pack_dir, entry=entry, proof={pack_id: entry},
        review=review, receipt=receipt, proof_path=proof_path,
        review_path=review_path, receipt_path=receipt_path,
    )


@contextmanager
def bind_fixture(gate: Any, fixture: SimpleNamespace, *, snapshot: bool = False) -> Iterator[None]:
    """Bind tmp evidence; isolate unrelated scanners only for aggregation tests."""
    with ExitStack() as stack:
        for name, value in {
            "MIGRATION_PROOF_PATH": fixture.proof_path,
            "MIGRATION_REVIEW_PATH": fixture.review_path,
            "MIGRATION_RUNTIME_RECEIPT_PATH": fixture.receipt_path,
        }.items():
            stack.enter_context(patch.object(gate, name, value))
        if snapshot:
            stack.enter_context(patch.object(gate, "RUNTIME", fixture.runtime_root))
            stack.enter_context(patch.object(gate, "ECOSYSTEM", fixture.ecosystem))
            original_relative = gate._relative

            def relative(path: Path) -> str:
                try:
                    return path.relative_to(fixture.root).as_posix()
                except ValueError:
                    return original_relative(path)

            stack.enter_context(patch.object(gate, "_relative", relative))
            # The real migration evaluator, loaders, status and inventory logic
            # remain active. Production source scanners are covered separately.
            for name in (
                "_v4_artifact_findings", "_declaration_disk_runtime_findings",
                "_executable_source_findings", "_authority_resolved_plan_findings",
                "_ast_legacy_runtime_findings", "_ast_authority_bypass_findings",
                "_ast_projection_findings", "_ast_fallback_findings",
                "_ast_old_composition_findings", "_double_authority_findings",
                "_launcher_safety_findings", "_offline_projection_findings",
                "_frontend_command_protocol_findings", "_migration_proof_generator_findings",
            ):
                stack.enter_context(patch.object(gate, name, list))
        yield
