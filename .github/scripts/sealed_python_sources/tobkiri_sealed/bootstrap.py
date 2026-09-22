"""Verified ``python -I -B -m tobkiri_sealed.bootstrap`` process boundary.

The Launcher supplies the role, nonce, manifest, environment root, and the
attestation destination.  Bootstrap never invents an identity or accepts
unknown bootstrap arguments; everything after ``--`` is passed to the fixed
role without interpretation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from . import SCHEMA

PROTOCOL_SCHEMA = "io.tobkiri.sealed-python-launch.v3"
ATTESTATION_SCHEMA = "io.tobkiri.sealed-python-attestation.v2"
ATTESTATION_FILE_SCHEMA = "io.tobkiri.sealed-python-attestation-file.v1"
ROLE_ENTRYPOINTS = {
    "typed": "kernel_entry.py",
    "defaultspack": "defaultspack_entry.py",
    "host_helper": "host_helper_entry.py",
}
ROLE_TARGETS = {
    "typed": ("app.py",),
    "defaultspack": (
        "ecosystem",
        "defaultspack",
        "defaultspack",
        "desktop_app.py",
    ),
    "host_helper": (
        "core_runtime",
        "host_broker",
        "computer_host_helper.py",
    ),
}
ROLE_APPLICATION_IMPORT_ROOTS = {
    "typed": (),
    "defaultspack": ("app/ecosystem/defaultspack",),
    "host_helper": (),
}
MANIFEST_NAME = "sealed-environment.v1.json"
RUNTIME_OVERLAY_NAME = "app/runtime-resource-manifest.v1.json"
OUTER_RUNTIME_MANIFEST_NAME = "runtime-resource-manifest.v1.json"
PACKVM_PROVISIONING_MANIFEST_NAME = "packvm-vz-provisioning.v1.json"
PACKVM_HELPER_MANIFEST_NAME = "packvm-vz-helper.manifest.v1.json"
RUNTIME_OVERLAY_SCHEMA = "io.tobkiri.sealed-runtime-overlay.v1"
DIRECTORY_MODES_NAME = "sealed-directory-modes.v1.json"
DIRECTORY_MODES_SCHEMA = "io.tobkiri.sealed-python-directory-modes.v1"
MANIFEST_SHA_ENV = "TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256"
LEASE_NAME = "lease.v1"
REPARSE_POINT = 0x0400
FILE_KEYS = ("path", "size", "sha256", "executable")
MANIFEST_KEYS = (
    "schema",
    "environment_digest",
    "platform",
    "architecture",
    "python_version",
    "package_provenance",
    "sentinels",
    "files",
)
SENTINEL_KEYS = (
    "stdlib_sha256",
    "site_packages_sha256",
    "native_sha256",
)
SENTINEL_FILENAMES = {
    "stdlib_sha256": "stdlib.sha256",
    "site_packages_sha256": "site-packages.sha256",
    "native_sha256": "native.sha256",
}
PACKAGE_KIND_BY_PLATFORM = {
    "macos": "pinned-python-build-standalone-v1",
    "linux": "linux-immutable-package-v1",
    "windows": "windows-authenticode-v1",
}
FORBIDDEN_LAUNCH_ENVIRONMENTS = {
    "REPO",
    "RUMI_CORE_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
}
_APPLE_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
_PACKVM_BUNDLE_BINDING_KEYS = (
    "root",
    "provisioning_sha256",
    "helper_manifest_sha256",
    "helper_team_id",
)


class SealedBootstrapError(RuntimeError):
    """Raised when the supplied sealed process contract is unsafe."""


_SCOPE_CONSTRUCTOR_TOKEN = object()


class _SealedDispatchScope:
    """Opaque bootstrap-issued capability for one sealed role target.

    The capability is created only after the supplied manifest has been read
    and is passed through the fixed wrapper API. Packaged application code
    must prove that its own module file is the exact manifest-bound target;
    no basename or environment variable can opt it into the sealed path.
    """

    __slots__ = (
        "_constructor_token",
        "_root",
        "_manifest_path",
        "_manifest_digest",
        "_environment_digest",
        "_target",
        "_packvm_bundle_binding",
    )

    def __init__(
        self,
        constructor_token: object,
        root: Path,
        manifest_path: Path,
        manifest_digest: str,
        environment_digest: str,
        target: Sequence[str],
        packvm_bundle_binding: Mapping[str, str] | None,
    ) -> None:
        if constructor_token is not _SCOPE_CONSTRUCTOR_TOKEN:
            raise TypeError("sealed dispatch scope is bootstrap-private")
        if not _is_sha256_identity(manifest_digest) or not _is_sha256_identity(environment_digest):
            raise SealedBootstrapError("sealed dispatch scope identity is invalid")
        self._constructor_token = constructor_token
        self._root = root
        self._manifest_path = manifest_path
        self._manifest_digest = manifest_digest
        self._environment_digest = environment_digest
        self._target = tuple(target)
        if packvm_bundle_binding is not None:
            if tuple(packvm_bundle_binding) != _PACKVM_BUNDLE_BINDING_KEYS:
                raise SealedBootstrapError("sealed PackVM bundle binding is invalid")
            self._packvm_bundle_binding: Mapping[str, str] | None = MappingProxyType(
                dict(packvm_bundle_binding)
            )
        else:
            self._packvm_bundle_binding = None

    def app_root_for(self, module_file: str | os.PathLike[str]) -> Path:
        """Return the app root only for this scope's exact sealed target."""
        if self._constructor_token is not _SCOPE_CONSTRUCTOR_TOKEN:
            raise SealedBootstrapError("sealed dispatch scope token changed")
        expected_manifest = self._root / MANIFEST_NAME
        if self._manifest_path != expected_manifest:
            raise SealedBootstrapError("sealed dispatch scope manifest is not bound")
        try:
            if _sha256_bytes(self._manifest_path.read_bytes()) != self._manifest_digest:
                raise SealedBootstrapError("sealed dispatch scope manifest changed")
            app_root = _assert_real_directory(self._root / "app", "sealed application root")
            candidate = Path(module_file)
            expected = app_root.joinpath(*self._target)
            candidate_resolved = candidate.resolve(strict=True)
            expected_resolved = expected.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SealedBootstrapError("sealed dispatch target is unavailable") from exc
        if (
            not candidate.is_absolute()
            or candidate != candidate_resolved
            or candidate_resolved != expected_resolved
        ):
            raise SealedBootstrapError(
                "sealed dispatch target is not the manifest-bound application file"
            )
        _assert_regular_file(expected, "sealed dispatch target")
        return app_root

    def packvm_bundle_binding_for(
        self,
        module_file: str | os.PathLike[str],
    ) -> Mapping[str, str] | None:
        """Return the immutable PackVM bundle binding for one exact target.

        This is intentionally a scope method rather than an environment
        variable or Host-contract lookup.  A packaged target must first prove
        it is the role target that the sealed manifest selected.
        """

        self.app_root_for(module_file)
        return self._packvm_bundle_binding


