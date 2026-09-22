"""Independent regression contracts for the manifest/loader root fix.

These tests intentionally describe the repository state expected after the
manifest authority migration.  They are kept in a dedicated file so the
production fix can be applied independently of the existing Wave 0 tests.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from backend_core.ecosystem.registry import LegacyRegistryUnavailable, Registry
from backend_core.ecosystem.spec.schema.validator import (
    SchemaValidationError,
    validate_ecosystem,
)
from scripts.quality.legacy_manifest_v3 import load_manifest
from scripts.offline_legacy_projection import (
    generate_legacy_ecosystem_projection,
    source_manifest_identity,
)
from core_runtime.paths import discover_pack_locations
from core_runtime.resolved_profile import ResolutionInput, resolve_profile


ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM = ROOT / "ecosystem"
EXAMPLE_V3 = ROOT / "examples" / "pack_v3" / "minimal_service.json"


def _authority_module():
    """Import the authority module while keeping the baseline failure useful."""
    try:
        return importlib.import_module("core_runtime.manifest_authority")
    except ModuleNotFoundError as exc:
        pytest.fail(f"manifest authority catalog/loader is not present at the baseline: {exc}")


def _write_legacy_pack(root: Path, pack_id: str) -> Path:
    """Write the smallest schema-valid legacy Pack fixture."""
    pack_dir = root / pack_id
    pack_dir.mkdir(parents=True)
    (pack_dir / "ecosystem.json").write_text(
        json.dumps(
            {
                "pack_id": pack_id,
                "pack_identity": f"local:{pack_id}",
                "version": "1.0.0",
                "vocabulary": {"types": ["service"]},
            }
        ),
        encoding="utf-8",
    )
    return pack_dir


def test_repository_authority_catalog_is_exact_and_has_no_loader_gaps() -> None:
    """Every discovered Pack has one explicit authority and a matching loader."""
    authority = _authority_module()
    authority.load_manifest_authority_catalog.cache_clear()
    locations = discover_pack_locations(str(ECOSYSTEM))
    direct_pack_ids = {
        path.name
        for path in ECOSYSTEM.iterdir()
        if path.is_dir() and path.name != "setup_pack" and not path.name.startswith(".")
    }
    authority.validate_manifest_authority_scope(
        direct_pack_ids,
        require_complete_catalog=True,
    )
    catalog = authority.load_manifest_authority_catalog()

    assert len(locations) == 141
    assert len(catalog) == 143
    assert set(catalog) == direct_pack_ids
    assert set(catalog.values()) == {"v4-authoritative"}
    assert catalog["defaults"] == "v4-authoritative"
    assert catalog["defaultspack"] == "v4-authoritative"
    assert direct_pack_ids - {location.pack_id for location in locations} == {
        "defaults",
        "defaultspack",
    }

    for location in locations:
        manifest_path = location.pack_subdir / "ecosystem.json"
        v3_path = location.pack_subdir / "rumi.pack.v3.json"
        pack_authority = catalog[location.pack_id]
        assert pack_authority == "v4-authoritative"
        assert manifest_path.is_file()
        assert (location.pack_subdir / "pack.v4.json").is_file()
        assert (location.pack_subdir / "executables.v4.json").is_file()
        if v3_path.is_file():
            assert v3_path.is_file(), location.pack_id
            legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
            metadata = legacy.get("metadata", {})
            assert metadata.get("manifest_authority") == "v4-authoritative"
            assert metadata.get("generated") is True
            assert metadata.get("read_only_projection") is True


def test_all_repository_legacy_manifests_validate_without_silent_exclusion() -> None:
    """The legacy projection audit must accept all 141 repository manifests."""
    paths = sorted(ECOSYSTEM.glob("*/ecosystem.json"))
    errors: list[str] = []
    for path in paths:
        try:
            validate_ecosystem(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, SchemaValidationError) as exc:
            errors.append(f"{path.parent.name}: {exc}")

    assert len(paths) == 141
    assert not errors, "legacy manifest diagnostics: " + " | ".join(errors[:8])


def test_all_repository_v3_manifests_validate_with_actionable_diagnostics() -> None:
    """Every checked-in v3 manifest must be accepted by the canonical loader."""
    paths = sorted(ECOSYSTEM.glob("*/rumi.pack.v3.json"))
    errors: list[str] = []
    for path in paths:
        result = load_manifest(path)
        if not result.ok:
            errors.append(f"{path.parent.name}: {'; '.join(result.diagnostics)}")

    assert len(paths) == 95
    assert not errors, "v3 manifest diagnostics: " + " | ".join(errors[:8])


def test_removed_legacy_registry_rejects_filesystem_discovery(
    tmp_path: Path,
) -> None:
    """Invalid or installed legacy Packs never reach the v4 runtime registry."""
    with pytest.raises(LegacyRegistryUnavailable, match="removed"):
        Registry(str(tmp_path / "ecosystem")).load_all_packs()


def test_v3_projection_is_legacy_schema_valid_source_bound_and_deterministic(
    tmp_path: Path,
) -> None:
    """Canonical v3 data owns a deterministic, integrity-bound legacy projection."""
    canonical = tmp_path / "rumi.pack.v3.json"
    output = tmp_path / "ecosystem.json"
    manifest = json.loads(EXAMPLE_V3.read_text(encoding="utf-8"))
    canonical.write_text(EXAMPLE_V3.read_text(encoding="utf-8"), encoding="utf-8")

    source_identity = generate_legacy_ecosystem_projection(canonical, output)
    first_bytes = output.read_bytes()
    generate_legacy_ecosystem_projection(canonical, output, check=True)
    second_bytes = output.read_bytes()
    projection = json.loads(second_bytes)

    validate_ecosystem(projection)
    assert first_bytes == second_bytes
    assert source_identity == source_manifest_identity(manifest)
    assert projection["metadata"]["manifest_authority"] == "v3-authoritative"
    assert projection["metadata"]["generated"] is True
    assert projection["metadata"]["read_only_projection"] is True
    assert projection["metadata"]["generated_from"]["source_content_hash"] == source_identity


def test_repository_v3_projections_are_current_and_source_integrity_bound() -> None:
    """No v3-authoritative Pack may have a missing, stale, or hand-edited projection."""
    authority = _authority_module()
    catalog = authority.load_manifest_authority_catalog()
    errors: list[str] = []
    for pack_id, pack_authority in sorted(catalog.items()):
        if pack_authority != "v3-authoritative":
            continue
        pack_dir = ECOSYSTEM / pack_id
        canonical = pack_dir / "rumi.pack.v3.json"
        projection = pack_dir / "ecosystem.json"
        try:
            source_identity = generate_legacy_ecosystem_projection(
                canonical,
                projection,
                check=True,
            )
            rendered = json.loads(projection.read_text(encoding="utf-8"))
            if rendered["metadata"]["generated_from"]["source_content_hash"] != source_identity:
                errors.append(f"{pack_id}: source hash mismatch")
        except Exception as exc:
            errors.append(f"{pack_id}: {exc}")

    assert not errors, "projection diagnostics: " + " | ".join(errors[:8])


def test_invalid_v3_manifest_is_not_available_or_effective(
    tmp_path: Path,
) -> None:
    """A v3 error must never remain in the resolved effective Pack set."""
    pack_id = "invalid_v3_profile_pack"
    ecosystem_dir = tmp_path / "ecosystem"
    pack_dir = _write_legacy_pack(ecosystem_dir, pack_id)
    (pack_dir / "rumi.pack.v3.json").write_text(
        json.dumps(
            {
                "pack_api_version": "rumi.pack.v3",
                "unknown_schema_key": True,
            }
        ),
        encoding="utf-8",
    )

    plan = resolve_profile(
        ResolutionInput(
            profile_id="invalid-v3-regression",
            profile_revision="r1",
            platform="test",
            policy_revision="p1",
            lockfile_revision=None,
            requested_pack_ids=(pack_id,),
            authorized_pack_ids=(pack_id,),
            healthy_pack_ids=(pack_id,),
        ),
        ecosystem_dir=ecosystem_dir,
    )

    assert pack_id not in plan.available_pack_ids
    assert pack_id not in plan.effective_pack_set
    assert any(
        item.code == "offline_projection_not_authority"
        and item.severity == "error"
        and item.subject == pack_id
        for item in plan.diagnostics
    )


def test_removed_binding_modules_are_not_importable() -> None:
    """v4 composition must not restore deleted Core binding registries."""
    for module_name in (
        "core_runtime.capability_binding_registration",
        "core_runtime.function_registry",
        "core_runtime.interface_registry",
    ):
        assert importlib.util.find_spec(module_name) is None
