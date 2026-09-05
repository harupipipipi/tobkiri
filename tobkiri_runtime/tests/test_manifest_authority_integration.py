"""Repository gates for classified Pack authority and production loading."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from backend_core.ecosystem.registry import LegacyRegistryUnavailable, Registry
from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from scripts.quality.legacy_manifest_v3 import load_manifest
from core_runtime.manifest_authority import (
    ManifestAuthorityError,
    load_manifest_authority_catalog,
    validate_manifest_authority_scope,
)
from scripts.offline_legacy_projection import (
    ManifestProjectionError,
    generate_legacy_ecosystem_projection,
)
from scripts.migrate_manifest_authority import _normalize_artifact_index
from core_runtime.pack_artifact_integrity import verify_declared_artifacts
from core_runtime.resolved_profile import ResolutionInput, resolve_profile

ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM = ROOT / "ecosystem"


def test_every_repository_pack_has_one_explicit_authority() -> None:
    catalog = load_manifest_authority_catalog()
    direct_pack_ids = tuple(
        sorted(
            path.name
            for path in ECOSYSTEM.iterdir()
            if (path / "pack.v4.json").is_file()
        )
    )

    validate_manifest_authority_scope(
        direct_pack_ids,
        require_complete_catalog=True,
    )
    assert len(catalog) == 140
    assert set(catalog.values()) == {"v4-authoritative"}
    assert catalog["defaults"] == "v4-authoritative"
    assert catalog["defaultspack"] == "v4-authoritative"


def test_authority_scope_rejects_missing_extra_and_implicit_inputs() -> None:
    catalog = load_manifest_authority_catalog()
    pack_ids = tuple(catalog)

    with pytest.raises(ManifestAuthorityError, match="must be explicit"):
        validate_manifest_authority_scope(None)
    with pytest.raises(ManifestAuthorityError, match="extra=.*injected_pack"):
        validate_manifest_authority_scope((*pack_ids, "injected_pack"))
    with pytest.raises(ManifestAuthorityError, match="stale="):
        validate_manifest_authority_scope(
            pack_ids[:-1],
            require_complete_catalog=True,
        )

    validate_manifest_authority_scope(pack_ids[:1])


def test_all_authoritative_manifests_and_projections_are_valid() -> None:
    catalog = load_manifest_authority_catalog()

    for pack_id, authority in sorted(catalog.items()):
        pack_root = ECOSYSTEM / pack_id
        ecosystem_path = pack_root / "ecosystem.json"
        assert authority == "v4-authoritative"
        assert (pack_root / "pack.v4.json").is_file()
        assert (pack_root / "contracts.v4.json").is_file()
        assert (pack_root / "executables.v4.json").is_file()
        assert (pack_root / "artifact-index.v4.json").is_file()
        v3_path = pack_root / "rumi.pack.v3.json"
        if not ecosystem_path.exists():
            assert not v3_path.exists()
            continue
        ecosystem = json.loads(ecosystem_path.read_text(encoding="utf-8"))
        assert validate_ecosystem(ecosystem, raise_on_error=False) == [], pack_id
        metadata = ecosystem["metadata"]
        assert metadata["manifest_authority"] == "v4-authoritative"
        assert metadata["read_only_projection"] is True
        assert metadata["generated_from"]["source"] == "pack.v4.json"
        assert metadata["generated_from"]["source_content_hash"]
        integrity_ok, integrity_diagnostics = verify_declared_artifacts(
            pack_root,
            ecosystem,
        )
        assert integrity_ok, (pack_id, integrity_diagnostics)
        if v3_path.is_file():
            loaded = load_manifest(v3_path)
            assert loaded.ok, (pack_id, loaded.diagnostics)
            canonical = json.loads(v3_path.read_text(encoding="utf-8"))
            projection = canonical["extensions"]["tobkiri.offline_projection"]
            assert projection["owner"] == pack_id
            assert projection["source"] == "pack.v4.json"
            assert projection["runtime_executable"] is False


def test_projection_and_artifact_tamper_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack_id = "rumi_conversation_store_pack"
    source_root = ECOSYSTEM / pack_id
    copied_ecosystem = tmp_path / "ecosystem"
    copied_root = copied_ecosystem / pack_id
    shutil.copytree(source_root, copied_root)
    v3_path = copied_root / "rumi.pack.v3.json"
    ecosystem_path = copied_root / "ecosystem.json"

    ecosystem_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ManifestProjectionError, match="drift"):
        generate_legacy_ecosystem_projection(v3_path, ecosystem_path, check=True)

    shutil.copy2(source_root / "ecosystem.json", ecosystem_path)
    ecosystem = json.loads(ecosystem_path.read_text(encoding="utf-8"))
    artifact_index = copied_root / "artifact-manifest.json"
    artifact_index.write_text('{"artifacts": []}\n', encoding="utf-8")
    monkeypatch.setattr(
        "core_runtime.pack_artifact_integrity.ECOSYSTEM_DIR",
        copied_ecosystem,
    )
    integrity_ok, diagnostics = verify_declared_artifacts(
        copied_root,
        ecosystem,
    )
    assert integrity_ok is False
    assert "artifact manifest hash does not match provenance" in diagnostics


def test_v3_artifact_sidecar_generator_refreshes_hash_without_projection_rebind(
    tmp_path: Path,
) -> None:
    """The authority generator refreshes orphan v3 sidecars deterministically."""
    pack_root = tmp_path / "rumi_file_inspect_pack"
    runtime = pack_root / "runtime"
    runtime.mkdir(parents=True)
    source = runtime / "inspect.py"
    source.write_text("print('current')\n", encoding="utf-8")
    artifact_index = pack_root / "artifact-manifest.json"
    artifact_index.write_text(
        '{"schema_version":"rumi.artifact-manifest.v1",'
        '"artifacts":[{"path":"runtime/inspect.py",'
        '"sha256":"' + "0" * 64 + '","role":"runtime"}]}\n',
        encoding="utf-8",
    )

    assert (
        _normalize_artifact_index(
            pack_root,
            {},
            check=False,
            include_unreferenced_sidecar=True,
        )
        is None
    )
    first = artifact_index.read_bytes()
    assert (
        str(json.loads(first)["artifacts"][0]["sha256"])
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )

    assert (
        _normalize_artifact_index(
            pack_root,
            {},
            check=True,
            include_unreferenced_sidecar=True,
        )
        is None
    )
    assert artifact_index.read_bytes() == first


def test_referenced_artifact_sidecar_persists_refreshed_hash(
    tmp_path: Path,
) -> None:
    """Referenced indexes must write the same digest used for provenance."""
    pack_root = tmp_path / "referenced_pack"
    runtime = pack_root / "runtime"
    runtime.mkdir(parents=True)
    source = runtime / "adapter.py"
    source.write_text("VALUE = 'current'\n", encoding="utf-8")
    artifact_index = pack_root / "artifact-manifest.json"
    artifact_index.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "path": "runtime/adapter.py",
                        "sha256": "sha256:" + "0" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ecosystem = {"metadata": {"integrity": {"artifact_manifest": "artifact-manifest.json"}}}

    index_hash = _normalize_artifact_index(
        pack_root,
        ecosystem,
        check=False,
    )
    payload = json.loads(artifact_index.read_text(encoding="utf-8"))
    expected = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()

    assert payload["artifacts"][0]["sha256"] == expected
    assert index_hash == "sha256:" + hashlib.sha256(artifact_index.read_bytes()).hexdigest()
    assert _normalize_artifact_index(pack_root, ecosystem, check=True) == index_hash


def test_removed_registry_rejects_runtime_discovery(tmp_path: Path) -> None:
    """Pack v4 runtime must refuse the removed filesystem registry path."""
    with pytest.raises(LegacyRegistryUnavailable, match="removed"):
        Registry(str(tmp_path / "ecosystem")).load_all_packs()


def test_invalid_v3_manifest_is_not_available_or_effective(tmp_path: Path) -> None:
    pack_root = tmp_path / "ecosystem" / "invalid_pack"
    pack_root.mkdir(parents=True)
    ecosystem = {
        "pack_id": "invalid_pack",
        "pack_identity": "local:invalid_pack",
        "version": "1.0.0",
        "vocabulary": {"types": ["service"]},
        "dependencies": {},
    }
    (pack_root / "ecosystem.json").write_text(json.dumps(ecosystem), encoding="utf-8")
    example = json.loads(
        (ROOT / "examples" / "pack_v3" / "minimal_service.json").read_text(encoding="utf-8")
    )
    example["unknown_authority"] = True
    (pack_root / "rumi.pack.v3.json").write_text(json.dumps(example), encoding="utf-8")
    resolution_input = ResolutionInput(
        profile_id="invalid-v3",
        profile_revision="1",
        platform="test",
        policy_revision="1",
        lockfile_revision=None,
        requested_pack_ids=("invalid_pack",),
        authorized_pack_ids=("invalid_pack",),
        healthy_pack_ids=("invalid_pack",),
    )

    plan = resolve_profile(
        resolution_input,
        ecosystem_dir=tmp_path / "ecosystem",
    )

    assert plan.available_pack_ids == ()
    assert plan.effective_pack_set == ()
    assert any(item.code == "offline_projection_not_authority" for item in plan.diagnostics)


def test_removed_binding_authorities_are_not_importable() -> None:
    """v4 composition must not restore deleted Core binding registries."""
    for module_name in (
        "core_runtime.capability_binding_registration",
        "core_runtime.function_registry",
        "core_runtime.interface_registry",
    ):
        assert importlib.util.find_spec(module_name) is None