def _sha256_bytes(payload: bytes) -> str:
    """Return the sealed raw SHA-256 identity."""
    return hashlib.sha256(payload).hexdigest()


def _is_sha256_identity(value: object) -> bool:
    """Return whether a value is a lowercase raw 64-hex identity."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT)


def _assert_real_directory(
    path: Path,
    label: str,
    *,
    require_immutable: bool = True,
) -> Path:
    """Require a canonical, non-linked directory."""
    if not path.is_absolute():
        raise SealedBootstrapError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SealedBootstrapError(f"{label} is unavailable: {path}") from exc
    if (
        path != resolved
        or path.is_symlink()
        or _is_reparse_point(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise SealedBootstrapError(f"{label} is linked or not a directory: {path}")
    if require_immutable and metadata.st_mode & 0o222:
        raise SealedBootstrapError(f"{label} is writable: {path}")
    return resolved


def _assert_regular_file(
    path: Path,
    label: str,
    *,
    allow_missing: bool = False,
    require_immutable: bool = True,
) -> Path:
    """Require a canonical regular file with one link and no write bits."""
    if not path.is_absolute():
        raise SealedBootstrapError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        if allow_missing:
            return path
        raise SealedBootstrapError(f"{label} is missing: {path}")
    except (OSError, RuntimeError) as exc:
        raise SealedBootstrapError(f"{label} is unavailable: {path}") from exc
    if (
        path != resolved
        or path.is_symlink()
        or _is_reparse_point(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or (require_immutable and metadata.st_mode & 0o222)
    ):
        raise SealedBootstrapError(f"{label} is linked, writable, or not regular: {path}")
    return resolved


def _safe_inventory_path(root: Path, relative: str) -> Path:
    """Resolve a manifest path without accepting links or path escapes."""
    if (
        not relative
        or relative.startswith("/")
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise SealedBootstrapError(f"sealed inventory path is unsafe: {relative!r}")
    path = root.joinpath(*relative.split("/"))
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SealedBootstrapError(f"sealed inventory path is unavailable: {relative}") from exc
    if (
        path != resolved
        or path.is_symlink()
        or _is_reparse_point(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o222
    ):
        raise SealedBootstrapError(f"sealed inventory path is unsafe: {relative}")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SealedBootstrapError(f"sealed inventory path escapes root: {relative}") from exc
    return path


def _validate_manifest_shape(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or tuple(document) != MANIFEST_KEYS:
        raise SealedBootstrapError("sealed manifest top-level shape is invalid")
    if document["schema"] != SCHEMA:
        raise SealedBootstrapError("sealed manifest schema is unsupported")
    platform_name = document["platform"]
    if platform_name not in PACKAGE_KIND_BY_PLATFORM:
        raise SealedBootstrapError("sealed platform identity is invalid")
    if document["architecture"] not in {"arm64", "aarch64", "x86_64"}:
        raise SealedBootstrapError("sealed architecture identity is invalid")
    if document["python_version"] != "3.13.13":
        raise SealedBootstrapError("sealed Python version is unsupported")
    provenance = document["package_provenance"]
    if not isinstance(provenance, dict) or tuple(provenance) != (
        "kind",
        "package_id",
        "release_digest",
    ):
        raise SealedBootstrapError("sealed package provenance shape is invalid")
    if (
        provenance["kind"] != PACKAGE_KIND_BY_PLATFORM[platform_name]
        or provenance["package_id"] != "dev.tobkiri.launcher"
        or not _is_sha256_identity(provenance["release_digest"])
    ):
        raise SealedBootstrapError("sealed package provenance identity is invalid")
    sentinels = document["sentinels"]
    if not isinstance(sentinels, dict) or tuple(sentinels) != SENTINEL_KEYS:
        raise SealedBootstrapError("sealed sentinel shape is invalid")
    if not all(_is_sha256_identity(sentinels[key]) for key in SENTINEL_KEYS):
        raise SealedBootstrapError("sealed sentinel identity is invalid")
    if not _is_sha256_identity(document["environment_digest"]):
        raise SealedBootstrapError("sealed environment identity is invalid")
    files = document["files"]
    if not isinstance(files, list):
        raise SealedBootstrapError("sealed manifest files are not a list")
    for entry in files:
        if not isinstance(entry, dict) or "path" not in entry:
            raise SealedBootstrapError("sealed file entry shape is invalid")
    if files != sorted(files, key=lambda item: item["path"]):
        raise SealedBootstrapError("sealed manifest files are not sorted")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or tuple(entry) != FILE_KEYS:
            raise SealedBootstrapError("sealed file entry shape is invalid")
        path = entry["path"]
        if (
            not isinstance(path, str)
            or path == MANIFEST_NAME
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen
        ):
            raise SealedBootstrapError(f"sealed file path is unsafe: {path!r}")
        if type(entry["size"]) is not int or entry["size"] < 0:
            raise SealedBootstrapError(f"sealed file size is invalid: {path}")
        if not _is_sha256_identity(entry["sha256"]):
            raise SealedBootstrapError(f"sealed file digest is invalid: {path}")
        if not isinstance(entry["executable"], bool):
            raise SealedBootstrapError(f"sealed executable flag is invalid: {path}")
        seen.add(path)
    return document


def _expected_directories(files: Sequence[dict[str, Any]]) -> list[str]:
    """Return the one canonical directory domain implied by file inventory."""
    expected: set[str] = set()
    for entry in files:
        parent = Path(str(entry["path"])).parent
        while str(parent) not in {"", "."}:
            expected.add(parent.as_posix())
            parent = parent.parent
    return sorted(expected)


def _actual_tree(root: Path) -> tuple[list[str], list[str]]:
    """Inventory every regular file and directory, rejecting unsafe entries."""
    files: list[str] = []
    directories: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink() or _is_reparse_point(metadata):
            raise SealedBootstrapError(f"sealed tree contains a link: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_mode & 0o222:
                raise SealedBootstrapError(f"sealed directory is writable: {relative}")
            directories.append(relative)
        elif stat.S_ISREG(metadata.st_mode):
            if relative == MANIFEST_NAME:
                continue
            if metadata.st_nlink != 1 or metadata.st_mode & 0o222:
                raise SealedBootstrapError(f"sealed file identity is unsafe: {relative}")
            if any(part == "__pycache__" for part in relative.split("/")) or path.suffix in {
                ".pyc",
                ".pyo",
            }:
                raise SealedBootstrapError(f"sealed bytecode is not allowed: {relative}")
            files.append(relative)
        else:
            raise SealedBootstrapError(f"sealed tree contains a special file: {relative}")
    return sorted(files), sorted(directories)


def _executable(path: Path, platform_name: str) -> bool:
    return bool(path.stat().st_mode & 0o111) or (
        platform_name == "windows" and path.suffix.lower() in {".exe", ".com", ".bat", ".cmd"}
    )


def _verify_tree(root: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify exact files, bytes, permissions, links, and directory closure."""
    actual_files, actual_directories = _actual_tree(root)
    expected_files = sorted(
        [str(entry["path"]) for entry in document["files"]] + [RUNTIME_OVERLAY_NAME]
    )
    if actual_files != expected_files:
        raise SealedBootstrapError("sealed environment has missing or extra files")
    if actual_directories != _expected_directories(document["files"]):
        raise SealedBootstrapError("sealed environment has missing or extra directories")
    if DIRECTORY_MODES_NAME not in expected_files:
        raise SealedBootstrapError("sealed directory mode evidence is missing")
    records: list[dict[str, Any]] = []
    platform_name = str(document["platform"])
    for entry in document["files"]:
        path = _safe_inventory_path(root, str(entry["path"]))
        payload = path.read_bytes()
        actual = {
            "path": str(entry["path"]),
            "size": len(payload),
            "sha256": _sha256_bytes(payload),
            "executable": _executable(path, platform_name),
        }
        if actual != entry:
            raise SealedBootstrapError(f"sealed file changed: {entry['path']}")
        if platform_name != "windows":
            expected_mode = 0o555 if bool(entry["executable"]) else 0o444
            if stat.S_IMODE(path.lstat().st_mode) != expected_mode:
                raise SealedBootstrapError(f"sealed file mode changed: {entry['path']}")
        records.append(actual)
    try:
        directory_modes = json.loads((root / DIRECTORY_MODES_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedBootstrapError("sealed directory mode evidence is malformed") from exc
    expected_mode_entries = [
        {"path": ".", "mode": "0555"},
        *({"path": path, "mode": "0555"} for path in actual_directories),
    ]
    if directory_modes != {
        "schema": DIRECTORY_MODES_SCHEMA,
        "directories": expected_mode_entries,
    }:
        raise SealedBootstrapError("sealed directory mode evidence is invalid")
    if platform_name != "windows":
        for entry in expected_mode_entries:
            relative = str(entry["path"])
            path = root if relative == "." else root / relative
            if stat.S_IMODE(path.lstat().st_mode) != 0o555:
                raise SealedBootstrapError(f"sealed directory mode changed: {relative}")
    compact = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if _sha256_bytes(compact) != document["environment_digest"]:
        raise SealedBootstrapError("sealed environment digest changed")
    return records


def _verify_runtime_overlay(
    root: Path,
    sealed_document: dict[str, Any],
    runtime_overlay_sha256: str,
    outer_runtime_manifest_sha256: str,
) -> dict[str, str]:
    """Verify the single Host overlay separately from the sealed base domain."""
    if not _is_sha256_identity(runtime_overlay_sha256) or not _is_sha256_identity(
        outer_runtime_manifest_sha256
    ):
        raise SealedBootstrapError("runtime overlay launch binding is invalid")
    path = root / RUNTIME_OVERLAY_NAME
    _assert_regular_file(path, "sealed runtime overlay")
    metadata = path.lstat()
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o444:
        raise SealedBootstrapError("sealed runtime overlay mode is invalid")
    raw = path.read_bytes()
    if _sha256_bytes(raw) != runtime_overlay_sha256:
        raise SealedBootstrapError("sealed runtime overlay digest changed")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedBootstrapError("sealed runtime overlay is malformed") from exc
    if not isinstance(document, dict) or tuple(document) != (
        "schema",
        "overlay",
        "entries",
    ):
        raise SealedBootstrapError("sealed runtime overlay shape is invalid")
    if document["schema"] != "io.tobkiri.runtime-resource-manifest.v1":
        raise SealedBootstrapError("sealed runtime overlay resource schema is invalid")
    authority = document["overlay"]
    if not isinstance(authority, dict) or tuple(authority) != (
        "schema",
        "outer_manifest_sha256",
        "sealed_manifest_sha256",
    ):
        raise SealedBootstrapError("sealed runtime overlay authority is invalid")
    sealed_manifest_sha256 = _sha256_bytes((root / MANIFEST_NAME).read_bytes())
    if authority != {
        "schema": RUNTIME_OVERLAY_SCHEMA,
        "outer_manifest_sha256": outer_runtime_manifest_sha256,
        "sealed_manifest_sha256": sealed_manifest_sha256,
    }:
        raise SealedBootstrapError("sealed runtime overlay authority changed")
    expected_entries = [
        {
            "path": str(entry["path"])[len("app/") :],
            "size": entry["size"],
            "sha256": entry["sha256"],
        }
        for entry in sealed_document["files"]
        if str(entry["path"]).startswith("app/")
    ]
    if not expected_entries or document["entries"] != expected_entries:
        raise SealedBootstrapError(
            "sealed runtime overlay does not exactly project the application closure"
        )
    return {
        "runtime_overlay_sha256": runtime_overlay_sha256,
        "outer_runtime_manifest_sha256": outer_runtime_manifest_sha256,
    }


def _read_bound_regular_bytes(path: Path, label: str, maximum_size: int) -> bytes:
    """Read one digest-bound regular file while detecting path replacement."""

    if not isinstance(maximum_size, int) or maximum_size < 1:
        raise SealedBootstrapError("sealed bundle read bound is invalid")
    _assert_regular_file(path, label, require_immutable=False)
    try:
        named_before = path.lstat()
        if named_before.st_size > maximum_size:
            raise SealedBootstrapError(f"{label} exceeds its read bound")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except SealedBootstrapError:
        raise
    except OSError as exc:
        raise SealedBootstrapError(f"{label} is unavailable") from exc
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or opened_before.st_size > maximum_size
            or _file_identity(opened_before) != _file_identity(named_before)
        ):
            raise SealedBootstrapError(f"{label} identity changed before read")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            payload = stream.read(maximum_size + 1)
            opened_after = os.fstat(stream.fileno())
        descriptor = -1
        named_after = path.lstat()
    except SealedBootstrapError:
        raise
    except OSError as exc:
        raise SealedBootstrapError(f"{label} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        len(payload) > maximum_size
        or _file_identity(opened_before) != _file_identity(opened_after)
        or _file_identity(opened_before) != _file_identity(named_after)
    ):
        raise SealedBootstrapError(f"{label} changed while read")
    return payload


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return stable metadata needed to detect a manifest path replacement."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _verify_packvm_bundle_binding(
    application_bundle_root: str,
    provisioning_sha256: str,
    helper_manifest_sha256: str,
    helper_team_id: str,
    outer_runtime_manifest_sha256: str,
) -> Mapping[str, str] | None:
    """Validate the Launcher-authenticated macOS app PackVM resource domain.

    The sealed snapshot is deliberately separate from the signed application
    bundle.  Only the Launcher can supply this four-part binding, and it is
    accepted only for the exact ``.app/Contents/Resources/app`` layout whose
    outer runtime manifest is already bound by the sealed overlay.
    """

    supplied = (
        application_bundle_root,
        provisioning_sha256,
        helper_manifest_sha256,
        helper_team_id,
    )
    if not all(isinstance(value, str) for value in supplied):
        raise SealedBootstrapError("sealed PackVM bundle launch binding is invalid")
    if all(value == "" for value in supplied):
        return None
    if not application_bundle_root or not all(
        _is_sha256_identity(value) for value in (provisioning_sha256, helper_manifest_sha256)
    ):
        raise SealedBootstrapError("sealed PackVM bundle launch binding is invalid")
    if helper_team_id and _APPLE_TEAM_ID.fullmatch(helper_team_id) is None:
        raise SealedBootstrapError("sealed PackVM helper team identity is invalid")
    if not _is_sha256_identity(outer_runtime_manifest_sha256):
        raise SealedBootstrapError("sealed outer runtime manifest binding is invalid")

    bundle = Path(application_bundle_root)
    if not bundle.is_absolute() or bundle.suffix != ".app":
        raise SealedBootstrapError("sealed application bundle root is invalid")
    bundle = _assert_real_directory(
        bundle,
        "sealed application bundle root",
        require_immutable=False,
    )
    contents = _assert_real_directory(
        bundle / "Contents",
        "sealed application contents",
        require_immutable=False,
    )
    resources = _assert_real_directory(
        contents / "Resources",
        "sealed application resources",
        require_immutable=False,
    )
    application = _assert_real_directory(
        resources / "app",
        "sealed application resource root",
        require_immutable=False,
    )
    if contents.parent != bundle or resources.parent != contents or application.parent != resources:
        raise SealedBootstrapError("sealed application bundle layout is invalid")

    provisioning = _read_bound_regular_bytes(
        resources / PACKVM_PROVISIONING_MANIFEST_NAME,
        "sealed PackVM provisioning manifest",
        2 * 1024 * 1024,
    )
    helper_manifest = _read_bound_regular_bytes(
        resources / PACKVM_HELPER_MANIFEST_NAME,
        "sealed PackVM helper manifest",
        256 * 1024,
    )
    outer_manifest = _read_bound_regular_bytes(
        application / OUTER_RUNTIME_MANIFEST_NAME,
        "sealed outer runtime manifest",
        4 * 1024 * 1024,
    )
    if (
        _sha256_bytes(provisioning) != provisioning_sha256
        or _sha256_bytes(helper_manifest) != helper_manifest_sha256
        or _sha256_bytes(outer_manifest) != outer_runtime_manifest_sha256
    ):
        raise SealedBootstrapError("sealed PackVM bundle binding changed")
    return MappingProxyType(
        {
            "root": str(bundle),
            "provisioning_sha256": f"sha256:{provisioning_sha256}",
            "helper_manifest_sha256": f"sha256:{helper_manifest_sha256}",
            "helper_team_id": helper_team_id,
        }
    )


