#!/usr/bin/env python3
"""Create and safely remove one identity-bound temporary DMG workspace."""

from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLEANUP_HELPER = REPOSITORY_ROOT / "tobkiri_runtime/scripts/packaging_cleanup.py"
WORKSPACE_PREFIX = ".tobkiri-dmg."


def _load_cleanup_module():
    spec = importlib.util.spec_from_file_location(
        "tobkiri_dmg_packaging_cleanup", CLEANUP_HELPER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"packaging cleanup helper is unavailable: {CLEANUP_HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canonical_parent(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("DMG workspace parent must be absolute")
    parent = value.resolve(strict=True)
    metadata = value.lstat()
    if value != parent or value.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("DMG workspace parent must be a canonical real directory")
    if any(character in os.fspath(parent) for character in ("\n", "\r", "\t")):
        raise ValueError("DMG workspace parent contains a control character")
    return parent


def _workspace_path(value: Path, parent: Path) -> Path:
    if not value.is_absolute() or value.parent != parent:
        raise ValueError("DMG workspace must be an exact child of its owned parent")
    if not value.name.startswith(WORKSPACE_PREFIX):
        raise ValueError("DMG workspace name is outside the owned namespace")
    return value


def _verify_workspace_identity(
    workspace: Path,
    device: int,
    inode: int,
) -> None:
    metadata = workspace.lstat()
    if (
        workspace.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != device
        or metadata.st_ino != inode
        or metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError("DMG workspace differs from its creation-time identity")


def _remove_applications_link(
    workspace: Path,
    device: int,
    inode: int,
) -> None:
    """Unlink only the fixed DMG presentation link through held directories."""
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if not all(hasattr(os, name) for name in required_flags):
        raise RuntimeError("descriptor-relative DMG cleanup is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    workspace_fd = os.open(workspace, flags)
    try:
        metadata = os.fstat(workspace_fd)
        if (metadata.st_dev, metadata.st_ino) != (device, inode):
            raise RuntimeError("DMG workspace changed before presentation cleanup")
        try:
            staging_fd = os.open("staging", flags, dir_fd=workspace_fd)
        except FileNotFoundError:
            return
        try:
            try:
                link = os.stat(
                    "Applications",
                    dir_fd=staging_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if not stat.S_ISLNK(link.st_mode):
                raise RuntimeError("DMG Applications presentation path is not a symlink")
            if os.readlink("Applications", dir_fd=staging_fd) != "/Applications":
                raise RuntimeError("DMG Applications presentation link target changed")
            os.unlink("Applications", dir_fd=staging_fd)
        finally:
            os.close(staging_fd)
    finally:
        os.close(workspace_fd)


def create_workspace(parent_value: Path) -> None:
    """Create a private workspace and print its exact path/device/inode binding."""
    parent = _canonical_parent(parent_value)
    workspace = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=parent))
    metadata = workspace.lstat()
    parent_metadata = parent.lstat()
    if (
        workspace.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != parent_metadata.st_dev
        or metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError("created DMG workspace has an invalid ownership identity")
    print(f"{workspace}\t{metadata.st_dev}\t{metadata.st_ino}")


def cleanup_workspace(
    parent_value: Path,
    workspace_value: Path,
    device: int,
    inode: int,
) -> None:
    """Remove only the creation-bound workspace, including sealed descendants."""
    parent = _canonical_parent(parent_value)
    workspace = _workspace_path(workspace_value, parent)
    try:
        _verify_workspace_identity(workspace, device, inode)
    except FileNotFoundError:
        return
    _remove_applications_link(workspace, device, inode)
    cleanup = _load_cleanup_module()
    cleanup.remove_owned_path(
        workspace,
        owner_root=parent,
        operation="remove temporary macOS DMG workspace",
        unseal_read_only=True,
        expected_identity=(device, inode),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--parent", required=True, type=Path)
    for command in ("verify", "cleanup"):
        action = subparsers.add_parser(command)
        action.add_argument("--parent", required=True, type=Path)
        action.add_argument("--workspace", required=True, type=Path)
        action.add_argument("--device", required=True, type=int)
        action.add_argument("--inode", required=True, type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "create":
        create_workspace(args.parent)
    elif args.command == "verify":
        parent = _canonical_parent(args.parent)
        workspace = _workspace_path(args.workspace, parent)
        _verify_workspace_identity(workspace, args.device, args.inode)
    else:
        cleanup_workspace(
            args.parent,
            args.workspace,
            args.device,
            args.inode,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
