from __future__ import annotations

import json
from pathlib import Path

import pytest

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.inventory import (
    _included_paths,
    generate_inventory,
    inventory_drift,
)
from tobkiri_protocol.scanners import scan_duplicate_ids, scan_v4_scope
from tobkiri_protocol.validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent
DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "identifier",
    ["worker", "shell.tauri.default", "defaults-basepack", "ui.route.v1"],
)
def test_canonical_identifier_round_trip_is_stable(identifier: str) -> None:
    principal = {
        "parent_artifact_digest": DIGEST,
        "function_implementation_digest": DIGEST,
        "function_id": identifier.replace(".v1", ""),
        "contract_revision_digest": DIGEST,
        "operation_id": "run",
    }
    principal["principal_digest"] = canonical_digest(
        {
            key: principal[key]
            for key in (
                "parent_artifact_digest",
                "function_implementation_digest",
                "function_id",
                "contract_revision_digest",
                "operation_id",
            )
        }
    )
    first = validate_document(principal, "function_principal")
    second = validate_document(json.loads(json.dumps(first)), "function_principal")
    assert first == second


def test_function_principal_digest_mismatch_fails_closed() -> None:
    principal = {
        "parent_artifact_digest": DIGEST,
        "function_implementation_digest": DIGEST,
        "function_id": "worker",
        "contract_revision_digest": DIGEST,
        "operation_id": "run",
        "principal_digest": "sha256:" + "b" * 64,
    }
    with pytest.raises(SchemaValidationError, match="digest does not match"):
        validate_document(principal, "function_principal")


def test_duplicate_pack_ids_are_reported_in_v4_scope(tmp_path: Path) -> None:
    packs = tmp_path / "tobkiri_runtime" / "packs_v4"
    packs.mkdir(parents=True)
    for name in ("one", "two"):
        (packs / f"{name}_pack_manifest.v4.json").write_text(
            json.dumps(
                {
                    "pack_api_version": "io.tobkiri.pack.v4",
                    "pack": {"id": "same-pack"},
                }
            ),
            encoding="utf-8",
        )
    findings = scan_duplicate_ids(tmp_path)
    assert any(finding.rule_id == "duplicate_semantic_id" for finding in findings)


def test_duplicate_local_function_ids_are_scoped_to_one_manifest(tmp_path: Path) -> None:
    packs = tmp_path / "tobkiri_runtime" / "packs_v4"
    packs.mkdir(parents=True)
    manifest = packs / "one_pack_manifest.v4.json"
    manifest.write_text(
        json.dumps(
            {
                "pack_api_version": "io.tobkiri.pack.v4",
                "functions": [{"id": "worker"}, {"id": "worker"}],
            }
        ),
        encoding="utf-8",
    )
    findings = scan_duplicate_ids(tmp_path)
    assert any(finding.rule_id == "duplicate_semantic_id" for finding in findings)


def test_same_local_function_id_in_separate_manifests_is_not_global_collision(
    tmp_path: Path,
) -> None:
    packs = tmp_path / "tobkiri_runtime" / "packs_v4"
    packs.mkdir(parents=True)
    for name in ("one", "two"):
        (packs / f"{name}_pack_manifest.v4.json").write_text(
            json.dumps(
                {
                    "pack_api_version": "io.tobkiri.pack.v4",
                    "functions": [{"id": "worker"}],
                }
            ),
            encoding="utf-8",
        )
    findings = scan_duplicate_ids(tmp_path)
    assert not any(finding.rule_id == "duplicate_semantic_id" for finding in findings)


def test_v4_legacy_marker_is_reported_as_a_hard_finding(tmp_path: Path) -> None:
    scope = tmp_path / "tobkiri_runtime" / "profiles_v4"
    scope.mkdir(parents=True)
    path = scope / "unsafe.profile.json"
    path.write_text('{"host_execution": true}\n', encoding="utf-8")
    findings = scan_v4_scope(tmp_path)
    assert any(finding.rule_id == "legacy_host_execution" for finding in findings)


def test_generated_inventory_is_schema_valid_and_not_drifting() -> None:
    inventory = generate_inventory(REPOSITORY_ROOT)
    assert inventory["schema"] == "io.tobkiri.architecture.inventory.v1"
    assert inventory["findings"]["v4"] == []
    assert inventory_drift(
        REPOSITORY_ROOT,
        REPOSITORY_ROOT
        / "tobkiri_runtime"
        / "generated"
        / "architecture"
        / "architecture_inventory.json",
    ) is False


def test_generated_inventory_excludes_python_cache_artifacts() -> None:
    """Python bytecode must not make the tracked inventory nondeterministic."""
    included = _included_paths(REPOSITORY_ROOT)

    assert all("__pycache__" not in path.parts for path in included)
    assert all(path.suffix != ".pyc" for path in included)


def _signed_distribution() -> dict[str, object]:
    payload: dict[str, object] = {
        "distribution_api_version": "io.tobkiri.distribution.v1",
        "distribution_id": "example.normal-packs",
        "version": "1.0.0",
        "packs": [{"pack_id": "example.pack", "artifact_digest": DIGEST}],
        "provenance": {
            "schema": "io.tobkiri.provenance.v1",
            "source_kind": "external",
            "source_path": "distribution.json",
            "source_digest": DIGEST,
            "repository_commit": "working-tree",
            "repository_tree": "a" * 64,
            "generator": "distribution-test",
            "generator_version": "1.0.0",
            "normative": True,
            "evidence": [],
        },
    }
    digest = canonical_digest(payload)
    payload["integrity"] = {
        "algorithm": "sha256-canonical-v1",
        "manifest_digest": digest,
    }
    payload["signature_envelope"] = {
        "algorithm": "ed25519",
        "publisher_id": "publisher.example",
        "key_id": "publisher.example.key-1",
        "signed_digest": digest,
        "signature": "A" * 86 + "==",
    }
    return payload


def test_distribution_requires_digest_bound_signature_envelope() -> None:
    document = _signed_distribution()
    assert validate_document(document, "distribution") == document
    unsigned = dict(document)
    unsigned.pop("signature_envelope")
    with pytest.raises(SchemaValidationError, match="required property"):
        validate_document(unsigned, "distribution")
    tampered = json.loads(json.dumps(document))
    tampered["packs"][0]["pack_id"] = "example.other"
    with pytest.raises(SchemaValidationError, match="digest does not match"):
        validate_document(tampered, "distribution")