def _group_digest(entries: Sequence[dict[str, Any]]) -> str:
    payload = b"".join(f"{entry['path']}\0{entry['sha256']}\n".encode("utf-8") for entry in entries)
    if not payload:
        raise SealedBootstrapError("sealed sentinel group is empty")
    return _sha256_bytes(payload)


def _recomputed_sentinels(
    document: dict[str, Any], records: Sequence[dict[str, Any]]
) -> dict[str, str]:
    """Recompute the three sentinel groups from verified inventory bytes."""
    python_version = document["python_version"]
    if not isinstance(python_version, str):
        raise SealedBootstrapError("sealed Python version is malformed")
    minor = ".".join(python_version.split(".")[:2])
    stdlib_prefixes = (
        f"runtime/lib/python{minor}/",
        f"runtime/Lib/python{minor}/",
        "runtime/Lib/",
    )
    site_prefixes = (
        f"venv/lib/python{minor}/site-packages/",
        "venv/Lib/site-packages/",
    )
    stdlib = [entry for entry in records if str(entry["path"]).startswith(stdlib_prefixes)]
    site_packages = [entry for entry in records if str(entry["path"]).startswith(site_prefixes)]
    native_suffixes = (".so", ".dylib", ".dll", ".pyd", ".exe")
    native = [
        entry
        for entry in records
        if str(entry["path"]).lower().endswith(native_suffixes) or bool(entry["executable"])
    ]
    return {
        "stdlib_sha256": _group_digest(stdlib),
        "site_packages_sha256": _group_digest(site_packages),
        "native_sha256": _group_digest(native),
    }


