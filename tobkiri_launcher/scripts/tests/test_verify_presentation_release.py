"""Boundary tests for presentation verification after Python packaging retirement."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = SCRIPTS / "verify_presentation_release.py"
PACKAGE_SCRIPT = SCRIPTS / "package_presentation_artifact.py"
ROOT = Path(__file__).resolve().parents[3]
V4_ROOT = ROOT / "tobkiri_runtime" / "ecosystem" / "defaultspack" / "v4"
VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_presentation_release", VERIFY_SCRIPT
)
assert VERIFY_SPEC and VERIFY_SPEC.loader
VERIFY = importlib.util.module_from_spec(VERIFY_SPEC)
sys.modules[VERIFY_SPEC.name] = VERIFY
VERIFY_SPEC.loader.exec_module(VERIFY)


def test_release_verifier_has_no_direct_packager_dependency() -> None:
    """Release verification cannot resurrect the retired Python producer."""
    source = VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert "package_presentation_artifact" not in source
    assert "TOBKIRI_PACKAGING_SOURCE_PROVENANCE_FILE" not in source
    ast.parse(source, filename=str(VERIFY_SCRIPT))


def test_retired_packager_is_a_single_fail_closed_boundary() -> None:
    """The old script is not a pathname-based producer/consumer bridge."""
    source = PACKAGE_SCRIPT.read_text(encoding="utf-8")
    assert source.count("_reject_direct_caller") >= 2
    assert "verified_catalog" in source
    assert "lease" in source
    assert "exec_module" not in source
    assert "subprocess" not in source


def _lock_entries() -> list[dict[str, str]]:
    lock = json.loads((V4_ROOT / "bundle.lock.json").read_text(encoding="utf-8"))
    return lock["entries"]


def test_official_bundle_lock_with_interleaved_executable_catalogs_verifies() -> None:
    """The release verifier accepts the canonical 135-entry source bundle."""
    entries = _lock_entries()
    assert len(entries) == 135
    assert sum(entry["kind"] == "executable_catalog" for entry in entries) == 63
    VERIFY._verify_defaultspack_bundle(entries, V4_ROOT)


@pytest.mark.parametrize(
    "mutation", ("missing", "duplicate", "position", "prefix", "digest")
)
def test_release_verifier_rejects_bundle_lock_drift(
    tmp_path: Path, mutation: str
) -> None:
    """Missing, duplicate, misplaced, wrong-domain, and stale entries fail closed."""
    bundle = tmp_path / "v4"
    shutil.copytree(V4_ROOT, bundle)
    entries = _lock_entries()
    if mutation == "missing":
        index = next(
            index
            for index, entry in enumerate(entries)
            if entry["kind"] == "executable_catalog"
        )
        entries.pop(index)
    elif mutation == "duplicate":
        entries.append(
            dict(
                next(
                    entry
                    for entry in entries
                    if entry["kind"] == "executable_catalog"
                )
            )
        )
    elif mutation == "position":
        index = next(
            index
            for index, entry in enumerate(entries)
            if entry["kind"] == "executable_catalog"
        )
        entries[index - 1], entries[index] = entries[index], entries[index - 1]
    elif mutation == "prefix":
        entries[0] = {**entries[0], "path": "wrong/pack.pack.v4.json"}
    else:
        entries[0] = {**entries[0], "digest": "sha256:" + "0" * 64}

    with pytest.raises(RuntimeError):
        VERIFY._verify_defaultspack_bundle(entries, bundle)
