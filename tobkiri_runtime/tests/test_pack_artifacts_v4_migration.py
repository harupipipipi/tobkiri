"""Production gates for the one-way Pack v4 artifact migration."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.migrate_pack_artifacts_v4 import (
    CATALOG,
    EXCLUDED_PACKS,
    PackV4MigrationError,
    _file_digest,
    _import_record,
    _migration_source_view,
    _render_record,
    _validate_catalog_payload,
    generate,
    verify_rendered_artifacts,
)
from tobkiri_protocol.errors import SchemaValidationError


def _catalog() -> dict[str, object]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_migration_source_view_excludes_generated_projection_envelopes(
    tmp_path: Path,
) -> None:
    """Authority metadata must not churn one-way source evidence hashes."""
    ecosystem = tmp_path / "ecosystem.json"
    ecosystem.write_text(
        json.dumps(
            {
                "dependencies": {"workspace": ">=1.0.0"},
                "metadata": {
                    "canonical_v4": {
                        "artifact_digest": "sha256:" + "a" * 64,
                        "source_identity": "sha256:" + "b" * 64,
                    },
                    "format": "rumi.ecosystem.v1",
                    "generated": True,
                    "generated_from": {"source_content_hash": "generated"},
                    "manifest_authority": "v3-authoritative",
                    "owner": "source",
                    "read_only_projection": True,
                },
                "provenance": {
                    "content_hash": "sha256:" + "c" * 64,
                },
                "runtime": {"implementation": "runtime.py"},
            }
        ),
        encoding="utf-8",
    )
    v3 = tmp_path / "rumi.pack.v3.json"
    v3.write_text(
        json.dumps(
            {
                "entrypoints": [
                    {
                        "module": "ecosystem.example.runtime",
                        "artifact_hash": "sha256:" + "d" * 64,
                    }
                ],
                "extensions": {
                    "rumi.legacy_projection": {
                        "manifest": {"provenance": {"content_hash": "generated"}}
                    },
                    "source_extension": {"stable": True},
                },
                "provenance": {"content_hash": "sha256:" + "e" * 64},
            }
        ),
        encoding="utf-8",
    )

    legacy_view = _migration_source_view(ecosystem)
    v3_view = _migration_source_view(v3)

    legacy_payload = json.loads(ecosystem.read_text(encoding="utf-8"))
    legacy_payload["metadata"]["canonical_v4"]["artifact_digest"] = "sha256:" + "f" * 64
    legacy_payload["provenance"]["content_hash"] = "sha256:" + "0" * 64
    ecosystem.write_text(json.dumps(legacy_payload), encoding="utf-8")
    v3_payload = json.loads(v3.read_text(encoding="utf-8"))
    v3_payload["extensions"]["rumi.legacy_projection"]["manifest"] = {
        "provenance": {"content_hash": "changed"}
    }
    v3_payload["provenance"]["content_hash"] = "sha256:" + "1" * 64
    v3.write_text(json.dumps(v3_payload), encoding="utf-8")

    assert _migration_source_view(ecosystem) == legacy_view
    assert _migration_source_view(v3) == v3_view


def test_v3_import_hashes_entrypoint_bytes_not_stale_projection_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime edit must update v4 authority before v3 projection repair."""
    source_root = CATALOG.parents[1] / "ecosystem" / "rumi_provider_adapters_pack"
    pack_root = tmp_path / source_root.name
    pack_root.mkdir()
    for name in ("ecosystem.json", "rumi.pack.v3.json"):
        payload = json.loads((source_root / name).read_text(encoding="utf-8"))
        if name == "rumi.pack.v3.json":
            for entrypoint in payload["entrypoints"]:
                entrypoint["artifact_hash"] = "sha256:" + "0" * 64
        (pack_root / name).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        "scripts.migrate_pack_artifacts_v4._runtime_artifacts",
        lambda _pack_root: [],
    )
    monkeypatch.setattr(
        "scripts.migrate_pack_artifacts_v4._source_evidence",
        lambda _pack_root, _paths: [],
    )

    record = _import_record(pack_root)
    expected = _file_digest(source_root / "runtime" / "adapter.py")
    imported = {
        operation["implementation_digest"]
        for contract in record["provided_contracts"]
        for operation in contract["operations"]
    }

    assert imported == {expected}