def _sentinels_match(
    root: Path, document: dict[str, Any], records: Sequence[dict[str, Any]]
) -> dict[str, str]:
    actual = _recomputed_sentinels(document, records)
    if document["sentinels"] != actual:
        raise SealedBootstrapError("sealed sentinel recomputation does not match manifest")
    for key, filename in SENTINEL_FILENAMES.items():
        path = _safe_inventory_path(root, f"sentinels/{filename}")
        if path.read_text(encoding="utf-8") != actual[key] + "\n":
            raise SealedBootstrapError(f"sealed sentinel marker changed: {path}")
    return actual


def _environment_root(value: str) -> Path:
    root = _assert_real_directory(Path(value), "sealed environment root")
    try:
        prefix = Path(sys.prefix).resolve(strict=True)
        base_prefix = Path(sys.base_prefix).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SealedBootstrapError("Python prefix contains an unsafe path") from exc
    if prefix != root / "venv" or base_prefix != root / "runtime":
        raise SealedBootstrapError("Python prefix is not bound to the supplied environment root")
    return root


def _load_manifest(root: Path, value: str) -> dict[str, Any]:
    expected = root / MANIFEST_NAME
    supplied = Path(value)
    try:
        supplied_resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SealedBootstrapError("manifest path is unavailable") from exc
    if not supplied.is_absolute() or supplied_resolved != expected:
        raise SealedBootstrapError("manifest path is not bound to the environment root")
    _assert_regular_file(supplied, "sealed manifest")
    raw = supplied.read_bytes()
    expected_binding = os.environ.get(MANIFEST_SHA_ENV, "")
    if expected_binding and (
        not _is_sha256_identity(expected_binding) or _sha256_bytes(raw) != expected_binding
    ):
        raise SealedBootstrapError("sealed Python manifest binding changed")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealedBootstrapError("sealed manifest is not valid UTF-8 JSON") from exc
    return _validate_manifest_shape(document)


