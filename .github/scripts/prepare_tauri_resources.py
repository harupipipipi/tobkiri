#!/usr/bin/env python3
"""Prepare bundled Tobkiri runtime resources for the Tauri desktop app."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tempfile
import urllib.request
import zipfile
from collections.abc import Mapping
from pathlib import Path

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parents[1]
_CLEANUP_HELPER = REPOSITORY_ROOT / "tobkiri_runtime/scripts/packaging_cleanup.py"
_CLEANUP_SPEC = importlib.util.spec_from_file_location(
    "tobkiri_packaging_cleanup",
    _CLEANUP_HELPER,
)
if _CLEANUP_SPEC is None or _CLEANUP_SPEC.loader is None:
    raise RuntimeError(f"packaging cleanup helper is unavailable: {_CLEANUP_HELPER}")
_CLEANUP_MODULE = importlib.util.module_from_spec(_CLEANUP_SPEC)
sys.modules[_CLEANUP_SPEC.name] = _CLEANUP_MODULE
_CLEANUP_SPEC.loader.exec_module(_CLEANUP_MODULE)
remove_owned_path = _CLEANUP_MODULE.remove_owned_path


APP_SOURCE_DIR = "tobkiri_runtime"
APP_RESOURCE_DIR = "tobkiri_launcher/src-tauri/gen/app"
APP_RESOURCE_OWNER_DIR = "tobkiri_launcher/src-tauri/gen"

EXCLUDED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".rumi_snapshots",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
    "user_data",
    "userdata",
    "venv",
    "virtualenv",
}
EXCLUDED_SUFFIXES = {
    ".bak",
    ".pyc",
    ".pyo",
    ".zip",
}
EXCLUDED_TOP_LEVEL_DIRS = {
    "tests",
}
LEGACY_AUTHORITY_FILENAMES = frozenset(
    {
        "ecosystem.json",
        "rumi.pack.v3.json",
    }
)
CANONICAL_DEFAULTSPACK_FILES = (
    Path("ecosystem/defaultspack/pack.v4.json"),
    Path("ecosystem/defaultspack/contracts.v4.json"),
    Path("ecosystem/defaultspack/artifact-index.v4.json"),
    Path("ecosystem/defaultspack/executables.v4.json"),
    Path("ecosystem/defaultspack/v4/bundle.lock.json"),
    Path("ecosystem/defaultspack/v4/defaults.profile.v4.json"),
)
GENERATED_RESOURCE_DIRS = (
    "core_runtime/core_pack/core_control_panel/web",
    "ecosystem/defaultspack/ui",
    "bundled",
    "python-runtime",
)
SEALED_PYTHON_RESOURCE_DIR = "python-runtime"
SEALED_PYTHON_MANIFEST = (
    f"{SEALED_PYTHON_RESOURCE_DIR}/sealed-environment.v1.json"
)
PACKAGING_PYTHON_SNAPSHOT_ENV = "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT"
PACKAGING_PYTHON_INVENTORY_SHA_ENV = "TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256"
SEALED_PYTHON_BUILDER = (
    Path(".github") / "scripts" / "build_sealed_python_environment.py"
)
RUNTIME_RESOURCE_MANIFEST = "runtime-resource-manifest.v1.json"
RUNTIME_RESOURCE_SCHEMA = "io.tobkiri.runtime-resource-manifest.v1"
CARGO_TARGET_DIR_ENV = "CARGO_TARGET_DIR"
REQUIRED_RUNTIME_BOOTSTRAP_FILES = (
    Path("app.py"),
    Path("core_runtime/__init__.py"),
    Path("core_runtime/bootstrap/__init__.py"),
    Path("core_runtime/bootstrap/runtime.py"),
    Path("core_runtime/app_lifecycle_manager.py"),
    Path("core_runtime/pack_api_server.py"),
)
SEALED_ROLE_TARGETS = (
    Path("app.py"),
    Path("ecosystem/defaultspack/defaultspack/desktop_app.py"),
    Path("core_runtime/host_broker/computer_host_helper.py"),
)
CANONICAL_HOST_INVENTORY = Path("tobkiri_host/canonical-files.v1.json")
CANONICAL_HOST_INVENTORY_SCHEMA = "io.tobkiri.host-file-inventory.v1"
UV_PINNED_VERSION = "0.11.14"
UV_SHA256_BY_TARGET = {
    "aarch64-apple-darwin": "4333af5c0730d94323a7819bbdf87ce92dd07fc857d67fff0059e0fca31b5c02",
    "x86_64-apple-darwin": "9836c1440b0bd6aa5f81793648a339bd01d593b7b8f575de3b855dae4ab64654",
    "x86_64-pc-windows-msvc": "52ba5d19409aaa688a8a1a6ec8dfb6a4817230d20186e75f4006105c3e39a846",
    "x86_64-unknown-linux-gnu": "f3b623eb0e6141a7053d571d59a0bdc341e0f238ea8f5f0b4815ddbec9a2a296",
}
UV_BINARY_SHA256_BY_TARGET = {
    "aarch64-apple-darwin": "77b80ca26ad2142c50b870c730d9b8f617665720f09888630257b40d0678e658",
    "x86_64-apple-darwin": "1bb756786175621eea70219911d02bf8d3e32203bb5a7a19b345e44d031f436e",
    "x86_64-pc-windows-msvc": "442b73298cf8648217e5bc232588bb1067f98ea5b40beea18e43c9c7929c020c",
    "x86_64-unknown-linux-gnu": "b5cbc3a3f35debad0b4770811efd190bcf460b654114d6a3f71e0ce298468e5d",
}


class _ValidatedSealedPythonBoundary:
    """Evidence binding the generic bundle scan to one formal Python tree."""

    __slots__ = (
        "bundle_root",
        "root",
        "root_identity",
        "manifest_identity",
        "manifest_digest",
        "tree_identity",
    )

    def __init__(
        self,
        *,
        bundle_root: Path,
        root: Path,
        root_identity: tuple[int, int],
        manifest_identity: tuple[int, int],
        manifest_digest: str,
        tree_identity: tuple[tuple[str, int, int, int, int, int], ...],
    ) -> None:
        self.bundle_root = bundle_root
        self.root = root
        self.root_identity = root_identity
        self.manifest_identity = manifest_identity
        self.manifest_digest = manifest_digest
        self.tree_identity = tree_identity

    def contains_descendant(self, path: Path) -> bool:
        """Return whether ``path`` is below this exact validated subtree."""
        if path == self.root:
            return False
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    def assert_unchanged(self) -> None:
        """Reject replacement or manifest mutation after formal validation."""
        expected_root = self.bundle_root / SEALED_PYTHON_RESOURCE_DIR
        if self.root != expected_root:
            raise RuntimeError("formal sealed Python bundle boundary is invalid")
        try:
            root_metadata = self.root.lstat()
        except OSError as exc:
            raise RuntimeError("formal sealed Python bundle boundary is unavailable") from exc
        if (
            self.root.is_symlink()
            or getattr(root_metadata, "st_file_attributes", 0) & 0x0400
            or not stat.S_ISDIR(root_metadata.st_mode)
            or (root_metadata.st_dev, root_metadata.st_ino) != self.root_identity
        ):
            raise RuntimeError("formal sealed Python bundle boundary changed")

        manifest = self.root / Path(SEALED_PYTHON_MANIFEST).name
        try:
            manifest_metadata = manifest.lstat()
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError("formal sealed Python manifest is unavailable") from exc
        if (
            manifest.is_symlink()
            or getattr(manifest_metadata, "st_file_attributes", 0) & 0x0400
            or not stat.S_ISREG(manifest_metadata.st_mode)
            or manifest_metadata.st_nlink != 1
            or (manifest_metadata.st_dev, manifest_metadata.st_ino)
            != self.manifest_identity
            or manifest_digest != self.manifest_digest
        ):
            raise RuntimeError("formal sealed Python manifest changed")
        if _sealed_tree_identity(self.root) != self.tree_identity:
            raise RuntimeError("formal sealed Python tree identity changed")


def _sealed_tree_identity(
    root: Path,
) -> tuple[tuple[str, int, int, int, int, int], ...]:
    """Bind every sealed entry identity and exact mode without following links."""
    entries = []
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x0400:
            raise RuntimeError("formal sealed Python tree contains a link")
        relative = "." if path == root else path.relative_to(root).as_posix()
        entries.append(
            (
                relative,
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_nlink,
                metadata.st_size,
            )
        )
    return tuple(entries)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root. Defaults to this script's repository.",
    )
    parser.add_argument(
        "--target",
        help="Rust/Tauri target triple. When set, stages pack-shell for that target.",
    )
    parser.add_argument(
        "--uv-version",
        help="uv release version to bundle, for example 0.11.14.",
    )
    parser.add_argument(
        "--stage-uv-only",
        action="store_true",
        help="Stage only the pinned uv resource and exit before other mutations.",
    )
    parser.add_argument(
        "--uv-output-root",
        type=Path,
        help="Private absolute root used only with --stage-uv-only.",
    )
    parser.add_argument(
        "--require-runtime-tools",
        action="store_true",
        help="Fail unless bundled uv and pack-shell are present.",
    )
    return parser.parse_args()


def path_parts(rel: str) -> list[str]:
    return [part for part in rel.replace("\\", "/").split("/") if part]


def should_skip_source_rel(rel_under_app: str) -> bool:
    parts = path_parts(rel_under_app)
    if not parts:
        return True
    if parts[0] in EXCLUDED_TOP_LEVEL_DIRS:
        return True
    if any(part in EXCLUDED_DIR_NAMES for part in parts):
        return True
    path = Path(rel_under_app)
    if path.name == ".DS_Store":
        return True
    if path.name in LEGACY_AUTHORITY_FILENAMES:
        return True
    return any(path.name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def run_git_ls_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", APP_SOURCE_DIR],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    mode = src.stat().st_mode
    if mode & stat.S_IXUSR:
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_tracked_runtime_files(repo_root: Path, source_root: Path, dest_root: Path) -> int:
    copied = 0
    source_prefix = f"{APP_SOURCE_DIR}/"
    for rel in run_git_ls_files(repo_root):
        if not rel.startswith(source_prefix):
            continue
        rel_under_app = rel[len(source_prefix) :]
        if should_skip_source_rel(rel_under_app):
            continue
        src = repo_root / rel
        if src.is_symlink():
            raise RuntimeError(f"Refusing symlinked runtime source: {rel}")
        if not src.is_file():
            continue
        copy_file(src, dest_root / rel_under_app)
        copied += 1
    return copied


def canonical_host_files(source_root: Path) -> tuple[Path, ...]:
    """Load and strictly validate the authoritative closed Host inventory."""

    inventory = source_root / CANONICAL_HOST_INVENTORY
    if inventory.is_symlink() or not inventory.is_file():
        raise FileNotFoundError(
            f"Canonical Host inventory is missing or unsafe: {inventory}"
        )
    try:
        document = json.loads(inventory.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Canonical Host inventory is malformed: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"schema", "files"}:
        raise RuntimeError("Canonical Host inventory has an invalid document shape")
    if document["schema"] != CANONICAL_HOST_INVENTORY_SCHEMA:
        raise RuntimeError("Canonical Host inventory schema is unsupported")
    raw_files = document["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeError("Canonical Host inventory files must be a non-empty list")
    if any(not isinstance(filename, str) for filename in raw_files):
        raise RuntimeError("Canonical Host inventory filenames must be strings")
    if raw_files != sorted(set(raw_files)):
        raise RuntimeError("Canonical Host inventory must be sorted and unique")
    inventory_name = CANONICAL_HOST_INVENTORY.name
    if inventory_name not in raw_files:
        raise RuntimeError("Canonical Host inventory must include itself")
    for filename in raw_files:
        if Path(filename).parts != (filename,) or filename in {"", ".", ".."}:
            raise RuntimeError(
                f"Canonical Host inventory filename is unsafe: {filename!r}"
            )
    host_root = source_root / "tobkiri_host"
    actual_source_files = set()
    for source in host_root.iterdir():
        if source.is_symlink():
            raise RuntimeError(f"Canonical Host source is a symlink: {source}")
        if source.is_file():
            actual_source_files.add(source.name)
    if actual_source_files != set(raw_files):
        raise RuntimeError(
            "Canonical Host source inventory mismatch: "
            f"missing={sorted(set(raw_files) - actual_source_files)}, "
            f"unlisted={sorted(actual_source_files - set(raw_files))}"
        )
    return tuple(Path("tobkiri_host") / filename for filename in raw_files)


def stage_canonical_host_package(source_root: Path, dest_root: Path) -> None:
    """Stage the closed canonical Host package from regular source files."""
    host_root = dest_root / "tobkiri_host"
    if host_root.exists():
        if host_root.is_symlink() or not host_root.is_dir():
            raise RuntimeError("Refusing unsafe staged tobkiri_host package")
        remove_owned_path(
            host_root,
            owner_root=dest_root,
            operation="remove staged canonical Host package",
        )
    for relative in canonical_host_files(source_root):
        source = source_root / relative
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(
                f"Canonical Host source is missing or unsafe: {source}"
            )
        copy_file(source, dest_root / relative)


def verify_canonical_host_package(
    dest_root: Path,
    repository_root: Path,
) -> None:
    """Require the staged Host inventory and bytes to equal canonical source."""
    source_root = repository_root / APP_SOURCE_DIR
    canonical_files = canonical_host_files(source_root)
    expected = {path.as_posix() for path in canonical_files}
    host_root = dest_root / "tobkiri_host"
    if host_root.is_symlink() or not host_root.is_dir():
        raise FileNotFoundError("Staged canonical Host package is missing or unsafe")
    actual = set()
    for staged in host_root.rglob("*"):
        relative = staged.relative_to(dest_root)
        if staged.is_symlink():
            raise RuntimeError(f"Staged canonical Host resource is a symlink: {relative}")
        if staged.is_file():
            actual.add(relative.as_posix())
    if actual != expected:
        missing = sorted(expected - actual)
        unlisted = sorted(actual - expected)
        raise RuntimeError(
            "Staged canonical Host inventory mismatch: "
            f"missing={missing[:20]}, unlisted={unlisted[:20]}"
        )
    for relative in canonical_files:
        source = source_root / relative
        staged = dest_root / relative
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(
                f"Canonical Host source is missing or unsafe: {source}"
            )
        if compute_sha256(staged) != compute_sha256(source):
            raise RuntimeError(
                f"Staged canonical Host resource hash mismatch: {relative}"
            )


def verify_sealed_role_closure(
    dest_root: Path,
    repository_root: Path,
) -> None:
    """Require direct role targets in both staged app roots."""
    source_root = repository_root / APP_SOURCE_DIR
    for relative in SEALED_ROLE_TARGETS:
        source = source_root / relative
        staged_paths = [(dest_root / relative, "staged")]
        sealed_root = dest_root / SEALED_PYTHON_RESOURCE_DIR
        if sealed_root.is_dir():
            staged_paths.append((sealed_root / "app" / relative, "sealed"))
        for path, label in ((source, "source"), *staged_paths):
            if path.is_symlink() or not path.is_file():
                raise FileNotFoundError(
                    f"Sealed role {label} target is missing or unsafe: {path}"
                )
        if compute_sha256(source) != compute_sha256(path):
            raise RuntimeError(f"Sealed role target hash mismatch: {relative}")


def copy_generated_resource_dirs(
    source_root: Path,
    dest_root: Path,
    *,
    sealed_python_source: Path | None = None,
) -> int:
    copied = 0
    for rel_dir in GENERATED_RESOURCE_DIRS:
        src_dir = (
            sealed_python_source
            if rel_dir == SEALED_PYTHON_RESOURCE_DIR and sealed_python_source is not None
            else source_root / rel_dir
        )
        if not src_dir.exists():
            continue
        if src_dir.is_symlink():
            raise RuntimeError(f"Refusing symlinked generated resource directory: {rel_dir}")
        if rel_dir == SEALED_PYTHON_RESOURCE_DIR:
            for src in src_dir.rglob("*"):
                if src.is_symlink():
                    raise RuntimeError(
                        "Refusing symlinked sealed Python resource: "
                        f"{Path(rel_dir) / src.relative_to(src_dir)}"
                    )
                if src.is_dir():
                    continue
                if not src.is_file():
                    raise RuntimeError(
                        "Refusing special sealed Python resource: "
                        f"{Path(rel_dir) / src.relative_to(src_dir)}"
                    )
                rel_under_app = (
                    Path(rel_dir) / src.relative_to(src_dir)
                ).as_posix()
                copy_file(src, dest_root / rel_under_app)
                copied += 1
            destination_dir = dest_root / rel_dir
            destination_dir.mkdir(parents=True, exist_ok=True)
            for src in sorted(
                (src_dir, *src_dir.rglob("*")),
                key=lambda item: len(item.relative_to(src_dir).parts),
                reverse=True,
            ):
                if src.is_symlink():
                    raise RuntimeError(
                        "Refusing symlinked sealed Python resource: "
                        f"{src.relative_to(src_dir).as_posix()}"
                    )
                if src.is_dir():
                    destination = destination_dir / src.relative_to(src_dir)
                    destination.chmod(stat.S_IMODE(src.stat().st_mode))
            continue
        for src in src_dir.rglob("*"):
            if src.is_symlink():
                raise RuntimeError(
                    "Refusing symlinked generated resource: "
                    f"{src.relative_to(source_root).as_posix()}"
                )
            if not src.is_file():
                continue
            rel_under_app = src.relative_to(source_root).as_posix()
            if should_skip_source_rel(rel_under_app):
                continue
            copy_file(src, dest_root / rel_under_app)
            copied += 1
    return copied


def _load_sealed_python_builder(repository_root: Path):
    """Load the sealed Python builder without making it a runtime dependency."""
    path = repository_root / SEALED_PYTHON_BUILDER
    spec = importlib.util.spec_from_file_location(
        "tobkiri_sealed_python_builder",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load sealed Python builder: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _formal_sealed_python_snapshot() -> tuple[Path, str]:
    """Return the rootless sealed Python snapshot and its manifest binding."""
    configured = os.environ.get(PACKAGING_PYTHON_SNAPSHOT_ENV)
    expected_digest = os.environ.get(PACKAGING_PYTHON_INVENTORY_SHA_ENV)
    if not configured or not expected_digest:
        raise RuntimeError(
            "formal sealed Python snapshot and manifest binding are required"
        )
    snapshot = Path(configured)
    if not snapshot.is_absolute():
        raise RuntimeError("formal sealed Python snapshot path must be absolute")
    try:
        resolved = snapshot.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("formal sealed Python snapshot is unavailable") from exc
    if snapshot.is_symlink() or resolved != snapshot or not snapshot.is_dir():
        raise RuntimeError("formal sealed Python snapshot path is not canonical")
    if len(expected_digest) != 64 or any(
        character not in "0123456789abcdef" for character in expected_digest
    ):
        raise RuntimeError("formal sealed Python manifest binding is invalid")
    return snapshot, expected_digest


def build_sealed_python_resource(
    repository_root: Path,
    source_root: Path,
    target: str,
) -> Path:
    """Validate the producer-owned sealed Python tree for Tauri staging."""
    builder = _load_sealed_python_builder(repository_root)
    snapshot, expected_digest = _formal_sealed_python_snapshot()
    digest = builder.validate_environment(
        snapshot,
        target,
        expected_manifest_digest=expected_digest,
        run_native_smoke=True,
    )
    print(f"Using formal sealed Python environment ({digest})")
    return snapshot


def validate_sealed_python_resource(
    dest_root: Path,
    target: str,
    repository_root: Path,
) -> _ValidatedSealedPythonBoundary:
    """Validate and bind the formal sealed Python subtree in ``gen/app``."""
    builder = _load_sealed_python_builder(repository_root)
    sealed_root = dest_root / SEALED_PYTHON_RESOURCE_DIR
    manifest = sealed_root / Path(SEALED_PYTHON_MANIFEST).name
    manifest_digest = builder.validate_environment(
        sealed_root,
        target,
        run_native_smoke=False,
    )
    if (
        not isinstance(manifest_digest, str)
        or len(manifest_digest) != 64
        or any(character not in "0123456789abcdef" for character in manifest_digest)
    ):
        raise RuntimeError("formal sealed Python validator returned an invalid identity")

    try:
        root_metadata = sealed_root.lstat()
        manifest_metadata = manifest.lstat()
    except OSError as exc:
        raise RuntimeError("formal sealed Python bundle boundary is unavailable") from exc
    if (
        sealed_root.is_symlink()
        or getattr(root_metadata, "st_file_attributes", 0) & 0x0400
        or not stat.S_ISDIR(root_metadata.st_mode)
        or manifest.is_symlink()
        or getattr(manifest_metadata, "st_file_attributes", 0) & 0x0400
        or not stat.S_ISREG(manifest_metadata.st_mode)
        or manifest_metadata.st_nlink != 1
    ):
        raise RuntimeError("formal sealed Python bundle boundary is unsafe")
    boundary = _ValidatedSealedPythonBoundary(
        bundle_root=dest_root,
        root=sealed_root,
        root_identity=(root_metadata.st_dev, root_metadata.st_ino),
        manifest_identity=(manifest_metadata.st_dev, manifest_metadata.st_ino),
        manifest_digest=manifest_digest,
        tree_identity=_sealed_tree_identity(sealed_root),
    )
    boundary.assert_unchanged()
    return boundary


def _validate_generated_bundle_directories(
    dest_root: Path,
    *,
    validated_sealed_python: _ValidatedSealedPythonBoundary | None,
) -> None:
    """Reject generated directories outside formally validated resource domains.

    The generic name policy has no allowlist for Python environments.  It may
    traverse the exact sealed Python subtree only after the producer-owned
    manifest, inventory, digest, provenance, and link-free tree have been
    validated by ``build_sealed_python_environment.py``.  The boundary is
    checked before and after traversal so a replacement cannot turn that
    evidence into a path/name exemption.
    """
    if validated_sealed_python is not None:
        validated_sealed_python.assert_unchanged()

    forbidden: list[str] = []
    for path in dest_root.rglob("*"):
        if (
            validated_sealed_python is not None
            and validated_sealed_python.contains_descendant(path)
        ):
            continue
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeError(
                "generated bundle changed during directory validation"
            ) from exc
        if stat.S_ISDIR(metadata.st_mode) and path.name in EXCLUDED_DIR_NAMES:
            forbidden.append(str(path.relative_to(dest_root)))
    if validated_sealed_python is not None:
        validated_sealed_python.assert_unchanged()
    if forbidden:
        raise RuntimeError(
            "Forbidden generated bundle directories: " + ", ".join(forbidden[:20])
        )


def _resource_files(dest_root: Path) -> list[Path]:
    """Return the exact regular-file inventory used by the resource seal."""
    verify_no_python_bytecode(dest_root)
    files: list[Path] = []
    ambiguity_keys: set[str] = set()
    for path in dest_root.rglob("*"):
        relative = path.relative_to(dest_root).as_posix()
        if path.is_symlink():
            raise RuntimeError(
                "Staged resource contains symlink: "
                f"{relative}"
            )
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"Staged resource contains special file: {relative}")
        if path.name == RUNTIME_RESOURCE_MANIFEST:
            continue
        canonical = canonical_runtime_resource_path(relative)
        assert_runtime_resource_path_unambiguous(canonical, ambiguity_keys)
        if metadata.st_nlink != 1:
            raise RuntimeError(f"Staged runtime resource is hardlinked: {canonical}")
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(dest_root).as_posix())


def canonical_runtime_resource_path(value: str) -> str:
    """Validate one printable-ASCII path relative to ``Resources/app``."""
    parts = value.split("/")
    if (
        not value
        or not value.isascii()
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
        or any(
            not part or part in {".", ".."} or ":" in part
            for part in parts
        )
    ):
        raise RuntimeError(
            f"Runtime resource path is not a canonical portable relative: {value!r}"
        )
    return value


def assert_runtime_resource_path_unambiguous(
    value: str,
    ambiguity_keys: set[str],
) -> None:
    """Reject a second portable path with the same ASCII-case identity."""
    ambiguity_key = value.lower()
    if ambiguity_key in ambiguity_keys:
        raise RuntimeError(
            "Staged runtime resource paths are ambiguous by ASCII case"
        )
    ambiguity_keys.add(ambiguity_key)


def sealed_application_resource_paths(sealed_path: str) -> tuple[str, str]:
    """Map sealed ``app/X`` to its sole outer and application path domains."""
    sealed = canonical_runtime_resource_path(sealed_path)
    if not sealed.startswith("app/"):
        raise RuntimeError("Sealed application path is outside the exact app domain")
    application = canonical_runtime_resource_path(sealed.removeprefix("app/"))
    outer = canonical_runtime_resource_path(
        f"{SEALED_PYTHON_RESOURCE_DIR}/{sealed}"
    )
    return outer, application


def verify_no_python_bytecode(dest_root: Path) -> None:
    """Fail closed if a staged runtime contains generated Python bytecode."""
    forbidden = []
    for path in dest_root.rglob("*"):
        if (path.is_dir() and path.name == "__pycache__") or (
            path.is_file() and path.suffix in {".pyc", ".pyo"}
        ):
            forbidden.append(path.relative_to(dest_root).as_posix())
    if forbidden:
        raise RuntimeError(
            "Staged runtime contains generated Python bytecode: "
            + ", ".join(sorted(forbidden)[:20])
        )


def build_runtime_resource_manifest(dest_root: Path) -> dict[str, object]:
    """Build a deterministic manifest over every staged runtime resource."""
    entries = []
    for path in _resource_files(dest_root):
        payload = path.read_bytes()
        relative = canonical_runtime_resource_path(
            path.relative_to(dest_root).as_posix()
        )
        sealed_prefix = f"{SEALED_PYTHON_RESOURCE_DIR}/app/"
        if relative.startswith(sealed_prefix):
            outer, _application = sealed_application_resource_paths(
                relative.removeprefix(f"{SEALED_PYTHON_RESOURCE_DIR}/")
            )
            if outer != relative:
                raise RuntimeError("Sealed application resource domain is inconsistent")
        entries.append(
            {
                "path": relative,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {"schema": RUNTIME_RESOURCE_SCHEMA, "entries": entries}


def write_runtime_resource_manifest(dest_root: Path) -> Path:
    """Write the deterministic runtime resource manifest."""
    manifest_path = dest_root / RUNTIME_RESOURCE_MANIFEST
    manifest_path.write_text(
        json.dumps(
            build_runtime_resource_manifest(dest_root),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def verify_runtime_resource_manifest(dest_root: Path) -> dict[str, object]:
    """Fail closed when staged bytes differ from the resource manifest."""
    manifest_path = dest_root / RUNTIME_RESOURCE_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FileNotFoundError(
            f"Runtime resource manifest is missing or unsafe: {manifest_path}"
        )
    if manifest_path.stat(follow_symlinks=False).st_nlink != 1:
        raise RuntimeError("Runtime resource manifest is hardlinked")
    try:
        actual = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Runtime resource manifest is malformed: {exc}") from exc
    if actual != build_runtime_resource_manifest(dest_root):
        raise RuntimeError("Runtime resource manifest does not match staged bytes")
    return actual


def runtime_resource_expected_tree(
    manifest: Mapping[str, object],
) -> dict[str, bool]:
    """Convert a verified runtime manifest into an exact cleanup inventory."""
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("Runtime resource manifest entries are invalid")
    expected = {RUNTIME_RESOURCE_MANIFEST: False}
    ambiguity_keys = {RUNTIME_RESOURCE_MANIFEST.lower()}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RuntimeError("Runtime resource manifest entry is invalid")
        relative = entry.get("path")
        if not isinstance(relative, str):
            raise RuntimeError("Runtime resource manifest path is invalid")
        relative = canonical_runtime_resource_path(relative)
        ambiguity_key = relative.lower()
        if ambiguity_key in ambiguity_keys:
            raise RuntimeError(
                "Runtime resource manifest paths are ambiguous by ASCII case"
            )
        ambiguity_keys.add(ambiguity_key)
        if relative in expected:
            raise RuntimeError(
                "Runtime resource manifest contains a duplicate path"
            )
        expected[relative] = False
    return expected


def verify_staged_bootstrap_import(dest_root: Path) -> None:
    """Import bootstrap and Host SDK surfaces using only the staged tree."""
    code = (
        "import pathlib,sys; "
        f"root=pathlib.Path({str(dest_root.resolve())!r}); "
        "sys.path.insert(0,str(root)); "
        "from core_runtime import Kernel; "
        "from tobkiri_host.extension_sdk import HostExtensionSDK; "
        "from tobkiri_host.platform_backends import MacOSVZBackend; "
        "from tobkiri_host.tauri_roles import validate_production_tauri_roles; "
        "objects=(Kernel,HostExtensionSDK,MacOSVZBackend,"
        "validate_production_tauri_roles); "
        "[pathlib.Path(sys.modules[obj.__module__].__file__).resolve()"
        ".relative_to(root.resolve()) for obj in objects]"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=dest_root.parent,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Staged core_runtime bootstrap import failed: {detail}")


def is_windows_target(target: str) -> bool:
    return "windows" in target or target.endswith("-msvc")


def pack_shell_binary_name(target: str) -> str:
    return "pack-shell.exe" if is_windows_target(target) else "pack-shell"


def uv_binary_name(target: str) -> str:
    return "uv.exe" if is_windows_target(target) else "uv"


def resolve_cargo_target_dir(
    repo_root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve Cargo's target directory for the repository build.

    Cargo resolves a relative ``CARGO_TARGET_DIR`` from the command's working
    directory. The release wrapper runs Cargo from ``repo_root``, so resolving
    relative values here keeps staging tied to the exact build output without
    searching other directories.
    """
    repository_root = repo_root.resolve(strict=True)
    if not repository_root.is_dir():
        raise RuntimeError(f"repository root must be a directory: {repository_root}")
    environment = os.environ if environ is None else environ
    configured = environment.get(CARGO_TARGET_DIR_ENV)
    if configured:
        target_dir = Path(configured)
        if ".." in target_dir.parts:
            raise ValueError(
                f"{CARGO_TARGET_DIR_ENV} may not contain parent traversal: {configured!r}"
            )
        if not target_dir.is_absolute():
            target_dir = repository_root / target_dir
    else:
        target_dir = repository_root / "pack-shell" / "target"
    target_dir = Path(os.path.normpath(target_dir))

    components = (target_dir, *target_dir.parents)
    for component in reversed(components):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(
                f"Cargo target directory contains a symlink component: {target_dir}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(
                f"Cargo target directory has a non-directory component: {component}"
            )
    return target_dir


