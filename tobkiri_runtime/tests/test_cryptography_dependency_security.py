"""Security contract for the direct cryptography dependency."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
import pytest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RUNTIME_ROOT.parent
SECURE_VERSION = Version("50.0.0")
TARGET_LOCK = RUNTIME_ROOT / "requirements-packaging-aarch64-apple-darwin.txt"
LOCK_GENERATOR_PATH = REPOSITORY_ROOT / ".github/scripts/generate_packaging_dependency_locks.py"
_AFFECTED_PKCS7_DECRYPT_APIS = {
    "pkcs7_decrypt_der",
    "pkcs7_decrypt_pem",
    "pkcs7_decrypt_smime",
}


def _load_lock_generator():
    """Load the checked-in target-lock generator without package-path fallback."""
    spec = importlib.util.spec_from_file_location(
        "tobkiri_packaging_dependency_lock_tests", LOCK_GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LOCK_GENERATOR = _load_lock_generator()


def _locked_cryptography_version() -> Version:
    lock_text = (RUNTIME_ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(
        r'\[\[package\]\]\nname = "cryptography"\nversion = "([^"]+)"',
        lock_text,
    )
    assert match is not None, "uv.lock must contain the direct cryptography package"
    return Version(match.group(1))


def test_cryptography_is_direct_and_pinned_to_fully_fixed_release() -> None:
    """Keep the direct dependency beyond all three audited advisories."""
    pyproject = (RUNTIME_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declarations = re.findall(r'^\s*"(cryptography[^"]*)",?$', pyproject, re.MULTILINE)
    assert declarations == ["cryptography==50.0.0"]

    requirement = Requirement(declarations[0])
    assert requirement.marker is None
    assert requirement.specifier == "==50.0.0"

    locked_version = _locked_cryptography_version()
    assert locked_version == SECURE_VERSION

    expected_pin = f"cryptography=={locked_version} "
    for export_name in ("requirements.txt", "requirements-dev.txt"):
        export = (RUNTIME_ROOT / export_name).read_text(encoding="utf-8")
        assert export.count(expected_pin) == 1


def test_vulnerable_pkcs7_decryption_entrypoints_are_not_used() -> None:
    """Keep untrusted PKCS#7 decryption out of runtime error and timing surfaces."""
    uses: list[str] = []
    source_roots = (
        RUNTIME_ROOT / "core_runtime",
        RUNTIME_ROOT / "ecosystem",
        RUNTIME_ROOT / "scripts",
        RUNTIME_ROOT / "tobkiri_protocol",
    )
    for source_root in source_roots:
        for path in source_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if function_name in _AFFECTED_PKCS7_DECRYPT_APIS:
                    uses.append(f"{path.relative_to(RUNTIME_ROOT)}:{node.lineno}")

    assert uses == []