def _new_dispatch_scope(
    root: Path,
    manifest: dict[str, Any],
    role: str,
    packvm_bundle_binding: Mapping[str, str] | None,
) -> _SealedDispatchScope:
    """Create the process-private capability for one verified role target."""
    manifest_path = root / MANIFEST_NAME
    try:
        manifest_digest = _sha256_bytes(manifest_path.read_bytes())
    except OSError as exc:
        raise SealedBootstrapError("sealed manifest binding is unavailable") from exc
    return _SealedDispatchScope(
        _SCOPE_CONSTRUCTOR_TOKEN,
        root,
        manifest_path,
        manifest_digest,
        str(manifest["environment_digest"]),
        ROLE_TARGETS[role],
        packvm_bundle_binding,
    )


def _attestation_destination(path_value: str, root: Path, nonce: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute() or path.name != f"startup-{nonce}.json":
        raise SealedBootstrapError("attestation path is not a fixed nonce-bound filename")
    parent = _assert_real_directory(
        path.parent,
        "attestation directory",
        require_immutable=False,
    )
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SealedBootstrapError("attestation path is unavailable") from exc
    for forbidden_root in (root, root.parent):
        try:
            canonical.relative_to(forbidden_root)
        except ValueError:
            continue
        raise SealedBootstrapError(
            "attestation path may not be inside sealed application resources"
        )
    if path.parent != parent:
        raise SealedBootstrapError("attestation path contains a linked parent")
    if path.exists() or path.is_symlink():
        raise SealedBootstrapError("attestation destination already exists")
    return path


def _publish_attestation(path: Path, evidence: dict[str, Any]) -> None:
    """Write, fsync, and atomically publish a nonce-bound attestation."""
    payload = (json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    parent_metadata = path.parent.lstat()
    parent_identity = _attestation_parent_identity(parent_metadata)
    published_descriptor = -1
    directory_descriptor = -1
    temporary_identity: tuple[int, int, int, int, int] | None = None
    try:
        if os.name != "nt":
            directory_descriptor = os.open(
                path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            if _attestation_parent_identity(os.fstat(directory_descriptor)) != parent_identity:
                raise SealedBootstrapError("attestation parent identity changed before publication")
        try:
            descriptor = (
                os.open(temporary.name, flags, 0o600, dir_fd=directory_descriptor)
                if directory_descriptor >= 0
                else os.open(temporary, flags, 0o600)
            )
        except FileExistsError as exc:
            raise SealedBootstrapError("attestation temporary destination already exists") from exc
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_identity = _attestation_file_identity(os.fstat(handle.fileno()))
        _validate_published_attestation_metadata(
            _publication_lstat(temporary, directory_descriptor),
            path.parent,
            expected_links=1,
        )
        if _publication_exists(path, directory_descriptor):
            raise SealedBootstrapError("attestation destination appeared during publish")
        try:
            if directory_descriptor >= 0:
                os.link(
                    temporary.name,
                    path.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            else:
                os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise SealedBootstrapError("attestation destination appeared during publish") from exc
        published_descriptor = os.open(
            path.name if directory_descriptor >= 0 else path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            **({"dir_fd": directory_descriptor} if directory_descriptor >= 0 else {}),
        )
        linked_metadata = os.fstat(published_descriptor)
        _validate_published_attestation_metadata(
            linked_metadata,
            path.parent,
            expected_links=2,
        )
        if (
            _attestation_file_identity(linked_metadata) != temporary_identity
            or _attestation_file_identity(_publication_lstat(path, directory_descriptor))
            != temporary_identity
            or _attestation_file_identity(_publication_lstat(temporary, directory_descriptor))
            != temporary_identity
        ):
            raise SealedBootstrapError("published attestation identity changed during publication")
        # os.replace would permit replacement of a target that appeared during
        # publication. The atomic link is no-replace; unlinking the temporary
        # name is the completion boundary observed by Host readers.
        _publication_unlink_owned(
            temporary,
            directory_descriptor,
            temporary_identity,
        )
        if directory_descriptor >= 0:
            os.fsync(directory_descriptor)
        final_metadata = os.fstat(published_descriptor)
        _validate_published_attestation_metadata(
            final_metadata,
            path.parent,
            expected_links=1,
        )
        if (
            _attestation_file_identity(final_metadata) != temporary_identity
            or _attestation_file_identity(_publication_lstat(path, directory_descriptor))
            != temporary_identity
            or _attestation_parent_identity(path.parent.lstat()) != parent_identity
            or (
                directory_descriptor >= 0
                and _attestation_parent_identity(os.fstat(directory_descriptor)) != parent_identity
            )
        ):
            raise SealedBootstrapError("published attestation identity changed after publication")
    finally:
        try:
            if published_descriptor >= 0:
                os.close(published_descriptor)
            if temporary_identity is not None:
                _publication_unlink_owned(
                    temporary,
                    directory_descriptor,
                    temporary_identity,
                    missing_ok=True,
                )
        finally:
            if directory_descriptor >= 0:
                os.close(directory_descriptor)


def _attestation_file_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int]:
    """Return the stable fields binding one attestation path to its inode."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_mode,
    )


def _publication_lstat(path: Path, directory_descriptor: int) -> os.stat_result:
    """Stat one publication name through the held parent when supported."""
    if directory_descriptor >= 0:
        return os.stat(
            path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    return path.lstat()


def _publication_exists(path: Path, directory_descriptor: int) -> bool:
    """Check one no-follow publication name without trusting a resolved path."""
    try:
        _publication_lstat(path, directory_descriptor)
    except FileNotFoundError:
        return False
    return True


def _publication_unlink_owned(
    path: Path,
    directory_descriptor: int,
    expected_identity: tuple[int, int, int, int, int],
    *,
    missing_ok: bool = False,
) -> None:
    """Unlink only the exact temporary inode created by this publisher."""
    try:
        metadata = _publication_lstat(path, directory_descriptor)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if _attestation_file_identity(metadata) != expected_identity:
        raise SealedBootstrapError("attestation temporary identity changed before cleanup")
    if directory_descriptor >= 0:
        os.unlink(path.name, dir_fd=directory_descriptor)
    else:
        path.unlink()


def _attestation_parent_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int | None]:
    """Bind the parent without treating expected directory mtime changes as swaps."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid if hasattr(metadata, "st_uid") else None,
    )


def _validate_published_attestation_metadata(
    metadata: os.stat_result,
    parent: Path,
    *,
    expected_links: int,
) -> None:
    """Validate one handle- or no-follow-stat view of the publication inode."""
    parent_metadata = parent.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != expected_links
        or metadata.st_dev != parent_metadata.st_dev
        or (
            hasattr(os, "geteuid")
            and hasattr(metadata, "st_uid")
            and metadata.st_uid != os.geteuid()
        )
        or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600)
    ):
        raise SealedBootstrapError("published attestation identity is invalid")


