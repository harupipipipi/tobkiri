from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
V4_ROOT = ROOT / "tobkiri_runtime" / "ecosystem" / "defaultspack" / "v4"
MODULE_PATH = Path(__file__).with_name("presentation_catalog_v4.py")
SPEC = importlib.util.spec_from_file_location("presentation_catalog_v4", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _copy_bundle(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    bundle = repository / "tobkiri_runtime" / "ecosystem" / "defaultspack" / "v4"
    bundle.parent.mkdir(parents=True)
    shutil.copytree(V4_ROOT, bundle)
    return repository, bundle


def test_v4_catalog_is_byte_identical_and_uninstalled_variants_fail_closed(
    tmp_path: Path,
) -> None:
    first = tmp_path / "catalog-first.json"
    second = tmp_path / "catalog-second.json"
    MODULE.write_presentation_catalog(ROOT, first)
    MODULE.write_presentation_catalog(ROOT, second)

    assert first.read_bytes() == second.read_bytes()
    catalog = json.loads(first.read_text(encoding="utf-8"))
    assert catalog["default_profile_source"].endswith("/v4/defaults.profile.v4.json")
    assert all(
        "domain/pack_architecture" not in value
        for value in json.dumps(catalog, sort_keys=True).split()
    )
    assert catalog["default_selection"]["shell_provider_id"] == "shell.tauri.default"
    bundle = MODULE.load_v4_bundle(ROOT)
    assert "shell.tauri.default" in bundle.selected_pack_ids
    assert "shell.cli.default" not in bundle.selected_pack_ids
    assert bundle.executable_catalogs
    for pack_id, executable in bundle.executable_catalogs.items():
        pack = bundle.packs[pack_id].value
        sidecars = [
            item
            for item in pack["artifacts"]
            if item["path"] == "executables.v4.json"
        ]
        assert len(sidecars) == 1
        assert sidecars[0]["kind"] == "sidecar"
        assert sidecars[0]["digest"] == executable.digest
        assert executable.value["source_identity"] == pack["integrity"]["source_identity"]
    assert "shell.tauri.default" in catalog["source_manifest_digests"]
    assert "shell.cli.default" not in catalog["source_manifest_digests"]
    shell = catalog["shell_providers"][0]
    assert shell["provider_id"] == "shell.tauri.default"
    assert {
        (item["platform"], item["architecture"])
        for item in shell["artifact_variants"]
    } == {
        ("macos", "arm64"),
        ("macos", "x86_64"),
        ("windows", "x86_64"),
        ("linux", "x86_64"),
    }
    assert all(
        item[field] is None
        for item in shell["artifact_variants"]
        for field in ("path", "sha256", "size", "source_identity", "source_revision")
    )
    assert "release_binding" not in catalog


def test_source_catalog_drops_stale_installed_metadata_and_release_binding(
    tmp_path: Path,
) -> None:
    target = tmp_path / "catalog.json"
    MODULE.write_presentation_catalog(ROOT, target)
    catalog = json.loads(target.read_text(encoding="utf-8"))
    variant: dict[str, Any] = {
        "artifact_id": "shell.tauri.default.macos-arm64",
        "platform": "macos",
        "architecture": "arm64",
    }
    installed: dict[str, Any] = {
        "path": "bundled/presentation-artifacts/shell.tauri.default.macos-arm64/Tobkiri.app",
        "sha256": "sha256:" + "1" * 64,
        "size": 17,
        "source_identity": "github:tobkiri/shell",
        "source_revision": "release-2026-08-05",
    }
    variant.update(installed)
    catalog["shell_providers"][0]["artifact_variants"] = [variant]
    binding = {
        "schema": "io.tobkiri.shell.release.v4",
        "artifact_index_path": "bundled/shell_artifact_index.v4.json",
        "artifact_index_sha256": "sha256:" + "2" * 64,
        "profile_lock_path": "bundled/shell_profile_lock.v4.json",
        "profile_lock_sha256": "sha256:" + "3" * 64,
        "catalog_revision": "sha256:" + "4" * 64,
        "artifact_id": variant["artifact_id"],
        "source_identity": installed["source_identity"],
        "source_revision": installed["source_revision"],
        "platform": "macos",
        "architecture": "arm64",
    }
    catalog["release_binding"] = binding
    target.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    generated = MODULE.generate_presentation_catalog(ROOT, target)
    generated_variants = generated["shell_providers"][0]["artifact_variants"]
    assert len(generated_variants) == 4
    assert all(
        item[field] is None
        for item in generated_variants
        for field in ("path", "sha256", "size", "source_identity", "source_revision")
    )
    assert "release_binding" not in generated


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("tamper", "digest changed"),
        ("missing", "regular file"),
        ("relative", "unsafe"),
        ("symlink", "symlink"),
        ("unsupported", "unsupported v4 bundle kind"),
    ),
)
def test_v4_bundle_rejects_tamper_missing_relative_symlink_and_unsupported(
    tmp_path: Path, case: str, message: str
) -> None:
    repository, bundle = _copy_bundle(tmp_path)
    target = bundle / "packs" / "defaultspack.pack.v4.json"
    lock_path = bundle / "bundle.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    if case == "tamper":
        target.write_bytes(target.read_bytes() + b" ")
    elif case == "missing":
        target.unlink()
    elif case == "relative":
        lock["entries"][0]["path"] = "../packs/defaults-basepack.pack.v4.json"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "symlink":
        target.unlink()
        target.symlink_to(V4_ROOT / "packs" / "defaultspack.pack.v4.json")
    else:
        lock["entries"][0]["kind"] = "unsupported"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(MODULE.PresentationCatalogError, match=message):
        MODULE.load_v4_bundle(repository)
