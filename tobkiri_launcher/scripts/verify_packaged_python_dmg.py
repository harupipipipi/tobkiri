#!/usr/bin/env python3
"""Verify packaged resources through an identity-bound DMG mount."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType


MOUNT_PREFIX = ".tobkiri-dmg-verify."
DEVICE_PATTERN = re.compile(r"^/dev/disk[0-9]+(?:s[0-9]+)?$")
NATIVE_SETUP_COMPONENT = Path("Contents/Resources/app/native-setup/component.wasm")
MAX_NATIVE_SETUP_COMPONENT_BYTES = 32 * 1024 * 1024
NATIVE_REGISTRY_PACKS = Path("Contents/Resources/app/native-registry-packs")
REGISTRY_RECEIPT_NAME = "receipt.json"
REGISTRY_RECEIPT_FORMAT = "tobkiri.registry-packs/1"
SETTLEMENT_RECEIPT_FORMAT = "tobkiri.registry-packs/2"
ORCHESTRATION_RECEIPT_FORMAT = "tobkiri.registry-packs/3"
PROVIDER_ADAPTER_RECEIPT_FORMAT = "tobkiri.registry-packs/4"
AI_ROUTING_RECEIPT_FORMAT = "tobkiri.registry-packs/5"
APPLICATION_PRESENTATION_RECEIPT_FORMAT = "tobkiri.registry-packs/6"
ORCHESTRATION_PACK_ID = "local.saved-orchestration"
PROVIDER_ADAPTER_PACK_ID = "local.provider-adapter"
AI_ROUTING_PACK_ID = "local.ai-routing"
APPLICATION_PRESENTATION_PACK_ID = "local.application-presentation"
PINNED_REGISTRY_PACK_VERSIONS = {
    ORCHESTRATION_PACK_ID: ("0.1.0",),
    PROVIDER_ADAPTER_PACK_ID: ("0.1.0", "0.2.0", "0.3.0"),
    AI_ROUTING_PACK_ID: ("0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0"),
    APPLICATION_PRESENTATION_PACK_ID: ("0.1.0",),
}
REGISTRY_ARCHIVES = {
    "local.provider-registry": "local.provider-registry.tpk",
    "local.model-registry": "local.model-registry.tpk",
    "local.saved-conversation": "local.saved-conversation.tpk",
}
REGISTRY_ARCHIVES_WITH_SETTLEMENT = {
    **REGISTRY_ARCHIVES,
    "local.saved-turn-runtime": "local.saved-turn-runtime.tpk",
}
REGISTRY_ARCHIVES_WITH_ORCHESTRATION = {
    **REGISTRY_ARCHIVES_WITH_SETTLEMENT,
    ORCHESTRATION_PACK_ID: f"{ORCHESTRATION_PACK_ID}.tpk",
}
REGISTRY_ARCHIVES_WITH_PROVIDER_ADAPTER = {
    **REGISTRY_ARCHIVES_WITH_ORCHESTRATION,
    PROVIDER_ADAPTER_PACK_ID: f"{PROVIDER_ADAPTER_PACK_ID}.tpk",
}
REGISTRY_ARCHIVES_WITH_AI_ROUTING = {
    **REGISTRY_ARCHIVES_WITH_PROVIDER_ADAPTER,
    AI_ROUTING_PACK_ID: f"{AI_ROUTING_PACK_ID}.tpk",
}
REGISTRY_ARCHIVES_WITH_APPLICATION_PRESENTATION = {
    **REGISTRY_ARCHIVES_WITH_AI_ROUTING,
    APPLICATION_PRESENTATION_PACK_ID: f"{APPLICATION_PRESENTATION_PACK_ID}.tpk",
}
MAX_REGISTRY_RECEIPT_BYTES = 16 * 1024
# Match the package limit in crates/tobkiri-manifest/src/package.rs.
MAX_REGISTRY_ARCHIVE_BYTES = 512 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
TAGGED_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
PACK_VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}")
ALLOWED_SYSTEM_ALIASES = {
    Path("/var"): Path("/private/var"),
    Path("/tmp"): Path("/private/tmp"),
}


class DmgVerificationError(RuntimeError):
    """A fail-closed mounted-DMG verification error."""


_ATTACH_TRANSIENT_ATTEMPTS = 3
_ATTACH_TRANSIENT_DELAY_SECONDS = 0.5
_ATTACH_TRANSIENT_ERROR = b"Resource temporarily unavailable"


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
        for attempt in range(_ATTACH_TRANSIENT_ATTEMPTS):
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
            if result.returncode == 0:
                break
            if result.stderr:
                sys.stderr.buffer.write(result.stderr)
            if (
                _ATTACH_TRANSIENT_ERROR not in result.stderr
                or attempt + 1 == _ATTACH_TRANSIENT_ATTEMPTS
            ):
                raise DmgVerificationError("hdiutil attach failed")
            time.sleep(_ATTACH_TRANSIENT_DELAY_SECONDS)
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

    @contextmanager
    def _resource_directory(
        self, app_bundle: Path, relative: Path, label: str
    ) -> Iterator[tuple[int, int]]:
        self.verify_mounted()
        assert self.mounted_fd is not None
        assert self.mounted_identity is not None
        volume_device = self.mounted_identity.device
        try:
            with ExitStack() as opened:
                directory = self.mounted_fd
                for part in (app_bundle.name, *relative.parts):
                    directory = os.open(part, _directory_flags(), dir_fd=directory)
                    opened.callback(os.close, directory)
                    if os.fstat(directory).st_dev != volume_device:
                        raise DmgVerificationError(f"{label} left the DMG")
                yield directory, volume_device
        except OSError as error:
            raise DmgVerificationError(
                f"{label} is missing or could not be read without following links"
            ) from error
        self.verify_mounted()

    def verify_native_setup_component(
        self, app_bundle: Path, expected_bytes: int, expected_sha256: str
    ) -> None:
        """Hash the fixed physical Wasm resource from the held mounted root."""
        label = "native Setup Wasm"
        with self._resource_directory(
            app_bundle, NATIVE_SETUP_COMPONENT.parent, label
        ) as (directory, device):
            _verify_resource(
                directory,
                device,
                NATIVE_SETUP_COMPONENT.name,
                label,
                expected_bytes,
                expected_sha256,
            )

    def verify_native_registry_packs(
        self, app_bundle: Path, expected_receipt_sha256: str
    ) -> None:
        """Verify the pinned receipt and each physical Registry Pack archive."""
        with self._resource_directory(
            app_bundle, NATIVE_REGISTRY_PACKS, "native Registry Packs"
        ) as (directory, device):
            receipt_label = "native Registry Packs receipt"
            with _resource_file(
                directory, device, REGISTRY_RECEIPT_NAME, receipt_label
            ) as (descriptor, size):
                if not 0 < size <= MAX_REGISTRY_RECEIPT_BYTES:
                    raise DmgVerificationError(f"{receipt_label} size is out of range")
                payload = b"".join(_file_blocks(descriptor, size, receipt_label))
            if hashlib.sha256(payload).hexdigest() != expected_receipt_sha256:
                raise DmgVerificationError(f"{receipt_label} SHA-256 differs")
            pins = _registry_archive_pins(payload)
            _verify_registry_directory_entries(directory, pins)
            for pin in pins:
                _verify_resource(
                    directory,
                    device,
                    pin.name,
                    f"native Registry Pack {pin.name}",
                    pin.size,
                    pin.sha256,
                )

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


@contextmanager
def _resource_file(
    directory: int, device: int, name: str, label: str
) -> Iterator[tuple[int, int]]:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_dev != device
                or metadata.st_nlink != 1
            ):
                raise DmgVerificationError(
                    f"{label} is not a physical regular file on the DMG"
                )
            yield descriptor, metadata.st_size
        finally:
            os.close(descriptor)
    except OSError as error:
        raise DmgVerificationError(
            f"{label} is missing or could not be read without following links"
        ) from error


def _file_blocks(descriptor: int, expected_bytes: int, label: str) -> Iterator[bytes]:
    remaining = expected_bytes
    while block := os.read(descriptor, min(64 * 1024, remaining + 1)):
        remaining -= len(block)
        if remaining < 0:
            raise DmgVerificationError(f"{label} size differs")
        yield block
    if remaining:
        raise DmgVerificationError(f"{label} size differs")


def _verify_resource(
    directory: int,
    device: int,
    name: str,
    label: str,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    with _resource_file(directory, device, name, label) as (descriptor, size):
        if size != expected_bytes:
            raise DmgVerificationError(f"{label} size differs")
        digest = hashlib.sha256()
        for block in _file_blocks(descriptor, expected_bytes, label):
            digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise DmgVerificationError(f"{label} SHA-256 differs")


def _verify_registry_directory_entries(
    directory: int, pins: list[_RegistryArchivePin]
) -> None:
    expected = {REGISTRY_RECEIPT_NAME, *(pin.name for pin in pins)}
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name not in expected:
                raise DmgVerificationError("native Registry Packs has extra entries")
            expected.remove(entry.name)
    if expected:
        raise DmgVerificationError("native Registry Packs is missing required entries")


@dataclass(frozen=True)
class _RegistryArchivePin:
    name: str
    size: int
    sha256: str


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _registry_archive_pins(payload: bytes) -> list[_RegistryArchivePin]:
    try:
        receipt = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except (ValueError, RecursionError) as error:
        raise DmgVerificationError(
            "native Registry Packs receipt is invalid"
        ) from error
    if (
        not isinstance(receipt, dict)
        or receipt.keys() != {"format", "packs"}
        or not isinstance(receipt["packs"], list)
    ):
        raise DmgVerificationError("native Registry Packs receipt has the wrong format")
    if receipt["format"] == REGISTRY_RECEIPT_FORMAT:
        archives = REGISTRY_ARCHIVES
    elif receipt["format"] == SETTLEMENT_RECEIPT_FORMAT:
        archives = REGISTRY_ARCHIVES_WITH_SETTLEMENT
    elif receipt["format"] == ORCHESTRATION_RECEIPT_FORMAT:
        archives = REGISTRY_ARCHIVES_WITH_ORCHESTRATION
    elif receipt["format"] == PROVIDER_ADAPTER_RECEIPT_FORMAT:
        archives = REGISTRY_ARCHIVES_WITH_PROVIDER_ADAPTER
    elif receipt["format"] == AI_ROUTING_RECEIPT_FORMAT:
        archives = REGISTRY_ARCHIVES_WITH_AI_ROUTING
    elif receipt["format"] == APPLICATION_PRESENTATION_RECEIPT_FORMAT:
        archives = REGISTRY_ARCHIVES_WITH_APPLICATION_PRESENTATION
    else:
        raise DmgVerificationError("native Registry Packs receipt has the wrong format")
    if len(receipt["packs"]) != len(archives):
        raise DmgVerificationError("native Registry Packs receipt has the wrong format")

    pins = []
    for (pack_id, archive), record in zip(archives.items(), receipt["packs"]):
        if not isinstance(record, dict) or record.keys() != {
            "id",
            "version",
            "archive",
            "archive_bytes",
            "archive_sha256",
            "manifest_sha256",
        }:
            raise DmgVerificationError(
                "native Registry Packs receipt has an invalid record"
            )
        if record["id"] != pack_id or record["archive"] != archive:
            raise DmgVerificationError(
                "native Registry Packs receipt has an invalid archive"
            )
        size = record["archive_bytes"]
        digest = record["archive_sha256"]
        if (
            type(size) is not int
            or not 0 < size <= MAX_REGISTRY_ARCHIVE_BYTES
            or not _matches(TAGGED_SHA256, digest)
            or not _matches(TAGGED_SHA256, record["manifest_sha256"])
            or not _matches(PACK_VERSION, record["version"])
            or (
                pack_id in PINNED_REGISTRY_PACK_VERSIONS
                and record["version"] not in PINNED_REGISTRY_PACK_VERSIONS[pack_id]
            )
        ):
            raise DmgVerificationError(
                "native Registry Packs receipt has an invalid pin"
            )
        pins.append(_RegistryArchivePin(archive, size, digest.removeprefix("sha256:")))
    return pins


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
        if args.expected_native_setup_component_sha256 is not None:
            mount.verify_native_setup_component(
                app_bundle,
                args.expected_native_setup_component_bytes,
                args.expected_native_setup_component_sha256,
            )
        if args.expected_native_registry_packs_receipt_sha256 is not None:
            mount.verify_native_registry_packs(
                app_bundle, args.expected_native_registry_packs_receipt_sha256
            )
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
    if args.expected_native_setup_component_sha256 is not None:
        print(f"Verified mounted DMG native Setup Wasm: {dmg}")
    if args.expected_native_registry_packs_receipt_sha256 is not None:
        print(f"Verified mounted DMG native Registry Packs: {dmg}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dmg", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-native-setup-component-sha256")
    parser.add_argument("--expected-native-setup-component-bytes")
    parser.add_argument("--expected-native-registry-packs-receipt-sha256")
    args = parser.parse_args()
    sha256 = args.expected_native_setup_component_sha256
    size = args.expected_native_setup_component_bytes
    if (sha256 is None) != (size is None):
        parser.error("native Setup Wasm SHA-256 and byte count must be supplied together")
    if sha256 is not None and not SHA256.fullmatch(sha256):
        parser.error("expected native Setup Wasm SHA-256 is invalid")
    if size is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,7}", size) or (
            int(size) > MAX_NATIVE_SETUP_COMPONENT_BYTES
        ):
            parser.error("expected native Setup Wasm byte count is out of range")
        args.expected_native_setup_component_bytes = int(size)
    receipt_digest = args.expected_native_registry_packs_receipt_sha256
    if receipt_digest is not None and not SHA256.fullmatch(receipt_digest):
        parser.error("expected native Registry Packs receipt SHA-256 is invalid")
    return args


def _raise_for_signal(signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def main() -> int:
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _raise_for_signal)
    args = parse_args()
    if not SHA256.fullmatch(args.expected_manifest_sha256):
        raise DmgVerificationError("expected sealed Python manifest digest is invalid")
    verify_dmg(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