def _sys_path_contract(
    root: Path,
    document: dict[str, Any],
    *,
    include_application: bool,
    application_import_roots: Sequence[str] = (),
) -> tuple[list[str], str | None]:
    """Return exact manifest-bound import roots and the optional zip spelling."""
    major, minor, *_ = str(document["python_version"]).split(".")
    compact = f"{major}{minor}"
    if document["platform"] == "windows":
        zip_relative = f"runtime/python{compact}.zip"
        directories = (
            "runtime",
            "runtime/Lib",
            "runtime/DLLs",
            "venv/Lib/site-packages",
        )
    else:
        zip_relative = f"runtime/lib/python{compact}.zip"
        directories = (
            f"runtime/lib/python{major}.{minor}",
            f"runtime/lib/python{major}.{minor}/lib-dynload",
            f"venv/lib/python{major}.{minor}/site-packages",
        )
    if include_application:
        directories = (*directories, "app", *application_import_roots)
    elif application_import_roots:
        raise SealedBootstrapError("role import roots require the sealed application import domain")

    manifest_files = {str(entry["path"]) for entry in document["files"]}
    manifest_directories = set(_expected_directories(document["files"]))
    if any(relative not in manifest_directories for relative in directories):
        raise SealedBootstrapError("sealed manifest omits a required import root")

    expected: list[str] = []
    zip_is_manifested = zip_relative in manifest_files
    if zip_is_manifested:
        zip_path = root / zip_relative
        zip_metadata = zip_path.lstat()
        if (
            zip_path.is_symlink()
            or _is_reparse_point(zip_metadata)
            or not stat.S_ISREG(zip_metadata.st_mode)
            or zip_metadata.st_nlink != 1
            or zip_metadata.st_mode & 0o222
        ):
            raise SealedBootstrapError("sealed runtime zip identity is invalid")
        expected.append(str(zip_path.resolve(strict=True)))
    expected.extend(str((root / relative).resolve(strict=True)) for relative in directories)
    return expected, None if zip_is_manifested else str(root / zip_relative)