def _locked_packages() -> dict[tuple[str, str], dict[str, object]]:
    """Return the canonical uv distribution metadata keyed by name/version."""
    lock = tomllib.loads((RUNTIME_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {
        (canonicalize_name(str(package["name"])), str(package["version"])): package
        for package in lock["package"]
    }


def _exported_requirements(path: Path) -> dict[tuple[str, str], set[str]]:
    """Parse pinned exports and retain their exact sha256 provenance lines."""
    packages: dict[tuple[str, str], set[str]] = {}
    current: tuple[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if line and not line[0].isspace() and not line.startswith("#") and "==" in line:
            requirement = Requirement(line.rstrip("\\").strip())
            specifier = str(requirement.specifier)
            assert specifier.startswith("=="), f"non-exact export entry: {line}"
            current = (canonicalize_name(requirement.name), specifier[2:])
            packages[current] = set()
        elif current is not None and "--hash=sha256:" in stripped:
            digest = stripped.split("--hash=sha256:", 1)[1].rstrip("\\").strip()
            packages[current].add(digest)
    return packages


def _lock_hashes(package: dict[str, object]) -> set[str]:
    """Return every hash recorded by uv for a locked package."""
    hashes: set[str] = set()
    sdist = package.get("sdist")
    if isinstance(sdist, dict) and isinstance(sdist.get("hash"), str):
        hashes.add(str(sdist["hash"]).split(":", 1)[-1])
    for wheel in package.get("wheels", []):
        if isinstance(wheel, dict) and isinstance(wheel.get("hash"), str):
            hashes.add(str(wheel["hash"]).split(":", 1)[-1])
    return hashes


def test_locked_exports_have_uv_hash_provenance() -> None:
    """Require every universal runtime/dev export hash to originate in uv.lock."""
    locked = _locked_packages()
    for export_name in ("requirements.txt", "requirements-dev.txt"):
        exported = _exported_requirements(RUNTIME_ROOT / export_name)
        assert exported, f"{export_name} did not contain pinned requirements"
        assert {("cffi", "2.1.1"), ("cryptography", "50.0.0")} <= set(exported)
        for key, hashes in exported.items():
            package = locked.get(key)
            assert package is not None, f"{export_name} entry is absent from uv.lock: {key}"
            assert hashes, f"{export_name} entry has no hashes: {key}"
            assert hashes <= _lock_hashes(package), f"{export_name} hash provenance drift: {key}"


def test_arm_packaging_lock_is_generated_from_only_compatible_wheels() -> None:
    """Bind the formal ARM export to exact CPython 3.13 macOS wheel hashes."""
    rendered = LOCK_GENERATOR.render_lock(
        RUNTIME_ROOT / "requirements.txt",
        RUNTIME_ROOT / "uv.lock",
        "aarch64-apple-darwin",
    )
    assert TARGET_LOCK.read_text(encoding="utf-8") == rendered

    exported = _exported_requirements(TARGET_LOCK)
    assert exported[("cffi", "2.1.1")] == {
        "19ee6127ee34de7d83ce3d371ebc5ed91addbdcc39f9ab15ce4eb35a4e534971"
    }
    assert exported[("cryptography", "50.0.0")] == {
        "031e2d5dd4bb9caa3ca9c82e5a197fd8ae680232cee62603d1a813f3f07e3d03",
        "ccdc4a71a4dabae05de219404f9f4abc38e3b58422177ff93d0da05967dafa07",
    }
    locked = _locked_packages()
    for key, hashes in exported.items():
        compatible = set(LOCK_GENERATOR.compatible_wheel_hashes(locked[key], "arm64"))
        assert hashes == compatible, f"non-ARM or missing wheel hash for {key}"


def test_intel_packaging_has_no_nonvulnerable_cryptography_wheel() -> None:
    """Keep Intel publication fail-closed until a fixed x86_64 wheel exists."""
    cryptography = _locked_packages()[("cryptography", "50.0.0")]
    assert LOCK_GENERATOR.compatible_wheel_hashes(cryptography, "x86_64") == ()

    with pytest.raises(
        LOCK_GENERATOR.LockGenerationError,
        match="unsupported formal packaging target",
    ):
        LOCK_GENERATOR.render_lock(
            RUNTIME_ROOT / "requirements.txt",
            RUNTIME_ROOT / "uv.lock",
            "x86_64-apple-darwin",
        )

    for workflow_name in ("desktop-installers.yml", "release.yml"):
        workflow = (
            REPOSITORY_ROOT / ".github/workflows" / workflow_name
        ).read_text(encoding="utf-8")
        assert "target: aarch64-apple-darwin" in workflow
        assert "target: x86_64-apple-darwin" not in workflow


def test_target_lock_tamper_is_rejected(tmp_path: Path) -> None:
    """A changed target export cannot pass the deterministic generation check."""
    tampered = tmp_path / TARGET_LOCK.name
    source = TARGET_LOCK.read_text(encoding="utf-8")
    tampered.write_text(source.replace("031e2d5d", "f31e2d5d", 1), encoding="utf-8")
    with pytest.raises(
        LOCK_GENERATOR.LockGenerationError,
        match="dependency lock is stale",
    ):
        LOCK_GENERATOR.verify_lock(
            tampered,
            RUNTIME_ROOT / "requirements.txt",
            RUNTIME_ROOT / "uv.lock",
            "aarch64-apple-darwin",
        )