def _validate_path_component(value: str, *, label: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"invalid {label} path component: {value!r}")


def _validate_target_component(target: str) -> None:
    _validate_path_component(target, label="Rust target")


def _validate_profile_component(profile: str) -> None:
    _validate_path_component(profile, label="Cargo profile")


def resolve_pack_shell_binary(
    repo_root: Path,
    target: str,
    environ: Mapping[str, str] | None = None,
    *,
    profile: str = "release",
) -> Path:
    """Resolve the exact, canonical pack-shell binary for one Cargo profile.

    The target triple and profile are validated as single path components. The
    target-dir environment variable selects only Cargo's output root; it does
    not authorize fallback searches or path traversal.
    """
    _validate_target_component(target)
    _validate_profile_component(profile)
    binary_name = pack_shell_binary_name(target)
    target_dir = resolve_cargo_target_dir(repo_root, environ)
    candidate = target_dir / target / profile / binary_name
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"pack-shell binary not found at {candidate}. "
            "Build pack-shell before preparing resources."
        ) from exc
    if candidate.is_symlink():
        raise RuntimeError(f"pack-shell binary may not be a symlink: {candidate}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"pack-shell binary must be a regular file: {candidate}")
    canonical = candidate.resolve(strict=True)
    if canonical != candidate:
        raise RuntimeError(
            f"pack-shell binary path is not canonical or contains a symlink: {candidate}"
        )
    if os.name != "nt" and not metadata.st_mode & (
        stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    ):
        raise RuntimeError(f"pack-shell binary must be executable: {candidate}")
    return candidate


def _expected_pack_shell_architecture(target: str) -> str:
    architecture = target.split("-", 1)[0]
    aliases = {"amd64": "x86_64", "arm64": "aarch64", "i586": "x86", "i686": "x86"}
    return aliases.get(architecture, architecture)


def _pack_shell_binary_architecture(payload: bytes, target: str) -> str:
    """Read the architecture from the target platform's executable header."""
    if is_windows_target(target):
        if len(payload) < 64 or payload[:2] != b"MZ":
            raise RuntimeError("pack-shell is not a PE executable")
        pe_offset = int.from_bytes(payload[60:64], "little")
        if (
            len(payload) < pe_offset + 6
            or payload[pe_offset : pe_offset + 4] != b"PE\0\0"
        ):
            raise RuntimeError("pack-shell has an invalid PE header")
        machine = int.from_bytes(payload[pe_offset + 4 : pe_offset + 6], "little")
        return {0x014C: "x86", 0x8664: "x86_64", 0xAA64: "aarch64"}.get(
            machine, f"pe-machine-{machine:#x}"
        )

    if "apple-darwin" in target:
        if len(payload) < 8:
            raise RuntimeError("pack-shell has a truncated Mach-O header")
        magic = payload[:4]
        if magic in {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"}:
            cpu_type = int.from_bytes(payload[4:8], "little")
        elif magic in {b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}:
            cpu_type = int.from_bytes(payload[4:8], "big")
        else:
            raise RuntimeError("pack-shell is not a thin Mach-O executable")
        return {7: "x86", 0x01000007: "x86_64", 0x0100000C: "aarch64"}.get(
            cpu_type, f"macho-cpu-{cpu_type:#x}"
        )

    if len(payload) < 20 or payload[:4] != b"\x7fELF":
        raise RuntimeError("pack-shell is not an ELF executable")
    if payload[5:6] == b"\x01":
        byte_order = "little"
    elif payload[5:6] == b"\x02":
        byte_order = "big"
    else:
        raise RuntimeError("pack-shell ELF header has an invalid byte order")
    machine = int.from_bytes(payload[18:20], byte_order)
    return {3: "x86", 62: "x86_64", 183: "aarch64"}.get(
        machine, f"elf-machine-{machine:#x}"
    )


def _read_verified_pack_shell(path: Path, target: str) -> tuple[bytes, int]:
    """Read a regular executable once and detect path substitution around the read."""
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(f"pack-shell binary must be a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read()
    finally:
        os.close(descriptor)
    after = path.lstat()
    identity_before = (before.st_dev, before.st_ino, before.st_size)
    identity_opened = (opened.st_dev, opened.st_ino, opened.st_size)
    identity_after = (after.st_dev, after.st_ino, after.st_size)
    if identity_before != identity_opened or identity_opened != identity_after:
        raise RuntimeError(f"pack-shell binary changed while being staged: {path}")
    actual_architecture = _pack_shell_binary_architecture(payload, target)
    expected_architecture = _expected_pack_shell_architecture(target)
    if actual_architecture != expected_architecture:
        raise RuntimeError(
            "pack-shell architecture mismatch: "
            f"expected {expected_architecture}, got {actual_architecture}"
        )
    return payload, opened.st_mode


def pack_shell_digest_path(binary: Path) -> Path:
    """Return the canonical digest sidecar for a pack-shell artifact."""
    return binary.with_name(f"{binary.name}.sha256")


def _write_pack_shell_digest(binary: Path, payload: bytes) -> Path:
    """Atomically seal the exact validated pack-shell bytes for Cargo staging."""
    digest_path = pack_shell_digest_path(binary)
    if digest_path.exists() or digest_path.is_symlink():
        metadata = digest_path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                f"pack-shell digest destination is unsafe: {digest_path}"
            )
    digest_payload = f"{hashlib.sha256(payload).hexdigest()}\n".encode("ascii")
    temporary = digest_path.with_name(f".{digest_path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError(
            f"pack-shell digest temporary destination already exists: {temporary}"
        )
    try:
        with temporary.open("xb") as handle:
            handle.write(digest_payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, digest_path)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = digest_path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or digest_path.read_bytes() != digest_payload:
        digest_path.unlink(missing_ok=True)
        raise RuntimeError(f"pack-shell digest sealing failed: {digest_path}")
    return digest_path


def seal_pack_shell_binary(
    repo_root: Path,
    target: str,
    environ: Mapping[str, str] | None = None,
    *,
    profile: str = "release",
) -> tuple[Path, bytes, int, Path]:
    """Validate and seal the canonical target/profile pack-shell artifact."""
    binary = resolve_pack_shell_binary(
        repo_root,
        target,
        environ,
        profile=profile,
    )
    payload, source_mode = _read_verified_pack_shell(binary, target)
    digest_path = _write_pack_shell_digest(binary, payload)
    return binary, payload, source_mode, digest_path


def stage_pack_shell(repo_root: Path, source_root: Path, target: str) -> Path:
    _src, payload, source_mode, _digest_path = seal_pack_shell_binary(
        repo_root,
        target,
    )
    binary_name = pack_shell_binary_name(target)
    staged_root = source_root.resolve()
    bundled_dir = staged_root / "bundled"
    if bundled_dir.exists() and (bundled_dir.is_symlink() or not bundled_dir.is_dir()):
        raise RuntimeError(f"pack-shell staging directory is unsafe: {bundled_dir}")
    bundled_dir.mkdir(parents=True, exist_ok=True)
    dest = bundled_dir / binary_name
    if dest.is_symlink():
        raise RuntimeError(f"pack-shell staging destination may not be a symlink: {dest}")
    if dest.resolve(strict=False) != dest:
        raise RuntimeError(f"pack-shell staging destination is not canonical: {dest}")
    if dest.exists() and not dest.is_file():
        raise RuntimeError(f"pack-shell staging destination must be a regular file: {dest}")
    temporary = bundled_dir / f".{binary_name}.{os.getpid()}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError(
            f"pack-shell temporary destination already exists: {temporary}"
        )
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(stat.S_IMODE(source_mode))
        os.replace(temporary, dest)
    finally:
        temporary.unlink(missing_ok=True)
    staged_payload = dest.read_bytes()
    source_hash = hashlib.sha256(payload).digest()
    staged_hash = hashlib.sha256(staged_payload).digest()
    if staged_hash != source_hash:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"staged pack-shell SHA256 mismatch: {dest}")
    return dest