def _canonical_sys_path_entry(root: Path, value: object) -> str:
    """Normalize one import path and require a direct path inside the snapshot."""
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise SealedBootstrapError("isolated Python sys.path contains an empty entry")
    candidate = Path(os.fspath(value))
    if not candidate.is_absolute():
        raise SealedBootstrapError("isolated Python sys.path contains a relative entry")
    try:
        canonical = candidate.resolve(strict=True)
        canonical.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SealedBootstrapError(
            f"isolated Python sys.path escaped the sealed root: {value}"
        ) from exc
    if candidate != canonical or candidate.is_symlink():
        raise SealedBootstrapError("isolated Python sys.path contains a linked entry")
    return str(canonical)


def _normalize_sys_path(
    root: Path,
    document: dict[str, Any],
    *,
    include_application: bool,
    application_import_roots: Sequence[str] = (),
) -> list[str]:
    """Require exactly the manifest-bound import roots in their isolated order."""
    expected, absent_zip = _sys_path_contract(
        root,
        document,
        include_application=include_application,
        application_import_roots=application_import_roots,
    )
    snapshot: list[str] = []
    for item in sys.path:
        if (
            absent_zip is not None
            and isinstance(item, (str, os.PathLike))
            and os.fspath(item) == absent_zip
            and not Path(absent_zip).exists()
            and not Path(absent_zip).is_symlink()
        ):
            continue
        snapshot.append(_canonical_sys_path_entry(root, item))
    if len(snapshot) != len(set(snapshot)):
        raise SealedBootstrapError("isolated Python sys.path contains a duplicate entry")
    if set(snapshot) != set(expected):
        raise SealedBootstrapError("isolated Python sys.path differs from sealed manifest")
    sys.path[:] = snapshot
    return snapshot


class _SealedSysPath(list[str]):
    """Keep later imports inside the already-verified snapshot."""

    def __init__(self, root: Path, values: Sequence[str]) -> None:
        self._root = root
        self._frozen = False
        super().__init__(values)

    def freeze(self) -> None:
        """Prevent the dispatched role from changing the attested path."""
        self._frozen = True

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise SealedBootstrapError("sealed sys.path changed after attestation")

    def _entry(self, value: object) -> str:
        return _canonical_sys_path_entry(self._root, value)

    def insert(self, index: int, value: str) -> None:
        self._ensure_mutable()
        super().insert(index, self._entry(value))

    def append(self, value: str) -> None:
        self._ensure_mutable()
        super().append(self._entry(value))

    def extend(self, values: Sequence[str]) -> None:
        self._ensure_mutable()
        super().extend(self._entry(value) for value in values)

    def __setitem__(self, index, value) -> None:
        self._ensure_mutable()
        if isinstance(index, slice):
            value = [self._entry(item) for item in value]
        else:
            value = self._entry(value)
        super().__setitem__(index, value)

    def __delitem__(self, index) -> None:
        self._ensure_mutable()
        super().__delitem__(index)

    def __iadd__(self, values: Sequence[str]):
        self._ensure_mutable()
        self.extend(values)
        return self

    def __imul__(self, value: int):
        self._ensure_mutable()
        return super().__imul__(value)

    def clear(self) -> None:
        self._ensure_mutable()
        super().clear()

    def pop(self, index: int = -1) -> str:
        self._ensure_mutable()
        return super().pop(index)

    def remove(self, value: str) -> None:
        self._ensure_mutable()
        super().remove(value)

    def reverse(self) -> None:
        self._ensure_mutable()
        super().reverse()

    def sort(self, *args: Any, **kwargs: Any) -> None:
        self._ensure_mutable()
        super().sort(*args, **kwargs)


def _validate_python_identity(root: Path) -> tuple[str, str, str]:
    """Validate executable and CPython prefixes against the sealed root."""
    try:
        executable = Path(sys.executable).resolve(strict=True)
        prefix = Path(sys.prefix).resolve(strict=True)
        base_prefix = Path(sys.base_prefix).resolve(strict=True)
        executable.relative_to(root)
        prefix.relative_to(root)
        base_prefix.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SealedBootstrapError("Python identity escaped the sealed root") from exc
    if prefix != root / "venv" or base_prefix != root / "runtime":
        raise SealedBootstrapError("Python prefixes are not bound to the sealed layout")
    if not executable.is_file() or executable.is_symlink():
        raise SealedBootstrapError("Python executable is not a sealed regular file")
    return str(executable), str(prefix), str(base_prefix)


def _reject_launch_environment_injection() -> None:
    """Reject inherited path and native-loader injection before role loading."""
    offenders = sorted(
        key
        for key in os.environ
        if key in FORBIDDEN_LAUNCH_ENVIRONMENTS or key.startswith("DYLD_") or key.startswith("LD_")
    )
    if offenders:
        raise SealedBootstrapError(
            "sealed launch environment contains forbidden injection keys: " + ", ".join(offenders)
        )


def _validate_runtime_state(
    root: Path,
    document: dict[str, Any],
    *,
    include_application: bool,
    application_import_roots: Sequence[str] = (),
) -> list[str]:
    """Validate prefixes, native import roots, and canonical sys.path."""
    _validate_python_identity(root)
    return _normalize_sys_path(
        root,
        document,
        include_application=include_application,
        application_import_roots=application_import_roots,
    )


def _validate_post_dispatch_state(
    root: Path,
    expected_sys_path: Sequence[str],
    expected_object: _SealedSysPath,
) -> None:
    """Require the attested import path to remain unchanged through dispatch."""
    _validate_python_identity(root)
    if sys.path is not expected_object:
        raise SealedBootstrapError("sealed sys.path object was replaced after attestation")
    actual = [_canonical_sys_path_entry(root, item) for item in sys.path]
    if actual != list(expected_sys_path):
        raise SealedBootstrapError("sealed sys.path changed after attestation")


