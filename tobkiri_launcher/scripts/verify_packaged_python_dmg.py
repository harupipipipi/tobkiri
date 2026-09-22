#!/usr/bin/env python3
"""Verify a packaged Python runtime through an identity-bound DMG mount."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import FrameType


MOUNT_PREFIX = ".tobkiri-dmg-verify."
DEVICE_PATTERN = re.compile(r"^/dev/disk[0-9]+(?:s[0-9]+)?$")
ALLOWED_SYSTEM_ALIASES = {
    Path("/var"): Path("/private/var"),
    Path("/tmp"): Path("/private/tmp"),
}


class DmgVerificationError(RuntimeError):
    """A fail-closed mounted-DMG verification error."""


@dataclass(frozen=True)
class Identity:
    """The stable filesystem identity of one object."""

    device: int
    inode: int
    owner: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> Identity:
        """Create an identity from stat metadata."""
        return cls(metadata.st_dev, metadata.st_ino, metadata.st_uid)


@dataclass(frozen=True)
class Executable:
    """A resolved executable and its creation-time identity."""

    path: Path
    identity: Identity


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if not all(hasattr(os, name) for name in required):
        raise DmgVerificationError("secure directory descriptors are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _identity(metadata: os.stat_result, *, kind: str) -> Identity:
    if not stat.S_ISDIR(metadata.st_mode):
        raise DmgVerificationError(f"{kind} is not a directory")
    return Identity.from_stat(metadata)


def _same_identity(actual: os.stat_result, expected: Identity, label: str) -> None:
    if Identity.from_stat(actual) != expected:
        raise DmgVerificationError(f"{label} identity changed")


def _reject_unapproved_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise DmgVerificationError(f"temporary parent does not exist: {current}")
        if not stat.S_ISLNK(metadata.st_mode):
            continue
        expected = ALLOWED_SYSTEM_ALIASES.get(current)
        if expected is None or current.resolve(strict=True) != expected:
            raise DmgVerificationError(
                f"temporary parent contains an unapproved symlink: {current}"
            )


def canonical_temporary_parent(value: Path) -> Path:
    """Resolve only the fixed macOS /var or /tmp system alias."""
    if not value.is_absolute() or value != Path(os.path.normpath(value)):
        raise DmgVerificationError("temporary parent must be an absolute clean path")
    if any(character in os.fspath(value) for character in ("\n", "\r", "\t")):
        raise DmgVerificationError("temporary parent contains a control character")
    _reject_unapproved_symlink_ancestors(value)
    canonical = value.resolve(strict=True)
    metadata = canonical.lstat()
    if canonical.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise DmgVerificationError("temporary parent is not a canonical directory")
    return canonical


def _resolve_executable(name: str) -> Executable:
    located = shutil.which(name)
    if located is None:
        raise DmgVerificationError(f"{name} is required for mounted DMG verification")
    return _bind_executable(Path(located), name)


def _bind_executable(value: Path, label: str) -> Executable:
    path = value.resolve(strict=True)
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise DmgVerificationError(f"{label} is not a canonical regular executable")
    if not os.access(path, os.X_OK):
        raise DmgVerificationError(f"{label} is not executable")
    return Executable(path, Identity.from_stat(metadata))


def _verify_executable(executable: Executable) -> None:
    metadata = executable.path.lstat()
    if executable.path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise DmgVerificationError(f"executable changed: {executable.path}")
    _same_identity(metadata, executable.identity, "executable")


def _run(
    executable: Executable,
    arguments: list[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    _verify_executable(executable)
    return subprocess.run(
        [os.fspath(executable.path), *arguments],
        check=False,
        close_fds=True,
        capture_output=capture_output,
    )


def _device_from_attach_plist(payload: bytes, mountpoint: Path) -> Path:
    try:
        document = plistlib.loads(payload)
    except (plistlib.InvalidFileException, ValueError) as error:
        raise DmgVerificationError(
            "hdiutil returned an invalid attach plist"
        ) from error
    entities = document.get("system-entities") if isinstance(document, dict) else None
    if not isinstance(entities, list):
        raise DmgVerificationError("hdiutil attach plist has no system entities")
    matches = [
        entity
        for entity in entities
        if isinstance(entity, dict)
        and entity.get("mount-point") == os.fspath(mountpoint)
    ]
    if len(matches) != 1:
        raise DmgVerificationError(
            "hdiutil did not bind exactly one canonical mountpoint"
        )
    device_value = matches[0].get("dev-entry")
    if (
        not isinstance(device_value, str)
        or DEVICE_PATTERN.fullmatch(device_value) is None
    ):
        raise DmgVerificationError("hdiutil returned an invalid mounted device")
    return Path(device_value)


class MountedDmg:
    """Own one private mountpoint while retaining host and mounted descriptors."""

    def __init__(self, dmg: Path, temporary_parent: Path | None = None) -> None:
        self.dmg = dmg
        requested_parent = temporary_parent or Path(
            os.environ.get("TMPDIR", tempfile.gettempdir())
        )
        self.parent = canonical_temporary_parent(requested_parent)
        self.hdiutil = _resolve_executable("hdiutil")
        self.parent_fd = os.open(self.parent, _directory_flags())
        parent_metadata = os.fstat(self.parent_fd)
        self.parent_identity = _identity(parent_metadata, kind="temporary parent")
        self.name = tempfile.mkdtemp(prefix=MOUNT_PREFIX, dir=self.parent).rsplit(
            os.sep, 1
        )[-1]
        self.path = self.parent / self.name
        host_metadata = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        self.host_identity = _identity(host_metadata, kind="created mountpoint")
        if self.host_identity.owner != os.geteuid():
            raise DmgVerificationError(
                "created mountpoint is not owned by this process"
            )
        self.host_fd = os.open(self.name, _directory_flags(), dir_fd=self.parent_fd)
        _same_identity(os.fstat(self.host_fd), self.host_identity, "mountpoint")
        self.device: Path | None = None
        self.mounted_identity: Identity | None = None
        self.mounted_fd: int | None = None
        self.detached = False
        self.removed = False

    def _verify_parent(self) -> None:
        _same_identity(os.fstat(self.parent_fd), self.parent_identity, "held parent")
        metadata = self.parent.lstat()
        if self.parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise DmgVerificationError("temporary parent path changed")
        _same_identity(metadata, self.parent_identity, "temporary parent path")

    def _path_metadata(self) -> os.stat_result:
        self._verify_parent()
        return os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)

    def _verify_host_path(self) -> None:
        metadata = self._path_metadata()
        if stat.S_ISLNK(metadata.st_mode):
            raise DmgVerificationError("mountpoint became a symlink")
        _identity(metadata, kind="mountpoint")
        _same_identity(metadata, self.host_identity, "mountpoint")
        _same_identity(os.fstat(self.host_fd), self.host_identity, "held mountpoint")

    def _bind_mounted_path(self, device: Path) -> None:
        metadata = self._path_metadata()
        mounted_identity = _identity(metadata, kind="mounted DMG root")
        if mounted_identity == self.host_identity:
            raise DmgVerificationError("hdiutil did not mount a distinct filesystem")
        if mounted_identity.owner != os.geteuid():
            raise DmgVerificationError("mounted DMG root has unexpected ownership")
        device_metadata = device.lstat()
        if device.is_symlink() or not stat.S_ISBLK(device_metadata.st_mode):
            raise DmgVerificationError("hdiutil mounted device is not a block device")
        if device_metadata.st_rdev != metadata.st_dev:
            raise DmgVerificationError("mounted DMG root has the wrong device identity")
        if self.path.resolve(strict=True) != self.path:
            raise DmgVerificationError("mounted DMG root is not canonical")
        mounted_fd = os.open(self.name, _directory_flags(), dir_fd=self.parent_fd)
        try:
            _same_identity(os.fstat(mounted_fd), mounted_identity, "mounted DMG root")
        except BaseException:
            os.close(mounted_fd)
            raise
        self.device = device
        self.mounted_identity = mounted_identity
        self.mounted_fd = mounted_fd

    def attach(self) -> None:
        """Attach read-only and bind the exact device and mounted root identity."""
        self._verify_host_path()
        result = _run(
            self.hdiutil,
            [
                "attach",
                "-plist",
                "-readonly",
                "-nobrowse",
                "-mountpoint",
                os.fspath(self.path),
                os.fspath(self.dmg),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            if result.stderr:
                sys.stderr.buffer.write(result.stderr)
            raise DmgVerificationError("hdiutil attach failed")
        device = _device_from_attach_plist(result.stdout, self.path)
        self._bind_mounted_path(device)

    def verify_mounted(self) -> None:
        """Reject every mount root, ancestor, owner, device, or path swap."""
        if self.mounted_identity is None or self.mounted_fd is None:
            raise DmgVerificationError("DMG mount is not identity-bound")
        metadata = self._path_metadata()
        if stat.S_ISLNK(metadata.st_mode):
            raise DmgVerificationError("mounted DMG root became a symlink")
        _same_identity(metadata, self.mounted_identity, "mounted DMG root")
        _same_identity(
            os.fstat(self.mounted_fd), self.mounted_identity, "held mounted DMG root"
        )
        if self.path.resolve(strict=True) != self.path:
            raise DmgVerificationError("mounted DMG path changed")

    def application_bundle(self) -> Path:
        """Return the sole direct, real application directory in the volume."""
        self.verify_mounted()
        assert self.mounted_fd is not None
        candidates: list[str] = []
        for name in os.listdir(self.mounted_fd):
            if not name.endswith(".app") or name in {".", ".."} or "/" in name:
                continue
            metadata = os.stat(name, dir_fd=self.mounted_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                candidates.append(name)
            elif stat.S_ISLNK(metadata.st_mode):
                raise DmgVerificationError("DMG application bundle is a symlink")
        if len(candidates) != 1:
            raise DmgVerificationError(
                "DMG must contain exactly one application bundle"
            )
        app_bundle = self.path / candidates[0]
        if app_bundle.resolve(strict=True) != app_bundle:
            raise DmgVerificationError("DMG application bundle is not canonical")
        self.verify_mounted()
        return app_bundle

    def detach(self) -> None:
        """Detach only the device established by the successful attach plist."""
        if self.device is None or self.detached:
            return
        self.verify_mounted()
        assert self.mounted_fd is not None
        os.close(self.mounted_fd)
        self.mounted_fd = None
        result = _run(self.hdiutil, ["detach", os.fspath(self.device)])
        if result.returncode != 0:
            metadata = self._path_metadata()
            if Identity.from_stat(metadata) == self.mounted_identity:
                self.mounted_fd = os.open(
                    self.name, _directory_flags(), dir_fd=self.parent_fd
                )
            raise DmgVerificationError(
                f"hdiutil failed to detach attached device {self.device}"
            )
        self.detached = True
        self._verify_host_path()

    def cleanup(self) -> None:
        """Idempotently detach and remove only the creation-bound mountpoint."""
        if self.removed:
            return
        self.detach()
        self._verify_host_path()
        os.rmdir(self.name, dir_fd=self.parent_fd)
        self.removed = True

    def close(self) -> None:
        """Close held descriptors after cleanup or a fail-closed error."""
        if self.mounted_fd is not None:
            os.close(self.mounted_fd)
            self.mounted_fd = None
        os.close(self.host_fd)
        os.close(self.parent_fd)


def _canonical_regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        path = path.absolute()
    canonical = path.resolve(strict=True)
    metadata = canonical.lstat()
    if canonical.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise DmgVerificationError(f"{label} must be a canonical regular file")
    return canonical


def verify_dmg(args: argparse.Namespace) -> None:
    """Mount, verify, codesign-check, detach, and safely remove one mountpoint."""
    dmg = _canonical_regular_file(args.dmg, "DMG")
    repository_root = args.repo_root.resolve(strict=True)
    verifier = _canonical_regular_file(
        Path(__file__).resolve().with_name("verify_packaged_python.py"),
        "packaged Python verifier",
    )
    python = _bind_executable(Path(sys.executable), "verification Python")
    codesign = _resolve_executable("codesign")
    mount = MountedDmg(dmg)
    primary_error: BaseException | None = None
    try:
        mount.attach()
        app_bundle = mount.application_bundle()
        for executable, arguments in (
            (
                python,
                [
                    "-B",
                    os.fspath(verifier),
                    "--repo-root",
                    os.fspath(repository_root),
                    "--app-bundle",
                    os.fspath(app_bundle),
                    "--target",
                    args.target,
                    "--expected-manifest-sha256",
                    args.expected_manifest_sha256,
                    "--native-smoke",
                ],
            ),
            (
                codesign,
                [
                    "--verify",
                    "--deep",
                    "--strict",
                    "--verbose=2",
                    os.fspath(app_bundle),
                ],
            ),
        ):
            mount.verify_mounted()
            result = _run(executable, arguments)
            if result.returncode != 0:
                raise DmgVerificationError(
                    f"mounted DMG verification command failed: {executable.path.name}"
                )
            mount.verify_mounted()
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            mount.cleanup()
        except (DmgVerificationError, OSError) as cleanup_error:
            if primary_error is None:
                raise
            print(
                f"mounted DMG cleanup also failed: {cleanup_error}",
                file=sys.stderr,
            )
        finally:
            mount.close()
    print(f"Verified mounted DMG packaged Python: {dmg}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dmg", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    return parser.parse_args()


def _raise_for_signal(signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def main() -> int:
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _raise_for_signal)
    args = parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_manifest_sha256):
        raise DmgVerificationError("expected sealed Python manifest digest is invalid")
    verify_dmg(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