def download_to_temp(url: str, attempts: int = 15) -> Path:
    suffix = ".zip" if url.endswith(".zip") else ".tar.gz"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        fd, temp_name = tempfile.mkstemp(prefix="rumi-uv-", suffix=suffix)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status} for {url}")
                with temp_path.open("wb") as out:
                    shutil.copyfileobj(response, out)
            return temp_path
        except Exception as exc:  # pragma: no cover - network retry path
            temp_path.unlink(missing_ok=True)
            last_error = exc
            if attempt < attempts:
                print(f"Download failed for {url} (attempt {attempt}/{attempts}): {exc}", file=sys.stderr)
                time.sleep(min(30, 2 * attempt))
    assert last_error is not None
    raise last_error


def download_text(url: str, attempts: int = 5) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status} for {url}")
                return response.read().decode("utf-8")
        except Exception as exc:  # pragma: no cover - network retry path
            last_error = exc
            if attempt < attempts:
                print(f"Download failed for {url} (attempt {attempt}/{attempts}): {exc}", file=sys.stderr)
                time.sleep(min(30, 2 * attempt))
    assert last_error is not None
    raise last_error


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_uv_sha256(target: str, version: str) -> str:
    if version != UV_PINNED_VERSION:
        raise RuntimeError(
            "No pinned SHA256 is configured for uv version "
            f"{version}. Update UV_PINNED_VERSION/UV_SHA256_BY_TARGET before bundling."
        )
    try:
        return UV_SHA256_BY_TARGET[target]
    except KeyError as exc:
        raise RuntimeError(
            f"No pinned SHA256 is configured for uv target {target!r}. "
            "Update UV_SHA256_BY_TARGET before bundling."
        ) from exc


