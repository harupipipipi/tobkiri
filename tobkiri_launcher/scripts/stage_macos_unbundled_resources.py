#!/usr/bin/env python3
"""Materialize exact Tauri resources beside an unbundled macOS executable."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import stat
import sys
from pathlib import Path


sys.dont_write_bytecode = True

RUNTIME_MANIFEST = "runtime-resource-manifest.v1.json"
RUNTIME_SCHEMA = "io.tobkiri.runtime-resource-manifest.v1"
SEALED_BUILDER = Path(".github/scripts/build_sealed_python_environment.py")
PACKAGED_VERIFIER = Path("tobkiri_launcher/scripts/verify_packaged_python.py")


def _canonical_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise RuntimeError(f"{label} must be a canonical absolute directory")
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{label} must be a real directory")
    return path


def _tree_identity(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x0400:
            raise RuntimeError(f"runtime resource is a link: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode))
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            payload = path.read_bytes()
            entries.append(
                (
                    relative,
                    "file",
                    mode,
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                )
            )
        else:
            raise RuntimeError(f"runtime resource has unsafe type: {relative}")
    return tuple(entries)


def _verify_runtime_manifest(root: Path) -> None:
    manifest = root / RUNTIME_MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError("runtime resource manifest is missing or unsafe")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema", "entries"}:
        raise RuntimeError("runtime resource manifest shape is invalid")
    if document["schema"] != RUNTIME_SCHEMA or not isinstance(document["entries"], list):
        raise RuntimeError("runtime resource manifest schema is invalid")
    actual = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or path == manifest:
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("runtime resource manifest tree is unsafe")
        payload = path.read_bytes()
        actual.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if document["entries"] != actual:
        raise RuntimeError("runtime resource manifest does not match the source tree")


def copy_exact_runtime_tree(source: Path, destination: Path) -> None:
    """Copy one sealed resource tree without xattrs or directory-mode drift."""
    source = _canonical_directory(source, "source runtime resource")
    parent = _canonical_directory(destination.parent, "destination parent")
    if destination.parent != parent or destination.exists() or destination.is_symlink():
        raise RuntimeError("destination runtime resource must not already exist")
    _verify_runtime_manifest(source)
    expected = _tree_identity(source)
    destination.mkdir(mode=0o700)
    try:
        directories = [source]
        for source_path in sorted(source.rglob("*")):
            relative = source_path.relative_to(source)
            destination_path = destination / relative
            metadata = source_path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                destination_path.mkdir(mode=0o700)
                directories.append(source_path)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                shutil.copyfile(source_path, destination_path, follow_symlinks=False)
                destination_path.chmod(stat.S_IMODE(metadata.st_mode))
            else:
                raise RuntimeError(f"runtime resource changed during copy: {relative}")
        for source_path in sorted(
            directories,
            key=lambda value: len(value.relative_to(source).parts),
            reverse=True,
        ):
            target = destination / source_path.relative_to(source)
            target.chmod(stat.S_IMODE(source_path.lstat().st_mode))
        if _tree_identity(destination) != expected:
            raise RuntimeError("unbundled runtime copy differs from its sealed source")
        _verify_runtime_manifest(destination)
    except Exception:
        for path in sorted(
            (destination, *destination.rglob("*")),
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            if path.exists() and path.is_dir():
                path.chmod(0o700)
            elif path.exists():
                path.chmod(0o600)
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"sealed Python builder is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def seal_staged_python(
    repository_root: Path,
    destination_root: Path,
    target: str,
) -> str:
    """Restore authenticated sealed modes in an exact unbundled resource copy."""
    builder = _load_module(
        "tobkiri_unbundled_seal",
        repository_root / SEALED_BUILDER,
    )
    verifier = _load_module(
        "tobkiri_unbundled_verifier",
        repository_root / PACKAGED_VERIFIER,
    )
    sealed_root = destination_root / "python-runtime"
    manifest = sealed_root / builder.MANIFEST_FILENAME
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    verifier._preseal_tauri_directories(
        sealed_root,
        target,
        digest,
        builder,
    )
    builder.validate_environment(
        sealed_root,
        target,
        expected_manifest_digest=digest,
        run_native_smoke=False,
        require_sealed=True,
    )
    return digest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--target", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository_root = _canonical_directory(args.repo_root, "repository root")
    copy_exact_runtime_tree(args.source_root, args.destination_root)
    digest = seal_staged_python(
        repository_root,
        args.destination_root,
        args.target,
    )
    print(
        "Staged exact unbundled resources "
        f"({digest}) at {args.destination_root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
