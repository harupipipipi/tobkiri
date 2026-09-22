#!/usr/bin/env python3
"""Host-seal and verify the actual packaged macOS Python resource tree."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar


RESOURCE_RELATIVE = Path("Contents/Resources/app/python-runtime")
MUTABLE_LOG_RELATIVE = Path("Contents/Resources/app/logs")
BUILDER_RELATIVE = Path(".github/scripts/build_sealed_python_environment.py")
_T = TypeVar("_T")


def _load_builder(repository_root: Path):
    path = repository_root / BUILDER_RELATIVE
    spec = importlib.util.spec_from_file_location("tobkiri_packaged_python_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"sealed Python builder is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canonical_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise RuntimeError(f"{label} must be absolute")
    resolved = path.resolve(strict=True)
    metadata = path.lstat()
    if path != resolved or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{label} must be a canonical real directory")
    return path


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle_tree_identity(root: Path) -> tuple[tuple[object, ...], ...]:
    """Return a byte, mode, type, and link identity for an application tree."""
    entries: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, "directory", mode))
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(
                (relative, "file", mode, metadata.st_size, _raw_sha256(path))
            )
        elif stat.S_ISLNK(metadata.st_mode):
            entries.append((relative, "symlink", mode, os.readlink(path)))
        else:
            entries.append((relative, "other", mode, metadata.st_rdev))
    return tuple(entries)


def _without_bundle_mutation(app_bundle: Path, operation: Callable[[], _T]) -> _T:
    """Run packaged verification and reject every application-tree mutation."""
    before = _bundle_tree_identity(app_bundle)
    try:
        result = operation()
    finally:
        after = _bundle_tree_identity(app_bundle)
        if after != before:
            raise RuntimeError(
                "packaged verification mutated the signed application bundle"
            )
    return result


def _preseal_tauri_directories(root: Path, target: str, expected: str, builder) -> None:
    """Accept only Tauri's owner-write directory delta, then Host-seal it."""
    spec = builder.target_spec(target)
    manifest_path = root / builder.MANIFEST_FILENAME
    if _raw_sha256(manifest_path) != expected:
        raise RuntimeError("packaged sealed Python manifest binding changed")
    document = builder._validate_manifest_shape(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    records = builder._records(root, spec)
    if records != document["files"]:
        raise RuntimeError("packaged sealed Python file inventory changed")
    for entry in records:
        path = root / str(entry["path"])
        expected_mode = (
            builder.IMMUTABLE_EXECUTABLE_MODE
            if bool(entry["executable"])
            else builder.IMMUTABLE_FILE_MODE
        )
        if stat.S_IMODE(path.lstat().st_mode) != expected_mode:
            raise RuntimeError(
                f"packaged sealed Python file mode changed: {entry['path']}"
            )
    if stat.S_IMODE(manifest_path.lstat().st_mode) != builder.IMMUTABLE_FILE_MODE:
        raise RuntimeError("packaged sealed Python manifest mode changed")
    if builder._actual_directories(root) != builder._expected_directories(records):
        raise RuntimeError("packaged sealed Python directory inventory changed")
    evidence = json.loads(
        (root / builder.DIRECTORY_MODES_FILENAME).read_text(encoding="utf-8")
    )
    if evidence != builder._directory_mode_document(records):
        raise RuntimeError("packaged sealed Python directory mode evidence changed")

    directories = [
        path
        for _relative, path, kind, _metadata in builder._walk_tree(root)
        if kind == "directory"
    ]
    for path in (root, *directories):
        mode = stat.S_IMODE(path.lstat().st_mode)
        if mode not in {builder.IMMUTABLE_DIRECTORY_MODE, 0o755}:
            raise RuntimeError(
                f"packaged sealed Python directory has unsafe pre-seal mode: "
                f"{path.relative_to(root) if path != root else '.'} {mode:04o}"
            )
    identities = {
        path: (path.lstat().st_dev, path.lstat().st_ino)
        for path in (root, *directories)
    }
    for path in sorted(
        directories,
        key=lambda value: (len(value.relative_to(root).parts), value.as_posix()),
        reverse=True,
    ):
        os.chmod(path, builder.IMMUTABLE_DIRECTORY_MODE, follow_symlinks=False)
    os.chmod(root, builder.IMMUTABLE_DIRECTORY_MODE, follow_symlinks=False)
    for path, identity in identities.items():
        metadata = path.lstat()
        if (
            (metadata.st_dev, metadata.st_ino) != identity
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != builder.IMMUTABLE_DIRECTORY_MODE
        ):
            raise RuntimeError("packaged sealed Python directory changed during Host seal")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--app-bundle", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--seal-tauri-directories", action="store_true")
    parser.add_argument("--native-smoke", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository_root = _canonical_directory(args.repo_root, "repository root")
    app_bundle = _canonical_directory(args.app_bundle, "application bundle")
    if len(args.expected_manifest_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in args.expected_manifest_sha256
    ):
        raise RuntimeError("expected sealed Python manifest digest is invalid")
    resource = _canonical_directory(
        app_bundle / RESOURCE_RELATIVE,
        "packaged sealed Python resource",
    )
    if (app_bundle / MUTABLE_LOG_RELATIVE).exists():
        raise RuntimeError("packaged application contains mutable launch logs")
    builder = _load_builder(repository_root)
    if args.seal_tauri_directories:
        _preseal_tauri_directories(
            resource,
            args.target,
            args.expected_manifest_sha256,
            builder,
        )
    digest = _without_bundle_mutation(
        app_bundle,
        lambda: (
            builder.validate_environment(
                resource,
                args.target,
                expected_manifest_digest=args.expected_manifest_sha256,
                run_native_smoke=args.native_smoke,
                require_sealed=True,
            ),
            builder.verify_packaged_application_closure(resource.parent, resource),
        )[0],
    )
    if (app_bundle / MUTABLE_LOG_RELATIVE).exists():
        raise RuntimeError("packaged verification created mutable launch logs")
    print(f"Verified packaged sealed Python environment ({digest}) at {resource}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