def expected_uv_binary_sha256(target: str, version: str) -> str:
    """Return the pinned SHA256 of the exact extracted uv member."""
    if version != UV_PINNED_VERSION:
        raise RuntimeError(
            "No pinned extracted uv SHA256 is configured for uv version "
            f"{version}. Update UV_PINNED_VERSION/UV_BINARY_SHA256_BY_TARGET."
        )
    try:
        return UV_BINARY_SHA256_BY_TARGET[target]
    except KeyError as exc:
        raise RuntimeError(
            f"No pinned extracted uv SHA256 is configured for target {target!r}."
        ) from exc


def expected_uv_member(target: str) -> str:
    """Return the exact archive member for one pinned uv target."""
    if target not in UV_SHA256_BY_TARGET:
        raise RuntimeError(f"No pinned uv archive member is configured for {target!r}")
    if is_windows_target(target):
        # uv's official Windows archive contains the executable at its root.
        return "uv.exe"
    return f"uv-{target}/uv"


def parse_sha256_manifest(text: str, expected_filename: str) -> str:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts:
            continue
        checksum = parts[0].lower()
        if len(parts) >= 2:
            filename = parts[-1].lstrip("*")
            if filename != expected_filename:
                raise RuntimeError(
                    f"Checksum manifest filename mismatch: expected {expected_filename}, got {filename}"
                )
        if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
            raise RuntimeError(f"Checksum manifest did not contain a valid SHA256 for {expected_filename}")
        return checksum
    raise RuntimeError(f"Checksum manifest was empty for {expected_filename}")