def _attestation(
    root: Path,
    role: str,
    nonce: str,
    document: dict[str, Any],
    sentinels: dict[str, str],
    sys_path: Sequence[str],
    overlay_binding: dict[str, str],
) -> dict[str, Any]:
    executable, prefix, base_prefix = _validate_python_identity(root)
    return {
        "schema": ATTESTATION_SCHEMA,
        "nonce": nonce,
        "role": role,
        "environment_digest": document["environment_digest"],
        "executable": str(executable),
        "prefix": str(prefix),
        "base_prefix": str(base_prefix),
        "sys_path": list(sys_path),
        "stdlib_sha256": sentinels["stdlib_sha256"],
        "site_packages_sha256": sentinels["site_packages_sha256"],
        "native_sha256": sentinels["native_sha256"],
        "runtime_overlay_sha256": overlay_binding["runtime_overlay_sha256"],
        "outer_runtime_manifest_sha256": overlay_binding["outer_runtime_manifest_sha256"],
        "lifetime_lease": True,
    }


class _LifetimeLease:
    """Hold a shared OS lock on ``lease.v1`` until the role exits."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "_LifetimeLease":
        _assert_regular_file(self.path, "sealed lifetime lease")
        handle = self.path.open("rb")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_RLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        except Exception:
            handle.close()
            raise
        self.handle = handle
        return self

    def __exit__(self, _exception_type, _exception, _traceback) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _load_role(root: Path, role: str):
    """Load one fixed wrapper from the sealed app subtree."""
    path = root / "app" / ROLE_ENTRYPOINTS[role]
    _assert_regular_file(path, f"sealed {role} role entrypoint")
    app_root = _assert_real_directory(root / "app", "sealed application root")
    target = app_root.joinpath(*ROLE_TARGETS[role])
    target = _assert_regular_file(
        target,
        f"canonical {role} target",
    )
    try:
        target.relative_to(app_root)
    except (OSError, ValueError) as exc:
        raise SealedBootstrapError(f"canonical {role} target escaped the app root") from exc
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    spec = importlib.util.spec_from_file_location(
        f"tobkiri_sealed_role_{role.replace('-', '_')}",
        path,
    )
    if spec is None or spec.loader is None:
        raise SealedBootstrapError(f"sealed role entrypoint is not importable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, target


def _prepare_role(module: Any, scope: _SealedDispatchScope) -> Any:
    """Load the role target and perform its import-path preflight."""
    prepare = getattr(module, "prepare_for_dispatch", None)
    if not callable(prepare):
        raise SealedBootstrapError("sealed role wrapper lacks dispatch preparation")
    main = prepare(scope)
    if not callable(main):
        raise SealedBootstrapError("sealed role wrapper returned a non-callable main")
    return main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tobkiri sealed Python bootstrap")
    parser.add_argument(
        "--role",
        choices=tuple(ROLE_ENTRYPOINTS),
        required=True,
    )
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--attestation", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--environment-root", required=True)
    parser.add_argument("--runtime-overlay-sha256", required=True)
    parser.add_argument("--outer-runtime-manifest-sha256", required=True)
    parser.add_argument("--application-bundle-root", required=True)
    parser.add_argument("--packvm-provisioning-sha256", required=True)
    parser.add_argument("--packvm-helper-manifest-sha256", required=True)
    parser.add_argument("--packvm-helper-team-id", required=True)
    return parser


def _split_arguments(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    values = list(argv)
    try:
        separator = values.index("--")
    except ValueError as exc:
        raise SealedBootstrapError("bootstrap requires -- before role arguments") from exc
    return values[:separator], values[separator + 1 :]


def main(argv: Sequence[str] | None = None) -> int:
    """Preload and verify one role, publish attestation, then dispatch it."""
    bootstrap_args, role_args = _split_arguments(list(argv) if argv is not None else sys.argv[1:])
    args = _parser().parse_args(bootstrap_args)
    if len(args.nonce) != 64 or any(
        character not in "0123456789abcdef" for character in args.nonce
    ):
        raise SealedBootstrapError("nonce must be the parent-provided 64-hex identity")
    _reject_launch_environment_injection()
    root = _environment_root(args.environment_root)
    manifest = _load_manifest(root, args.manifest)
    attestation_path = _attestation_destination(args.attestation, root, args.nonce)
    with _LifetimeLease(root / LEASE_NAME):
        overlay_binding = _verify_runtime_overlay(
            root,
            manifest,
            args.runtime_overlay_sha256,
            args.outer_runtime_manifest_sha256,
        )
        packvm_bundle_binding = _verify_packvm_bundle_binding(
            args.application_bundle_root,
            args.packvm_provisioning_sha256,
            args.packvm_helper_manifest_sha256,
            args.packvm_helper_team_id,
            args.outer_runtime_manifest_sha256,
        )
        records = _verify_tree(root, manifest)
        sentinels = _sentinels_match(root, manifest, records)
        _validate_runtime_state(root, manifest, include_application=False)
        scope = _new_dispatch_scope(
            root,
            manifest,
            args.role,
            packvm_bundle_binding,
        )
        role_module, target = _load_role(root, args.role)
        role_main = _prepare_role(role_module, scope)
        sys_path = _validate_runtime_state(
            root,
            manifest,
            include_application=True,
            application_import_roots=ROLE_APPLICATION_IMPORT_ROOTS[args.role],
        )
        sealed_sys_path = _SealedSysPath(root, sys_path)
        sys.path = sealed_sys_path
        if (
            _verify_runtime_overlay(
                root,
                manifest,
                args.runtime_overlay_sha256,
                args.outer_runtime_manifest_sha256,
            )
            != overlay_binding
        ):
            raise SealedBootstrapError("sealed runtime overlay changed before attestation")
        evidence = _attestation(
            root,
            args.role,
            args.nonce,
            manifest,
            sentinels,
            sys_path,
            overlay_binding,
        )
        _publish_attestation(attestation_path, evidence)
        sealed_sys_path.freeze()
        sys.argv = [str(target), *role_args]
        if args.role == "host_helper":
            result = int(role_main())
        else:
            result = int(role_main(role_args))
        _validate_post_dispatch_state(root, sys_path, sealed_sys_path)
        return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SealedBootstrapError, OSError, ValueError) as exc:
        print(f"sealed Python bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