def test_all_packs_have_valid_deterministic_v4_artifacts() -> None:
    """All 143 owned Packs must match a second byte-identical generation."""
    result = generate(check=True)
    assert result == {
        "packs": 143,
        "valid": 143,
        "contracts": 162,
            "operations": 221,
    }
    payload = _catalog()
    assert payload["excluded_packs"] == sorted(EXCLUDED_PACKS)
    for record in payload["packs"]:
        files = _render_record(record)
        verify_rendered_artifacts(files)
        pack_root = CATALOG.parents[1] / "ecosystem" / record["pack_id"]
        assert {name: (pack_root / name).read_text(encoding="utf-8") for name in files} == files


def test_normal_generation_has_no_v3_or_legacy_authority_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal generation may consume only the canonical v4 source catalog."""
    original = Path.read_text

    def guarded_read(path: Path, *args: object, **kwargs: object) -> str:
        if path.name in {"rumi.pack.v3.json", "ecosystem.json"}:
            raise AssertionError(f"legacy authority read: {path}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    assert generate(check=True)["valid"] == 143


@pytest.mark.parametrize("failure", ["duplicate", "missing", "unknown", "malformed"])
def test_catalog_rejects_inventory_and_shape_failures(failure: str) -> None:
    """Unknown, missing, duplicate, and malformed source records fail closed."""
    payload = copy.deepcopy(_catalog())
    if failure == "duplicate":
        payload["packs"][-1] = copy.deepcopy(payload["packs"][0])
    elif failure == "missing":
        payload["packs"].pop()
    elif failure == "unknown":
        payload["packs"][-1]["pack_id"] = "unknown_pack"
    else:
        payload["packs"][0].pop("network")
    with pytest.raises(PackV4MigrationError):
        _validate_catalog_payload(payload)


def test_tampered_and_stale_generated_artifacts_fail_closed() -> None:
    """Content tampering and stale integrity seals cannot pass artifact checks."""
    record = _catalog()["packs"][0]
    files = _render_record(record)

    tampered = dict(files)
    manifest = json.loads(tampered["pack.v4.json"])
    manifest["pack"]["display_name"] += " tampered"
    tampered["pack.v4.json"] = json.dumps(manifest, sort_keys=True) + "\n"
    with pytest.raises(PackV4MigrationError, match="digest mismatch"):
        verify_rendered_artifacts(tampered)

    stale = dict(files)
    index = json.loads(stale["artifact-index.v4.json"])
    index["integrity_seal"]["signed_digest"] = "sha256:" + "0" * 64
    stale["artifact-index.v4.json"] = json.dumps(index, sort_keys=True) + "\n"
    with pytest.raises(PackV4MigrationError, match="integrity seal"):
        verify_rendered_artifacts(stale)

    malformed = dict(files)
    malformed["pack.v4.json"] = "{}\n"
    with pytest.raises(SchemaValidationError):
        verify_rendered_artifacts(malformed)


def test_global_catalog_has_no_duplicate_provider_or_operation() -> None:
    """Canonical Provider and Operation identities are globally unique."""
    providers: set[str] = set()
    operations: set[str] = set()
    owners: dict[str, str] = {}
    for record in _catalog()["packs"]:
        files = _render_record(record)
        manifest = json.loads(files["pack.v4.json"])
        contracts = json.loads(files["contracts.v4.json"])["contracts"]
        for contract in contracts:
            assert (
                owners.setdefault(contract["contract_id"], contract["owner"]) == contract["owner"]
            )
        for provider in manifest["provider_catalog"]:
            assert provider["provider_id"] not in providers
            providers.add(provider["provider_id"])
        for operation in manifest["operation_catalog"]:
            assert operation["source_kind"] == "canonical_v4_contract"
            assert operation["operation_id"] not in operations
            operations.add(operation["operation_id"])
    assert len(providers) == 162
    assert len(operations) == 221


def test_legacy_operation_source_cannot_enter_v4_authority_catalog() -> None:
    """Offline legacy evidence is never an executable v4 Operation."""
    record = next(
        item
        for item in _catalog()["packs"]
        if item["pack_id"] == "rumi_agent_services_pack"
    )
    files = _render_record(record)
    manifest = json.loads(files["pack.v4.json"])
    assert manifest["functions"] == []
    assert manifest["operation_catalog"] == []
    assert manifest["provider_catalog"] == []

    manifest["operation_catalog"] = [
        {
            "operation_id": "rumi_agent_services_pack.legacy.operation",
            "owner": record["pack_id"],
            "source_kind": "legacy_component",
            "effect_ceiling": [],
        }
    ]
    files["pack.v4.json"] = json.dumps(manifest, sort_keys=True) + "\n"
    with pytest.raises(PackV4MigrationError, match="executable catalog entries"):
        verify_rendered_artifacts(files)