def verify_uv_archive_checksum(archive_path: Path, *, target: str, version: str, url: str) -> None:
    pinned_sha256 = expected_uv_sha256(target, version).lower()
    actual_sha256 = compute_sha256(archive_path).lower()
    if actual_sha256 != pinned_sha256:
        raise RuntimeError(
            "uv archive SHA256 mismatch for "
            f"{Path(url).name}: expected {pinned_sha256}, got {actual_sha256}"
        )


def _assert_uv_destination(path: Path) -> None:
    """Reject a destination that could redirect or alias the staged binary."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"cannot inspect uv staging destination: {path}") from exc
    if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x0400:
        raise RuntimeError(f"uv staging destination may not be a link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"uv staging destination must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise RuntimeError(f"uv staging destination may not be hardlinked: {path}")


def _assert_uv_directory(path: Path) -> None:
    """Reject symlink, reparse, and non-directory path components."""
    current = path
    missing: list[Path] = []
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                return
            current = parent
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot inspect uv staging directory: {current}") from exc
        if current.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x0400:
            raise RuntimeError(f"uv staging directory contains a link: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"uv staging path is not a directory: {current}")
        break
    for component in reversed(missing):
        if component.exists() or component.is_symlink():
            raise RuntimeError(f"uv staging directory changed during validation: {component}")


def _assert_uv_archive_member(member, expected: str) -> None:
    """Require the exact regular archive member and reject link metadata."""
    if member.name != expected:
        raise RuntimeError(
            f"uv archive member mismatch: expected {expected}, got {member.name}"
        )
    if not member.isreg() or member.issym() or member.islnk():
        raise RuntimeError(f"uv archive member is not a regular file: {expected}")


def _assert_uv_zip_member(info: zipfile.ZipInfo, expected: str) -> None:
    """Require the exact regular ZIP member and reject Unix link metadata."""
    if info.filename != expected or info.is_dir():
        raise RuntimeError(f"uv archive member is not the expected regular file: {expected}")
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type and file_type != stat.S_IFREG:
        raise RuntimeError(f"uv archive member contains link or special metadata: {expected}")


def _validate_staged_uv(path: Path, target: str, version: str) -> None:
    """Validate extracted bytes, metadata, and immutable executable mode."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"staged uv executable is unavailable: {path}") from exc
    if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x0400:
        raise RuntimeError(f"staged uv executable may not be a link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"staged uv executable is not a regular file: {path}")
    if metadata.st_nlink != 1:
        raise RuntimeError(f"staged uv executable may not be hardlinked: {path}")
    if metadata.st_mode & 0o222:
        raise RuntimeError(f"staged uv executable is owner-writable: {path}")
    if not metadata.st_mode & 0o111:
        raise RuntimeError(f"staged uv executable is not executable: {path}")
    expected = expected_uv_binary_sha256(target, version)
    actual = compute_sha256(path)
    if actual != expected:
        raise RuntimeError(
            "extracted uv SHA256 mismatch for "
            f"{target}: expected {expected}, got {actual}"
        )


def stage_uv(source_root: Path, target: str, version: str) -> Path:
    binary_name = uv_binary_name(target)
    archive_ext = "zip" if is_windows_target(target) else "tar.gz"
    url = f"https://github.com/astral-sh/uv/releases/download/{version}/uv-{target}.{archive_ext}"
    archive_path = download_to_temp(url)
    dest = source_root / "bundled" / binary_name
    _assert_uv_directory(dest.parent)
    dest.parent.mkdir(parents=True, exist_ok=True)
    expected = expected_uv_member(target)
    temporary: Path | None = None

    try:
        verify_uv_archive_checksum(archive_path, target=target, version=version, url=url)
        _assert_uv_destination(dest)
        _assert_uv_directory(dest.parent)
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{binary_name}.",
            suffix=".tmp",
            dir=dest.parent,
        )
        os.close(temporary_fd)
        temporary = Path(temporary_name)
        if archive_ext == "zip":
            with zipfile.ZipFile(archive_path) as archive:
                matches = [info for info in archive.infolist() if info.filename == expected]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"uv archive must contain exactly one member {expected!r}"
                    )
                info = matches[0]
                _assert_uv_zip_member(info, expected)
                with archive.open(info) as src, temporary.open("wb") as out:
                    shutil.copyfileobj(src, out)
                    out.flush()
                    os.fsync(out.fileno())
        else:
            with tarfile.open(archive_path, "r:gz") as archive:
                matches = [member for member in archive.getmembers() if member.name == expected]
                if len(matches) != 1:
                    raise RuntimeError(
                        f"uv archive must contain exactly one member {expected!r}"
                    )
                member = matches[0]
                _assert_uv_archive_member(member, expected)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError(f"{expected} is not a file in {archive_path}")
                with extracted, temporary.open("wb") as out:
                    shutil.copyfileobj(extracted, out)
                    out.flush()
                    os.fsync(out.fileno())
        temporary.chmod(0o555)
        _validate_staged_uv(temporary, target, version)
        os.replace(temporary, dest)
        temporary = None
        _validate_staged_uv(dest, target, version)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        archive_path.unlink(missing_ok=True)

    return dest


