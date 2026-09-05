"""Regression tests for the finite Pack boundary used by runtime code."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core_runtime.pack_boundary import (
    PackBoundaryError,
    declared_pack_dependencies,
    load_pack_catalog,
    resolve_pack_root,
    resolve_selected_pack_roots,
    finite_files,
)


RUNTIME = Path(__file__).resolve().parents[1]
REPOSITORY = RUNTIME.parent


def test_v4_catalog_resolves_only_explicit_pack_ids() -> None:
    """The catalog owns all Pack roots and rejects injected or missing IDs."""

    catalog = load_pack_catalog()
    assert len(catalog) == 140
    assert set(resolve_selected_pack_roots(["defaults", "defaultspack"])) == {
        "defaults",
        "defaultspack",
    }
    assert resolve_pack_root("defaultspack").name == "defaultspack"
    with pytest.raises(PackBoundaryError):
        resolve_pack_root("untrusted_installed_pack")
    with pytest.raises(PackBoundaryError):
        resolve_selected_pack_roots(["defaultspack", "defaultspack"])


def test_catalog_dependencies_are_finite_and_legacy_projection_is_clean() -> None:
    """Dependency closure comes from v4 records and legacy aliases stay projected."""

    catalog = load_pack_catalog()
    for pack_id in catalog:
        dependencies = declared_pack_dependencies(pack_id)
        assert all(dependency in catalog for dependency in dependencies)
        legacy = resolve_pack_root(pack_id) / "ecosystem.json"
        if legacy.is_file():
            import json

            payload = json.loads(legacy.read_text(encoding="utf-8"))
            assert "defaultspack" not in payload.get("dependencies", {})


def test_pack_architecture_boundary_debt_is_exactly_baselined() -> None:
    """Production boundary debt cannot grow outside the reviewed exact baseline."""

    scanner_dir = RUNTIME / "scripts" / "quality"
    sys.path.insert(0, str(scanner_dir))
    try:
        import scan_pack_architecture
    finally:
        sys.path.pop(0)

    baseline = scan_pack_architecture.load_baseline(
        REPOSITORY / "scripts" / "quality" / "pack_architecture_baseline.json"
    )
    violations = scan_pack_architecture.scan_repository(REPOSITORY)

    assert len(baseline) == 44
    assert len(violations) == 44
    assert scan_pack_architecture.find_unbaselined_violations(violations, baseline) == []
    assert scan_pack_architecture.find_stale_baseline_exceptions(
        violations, baseline
    ) == []


def test_finite_boundary_rejects_symlinked_pack_roots_and_files(
    tmp_path: Path,
) -> None:
    ecosystem = tmp_path / "ecosystem"
    ecosystem.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ecosystem / "defaultspack").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PackBoundaryError, match="symlink"):
        resolve_pack_root("defaultspack", ecosystem)

    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    secret = outside / "secret.json"
    secret.write_text("{}", encoding="utf-8")
    (safe_root / "secret.json").symlink_to(secret)
    with pytest.raises(PackBoundaryError, match="symlink"):
        finite_files(safe_root, (".json",))