def _validate_defaultspack_v4(
    dest_root: Path,
    repository_root: Path,
) -> None:
    """Run the strict canonical Defaultspack v4 integrity gate on staged bytes."""

    pack_root = dest_root / "ecosystem" / "defaultspack"
    integrity_script = (
        repository_root
        / APP_SOURCE_DIR
        / "scripts"
        / "quality"
        / "scan_defaultspack_integrity.py"
    )
    if not integrity_script.is_file():
        raise FileNotFoundError(
            f"Defaultspack v4 integrity checker is missing: {integrity_script}"
        )

    runtime_root = repository_root / APP_SOURCE_DIR
    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))
    spec = importlib.util.spec_from_file_location(
        "_prepare_tauri_defaultspack_integrity",
        integrity_script,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load v4 integrity checker: {integrity_script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    errors: list[str] = []
    module.check_v4_integrity(errors, pack_root, strict=True)
    if errors:
        detail = "\n".join(f"- {error}" for error in errors[:40])
        suffix = "\n- ..." if len(errors) > 40 else ""
        raise RuntimeError(
            "Canonical Defaultspack v4 preflight failed:\n"
            f"{detail}{suffix}"
        )


def validate_bundle(
    dest_root: Path,
    require_runtime_tools: bool,
    target: str | None,
    *,
    repository_root: Path | None = None,
) -> None:
    """Validate staged resources without consulting legacy authority documents."""

    repository_root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    verify_no_python_bytecode(dest_root)
    required = [
        *REQUIRED_RUNTIME_BOOTSTRAP_FILES,
        *SEALED_ROLE_TARGETS,
        Path("requirements.txt"),
        Path("core_runtime/core_pack/core_control_panel/web/index.html"),
        *CANONICAL_DEFAULTSPACK_FILES,
        Path("ecosystem/defaultspack/ui/shell.html"),
        Path("ecosystem/defaultspack/ui/shell-app.js"),
    ]
    if require_runtime_tools:
        if not target:
            raise ValueError("--require-runtime-tools needs --target")
        required.extend(
            [
                Path("bundled") / uv_binary_name(target),
                Path("bundled") / pack_shell_binary_name(target),
            ]
        )
    if target:
        required.append(Path(SEALED_PYTHON_MANIFEST))

    missing = [str(path) for path in required if not (dest_root / path).exists()]
    if missing:
        raise FileNotFoundError("Missing bundled resource(s): " + ", ".join(missing))

    symlinks = [
        path.relative_to(dest_root).as_posix()
        for path in dest_root.rglob("*")
        if path.is_symlink()
    ]
    if symlinks:
        raise RuntimeError(
            "Staged resource contains symlink(s): " + ", ".join(symlinks[:20])
        )

    legacy = [
        path.relative_to(dest_root).as_posix()
        for path in dest_root.rglob("*")
        if path.is_file() and path.name in LEGACY_AUTHORITY_FILENAMES
    ]
    if legacy:
        raise RuntimeError(
            "Staged resource contains legacy authority document(s): "
            + ", ".join(legacy[:20])
        )

    _validate_defaultspack_v4(dest_root, repository_root)
    verify_canonical_host_package(dest_root, repository_root)
    verify_sealed_role_closure(dest_root, repository_root)
    verify_staged_bootstrap_import(dest_root)
    validated_sealed_python = None
    if target:
        validated_sealed_python = validate_sealed_python_resource(
            dest_root,
            target,
            repository_root,
        )
    _validate_generated_bundle_directories(
        dest_root,
        validated_sealed_python=validated_sealed_python,
    )
    verify_no_python_bytecode(dest_root)


def warn_legacy_defaultspack_app_bundle() -> None:
    legacy_app = Path.home() / "Applications" / "Rumi_Defaultspack.app"
    if not legacy_app.exists():
        return

    launch_script = legacy_app / "Contents" / "MacOS" / "launch"
    script_text = ""
    try:
        script_text = launch_script.read_text(encoding="utf-8")
    except OSError:
        pass

    missing_markers = [
        marker
        for marker in ("--api-token", "--port", "RUMI_LOG_DIR", "RUMI_DEFAULTSPACK_OPEN_BROWSER")
        if marker not in script_text
    ]
    if missing_markers:
        print(
            "warning: legacy Defaultspack app bundle detected at "
            f"{legacy_app}. It is missing current launch markers: "
            f"{', '.join(missing_markers)}. Re-register Defaultspack from Rumi Viewer "
            "or remove the legacy bundle to avoid stale launch/load-failed behavior.",
            file=sys.stderr,
        )
    else:
        print(
            "warning: legacy underscore-named Defaultspack app bundle detected at "
            f"{legacy_app}. Current builds generate 'Rumi Defaultspack.app'; "
            "re-registering from Rumi Viewer will clean up old launch services entries.",
            file=sys.stderr,
        )


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def ensure_resource_owner_root(repo_root: Path) -> Path:
    """Create the private generated-resource owner root on a clean checkout."""
    owner_root = repo_root / APP_RESOURCE_OWNER_DIR
    parent = owner_root.parent
    try:
        parent_metadata = parent.lstat()
        parent_resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            f"Tauri resource owner parent is unavailable: {parent}"
        ) from exc
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_resolved != parent
    ):
        raise RuntimeError(f"Tauri resource owner parent is unsafe: {parent}")
    try:
        owner_metadata = owner_root.lstat()
    except FileNotFoundError:
        owner_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        owner_metadata = owner_root.lstat()
    if (
        owner_root.is_symlink()
        or not stat.S_ISDIR(owner_metadata.st_mode)
        or owner_root.resolve(strict=True) != owner_root
    ):
        raise RuntimeError(f"Tauri resource owner root is unsafe: {owner_root}")
    return owner_root


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    source_root = repo_root / APP_SOURCE_DIR
    if args.uv_output_root is not None:
        if not args.stage_uv_only or not args.uv_output_root.is_absolute():
            print(
                "--uv-output-root requires --stage-uv-only and an absolute path",
                file=sys.stderr,
            )
            return 2
        source_root = args.uv_output_root
        source_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    dest_root = repo_root / APP_RESOURCE_DIR

    if not args.stage_uv_only and not source_root.joinpath("app.py").exists():
        print(f"Rumi source directory not found: {source_root}", file=sys.stderr)
        return 2

    if args.target and not args.stage_uv_only:
        staged_pack_shell = stage_pack_shell(repo_root, source_root, args.target)
        print(f"Staged {staged_pack_shell.relative_to(repo_root)}")

    if args.uv_version:
        if not args.target:
            print("--uv-version requires --target", file=sys.stderr)
            return 2
        staged_uv = stage_uv(source_root, args.target, args.uv_version)
        displayed_uv = (
            staged_uv
            if args.uv_output_root is not None
            else staged_uv.relative_to(repo_root)
        )
        print(f"Staged {displayed_uv}")

    if args.stage_uv_only:
        if not args.target or not args.uv_version:
            print("--stage-uv-only requires --target and --uv-version", file=sys.stderr)
            return 2
        return 0

    sealed_python_source = None
    if args.target:
        sealed_python_source = build_sealed_python_resource(
            repo_root,
            source_root,
            args.target,
        )

    expected_tree = None
    if dest_root.is_symlink():
        # Let the descriptor-bound cleanup report the unsafe final component.
        pass
    elif dest_root.exists():
        if not dest_root.is_dir():
            raise RuntimeError(
                f"Staged Tauri resource root is not a directory: {dest_root}"
            )
        manifest = verify_runtime_resource_manifest(dest_root)
        expected_tree = runtime_resource_expected_tree(manifest)

    owner_root = ensure_resource_owner_root(repo_root)
    sealed_reset = expected_tree is not None and os.name != "nt"
    remove_owned_path(
        dest_root,
        owner_root=owner_root,
        operation="reset staged Tauri resources",
        expected_tree=expected_tree if sealed_reset else None,
        unseal_read_only=sealed_reset,
    )
    dest_root.mkdir(parents=True, exist_ok=True)

    tracked_count = copy_tracked_runtime_files(repo_root, source_root, dest_root)
    stage_canonical_host_package(source_root, dest_root)
    generated_count = copy_generated_resource_dirs(
        source_root,
        dest_root,
        sealed_python_source=sealed_python_source,
    )

    validate_bundle(
        dest_root,
        args.require_runtime_tools,
        args.target,
        repository_root=repo_root,
    )
    write_runtime_resource_manifest(dest_root)
    verify_runtime_resource_manifest(dest_root)
    warn_legacy_defaultspack_app_bundle()
    print(
        "Prepared "
        f"{APP_RESOURCE_DIR} "
        f"({tracked_count} tracked files, {generated_count} generated files, "
        f"{format_size(directory_size(dest_root))})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
