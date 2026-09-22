#!/usr/bin/env python3
"""Headlessly verify a packaged Launcher presentation catalog and IPC surface."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import plistlib
import signal
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

try:
    from tobkiri_launcher.scripts.artifact_integrity import artifact_digest_and_size
except ModuleNotFoundError:
    from artifact_integrity import artifact_digest_and_size  # type: ignore[no-redef]

try:
    from tobkiri_protocol.defaultspack_bundle_order import (  # type: ignore[import-not-found]
        canonical_defaultspack_bundle_entries,
    )
except ModuleNotFoundError:
    _RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "tobkiri_runtime"
    if str(_RUNTIME_ROOT) not in sys.path:
        sys.path.insert(0, str(_RUNTIME_ROOT))
    from tobkiri_protocol.defaultspack_bundle_order import (
        canonical_defaultspack_bundle_entries,
    )

CATALOG_SCHEMA = "io.tobkiri.launcher.presentation-catalog.v1"
SHELL_CONTRACT = "app.shell.v1"
PRESENTATION_COMMANDS = (
    "get_presentation_catalog",
    "select_presentation",
    "launch_selected_presentation",
)
PRESENTATION_PERMISSIONS = (
    "allow-get-presentation-catalog",
    "allow-select-presentation",
    "allow-launch-selected-presentation",
)
RELEASE_SCHEMA = "io.tobkiri.shell.release.v4"
VALID_TARGETS = {
    ("macos", "arm64"),
    ("macos", "x86_64"),
    ("windows", "x86_64"),
    ("linux", "x86_64"),
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the package harness arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app",
        type=Path,
        required=True,
        help="Path to the packaged Tobkiri Launcher.app.",
    )
    parser.add_argument(
        "--launch-seconds",
        type=float,
        default=4.0,
        help="How long to keep the packaged binary running (default: 4 seconds).",
    )
    return parser.parse_args(argv)


def resource_root(app: Path) -> Path:
    """Return the runtime resource root inside a packaged app."""
    resolved = app.expanduser().resolve()
    if resolved.name == "app" and resolved.parent.name == "Resources":
        return resolved
    if resolved.suffix != ".app":
        raise RuntimeError(f"expected a .app bundle or Resources/app path: {app}")
    return resolved / "Contents" / "Resources" / "app"


def load_catalog(path: Path) -> dict[str, Any]:
    """Load and validate the package's manifest-derived catalog JSON."""
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"failed to read packaged presentation catalog {path}: {error}"
        ) from error
    if not isinstance(catalog, dict):
        raise RuntimeError(f"packaged presentation catalog is not an object: {path}")
    if catalog.get("schema") != CATALOG_SCHEMA:
        raise RuntimeError(f"unexpected packaged presentation catalog schema: {path}")
    return catalog


def _byte_digest(contents: bytes) -> str:
    return "sha256:" + hashlib.sha256(contents).hexdigest()


def _canonical_digest(value: Any) -> str:
    contents = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _byte_digest(contents)


def _verify_defaultspack_bundle(entries: object, bundle_root: Path) -> None:
    """Verify the signed v4 lock order, bytes, and Pack-sidecar bindings."""
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Defaults bundle lock entries are missing")
    try:
        canonical_entries = canonical_defaultspack_bundle_entries(entries)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Defaults bundle lock entry contract failed: {error}") from error
    if entries != canonical_entries:
        raise RuntimeError("Defaults bundle lock order is not canonical")
    if bundle_root.is_symlink() or not bundle_root.is_dir():
        raise RuntimeError("Defaults bundle root is missing or symlinked")

    lock_paths = {str(entry["path"]) for entry in entries}
    actual_paths: set[str] = set()
    for candidate in bundle_root.rglob("*"):
        relative = candidate.relative_to(bundle_root).as_posix()
        if candidate.is_symlink():
            raise RuntimeError(f"Defaults bundle artifact is symlinked: {relative}")
        if candidate.is_file():
            if relative != "bundle.lock.json":
                actual_paths.add(relative)
    if actual_paths != lock_paths:
        missing = sorted(lock_paths - actual_paths)
        extra = sorted(actual_paths - lock_paths)
        raise RuntimeError(
            f"Defaults bundle file set mismatch; missing={missing}, extra={extra}"
        )

    documents: dict[tuple[str, str], dict[str, Any]] = {}
    pack_sidecars: dict[str, str] = {}
    catalog_entries: dict[str, tuple[dict[str, Any], str]] = {}
    for entry in entries:
        relative = str(entry["path"])
        path = _safe_resource_path(bundle_root, relative, "Defaults bundle entry")
        raw = _regular_bytes(path, "Defaults bundle entry")
        if _byte_digest(raw) != entry["digest"]:
            raise RuntimeError(f"Defaults bundle digest mismatch: {relative}")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Defaults bundle document is malformed: {relative}") from error
        if not isinstance(document, dict):
            raise RuntimeError(f"Defaults bundle document is not an object: {relative}")
        kind = str(entry["kind"])
        if kind == "pack":
            pack = document.get("pack")
            pack_id = pack.get("id") if isinstance(pack, dict) else None
            if not isinstance(pack_id, str) or not pack_id:
                raise RuntimeError(f"Defaults bundle Pack identity is missing: {relative}")
            identity_key = (kind, pack_id)
            if identity_key in documents:
                raise RuntimeError(f"duplicate Defaults bundle Pack identity: {pack_id}")
            documents[identity_key] = document
            artifacts = document.get("artifacts")
            if not isinstance(artifacts, list):
                raise RuntimeError(f"Defaults bundle Pack artifacts are missing: {relative}")
            sidecars = [
                artifact
                for artifact in artifacts
                if isinstance(artifact, dict)
                and artifact.get("kind") == "sidecar"
                and artifact.get("path") == "executables.v4.json"
            ]
            if len(sidecars) > 1:
                raise RuntimeError(f"Pack pins duplicate executable catalogs: {pack_id}")
            if sidecars:
                sidecar_digest = sidecars[0].get("digest")
                if not isinstance(sidecar_digest, str):
                    raise RuntimeError(f"Pack executable catalog digest is missing: {pack_id}")
                pack_sidecars[pack_id] = sidecar_digest
        elif kind == "executable_catalog":
            pack_id = document.get("pack_id")
            if not isinstance(pack_id, str) or not pack_id:
                raise RuntimeError(f"executable catalog identity is missing: {relative}")
            identity_key = (kind, pack_id)
            if identity_key in documents:
                raise RuntimeError(f"duplicate executable catalog identity: {pack_id}")
            documents[identity_key] = document
            catalog_entries[pack_id] = (document, str(entry["digest"]))
            unsigned = {
                key: value for key, value in document.items() if key != "catalog_digest"
            }
            if document.get("catalog_digest") != _canonical_digest(unsigned):
                raise RuntimeError(f"executable catalog digest is stale: {pack_id}")

    if set(pack_sidecars) != set(catalog_entries):
        missing = sorted(set(pack_sidecars) - set(catalog_entries))
        extra = sorted(set(catalog_entries) - set(pack_sidecars))
        raise RuntimeError(
            "Defaults executable catalog coverage mismatch; "
            f"missing={missing}, extra={extra}"
        )
    for pack_id, sidecar_digest in pack_sidecars.items():
        catalog, catalog_digest = catalog_entries[pack_id]
        if sidecar_digest != catalog_digest:
            raise RuntimeError(f"Pack executable catalog artifact pin is stale: {pack_id}")
        if catalog.get("source_identity") != documents[("pack", pack_id)].get(
            "integrity", {}
        ).get("source_identity"):
            raise RuntimeError(f"executable catalog source identity is stale: {pack_id}")


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular non-symlink file: {path}")
    return path.read_bytes()


def _safe_resource_path(root: Path, relative: str, label: str) -> Path:
    """Resolve one package-relative path without following symlink components."""
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("~")
        or "\\" in relative
    ):
        raise RuntimeError(f"{label} is unsafe: {relative!r}")
    if root.is_symlink():
        raise RuntimeError(f"Resources/app root may not be a symlink: {root}")
    candidate = root / relative
    current = root
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as error:
        raise RuntimeError(f"{label} escapes Resources/app: {relative}") from error
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"{label} contains a symlink: {current}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise RuntimeError(f"{label} escapes Resources/app: {relative}")
    return resolved


def _validate_bundle_identity(artifact: Path, expected: object) -> None:
    """Verify the bundle id recorded by the v4 Shell descriptor."""
    if artifact.suffix != ".app":
        return
    if not isinstance(expected, str) or not expected.strip():
        raise RuntimeError("packaged macOS artifact has no bundle identifier")
    plist_path = artifact / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as error:
        raise RuntimeError(f"packaged macOS artifact Info.plist is invalid: {plist_path}") from error
    if plist.get("CFBundleIdentifier") != expected:
        raise RuntimeError(
            "packaged macOS artifact bundle identifier mismatch: "
            f"expected {expected!r}, got {plist.get('CFBundleIdentifier')!r}"
        )


def _entrypoint_path(artifact: Path, value: object) -> Path:
    """Resolve a catalog entrypoint strictly within its selected artifact."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError("packaged artifact entrypoint is unsafe")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("packaged artifact entrypoint is unsafe")
    if artifact.is_dir():
        candidate = (
            artifact / Path(*relative.parts[1:])
            if relative.parts and relative.parts[0] == artifact.name
            else artifact / relative
        )
        boundary = artifact.resolve()
    else:
        candidate = artifact
        boundary = artifact.parent.resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise RuntimeError("packaged artifact entrypoint is missing or unsafe")
    if not candidate.resolve().is_relative_to(boundary):
        raise RuntimeError("packaged artifact entrypoint escapes its artifact")
    return candidate


def _host_target() -> tuple[str, str]:
    """Return the platform/architecture of the verifier host."""
    if sys.platform == "darwin":
        platform_name = "macos"
    elif sys.platform == "win32":
        platform_name = "windows"
    elif sys.platform.startswith("linux"):
        platform_name = "linux"
    else:
        raise RuntimeError(f"unsupported verifier platform: {sys.platform}")
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    return platform_name, architecture


def _release_signature_message(release: dict[str, Any]) -> bytes:
    fields = (
        RELEASE_SCHEMA,
        release["catalog_sha256"],
        release["artifact_index_sha256"],
        release["profile_lock_sha256"],
        release["default_profile_sha256"],
        release["defaultspack_lock_sha256"],
        release["source_identity"],
        release["source_revision"],
        release["platform"],
        release["architecture"],
        release["artifact_id"],
        release["key_id"],
    )
    return b"\0".join(str(field).encode("utf-8") for field in fields)


def verify_release_binding(catalog: dict[str, Any], root: Path) -> dict[str, Any]:
    """Verify signed catalog/index/lock bytes and their exact cross-bindings."""
    binding = catalog.get("release_binding")
    if not isinstance(binding, dict) or binding.get("schema") != RELEASE_SCHEMA:
        raise RuntimeError(
            "installed artifact metadata requires a Shell v4 release binding"
        )
    release_path = _safe_resource_path(
        root, "bundled/presentation_release.v4.json", "release manifest path"
    )
    release = json.loads(_regular_bytes(release_path, "release manifest"))
    if release.get("schema") != RELEASE_SCHEMA:
        raise RuntimeError("release manifest schema is invalid")
    fixed_paths = {
        "catalog_path": "bundled/presentation_catalog.json",
        "artifact_index_path": "bundled/shell_artifact_index.v4.json",
        "profile_lock_path": "bundled/shell_profile_lock.v4.json",
        "default_profile_path": "ecosystem/defaultspack/v4/defaults.profile.v4.json",
        "defaultspack_lock_path": "ecosystem/defaultspack/v4/bundle.lock.json",
    }
    release_fields = {
        "schema",
        "catalog_sha256",
        "artifact_index_sha256",
        "profile_lock_sha256",
        "default_profile_sha256",
        "defaultspack_lock_sha256",
        "artifact_id",
        "platform",
        "architecture",
        "source_identity",
        "source_revision",
        "key_id",
        "public_key",
        "signature",
        *fixed_paths,
    }
    if set(release) != release_fields:
        raise RuntimeError("release manifest has unknown or missing fields")
    for field, expected in fixed_paths.items():
        if release.get(field) != expected:
            raise RuntimeError(f"release manifest {field} is not canonical")
    catalog_bytes = _regular_bytes(
        _safe_resource_path(root, fixed_paths["catalog_path"], "catalog path"),
        "catalog",
    )
    index_bytes = _regular_bytes(
        _safe_resource_path(root, fixed_paths["artifact_index_path"], "artifact index path"),
        "artifact index",
    )
    lock_bytes = _regular_bytes(
        _safe_resource_path(root, fixed_paths["profile_lock_path"], "profile lock path"),
        "profile lock",
    )
    profile_bytes = _regular_bytes(
        _safe_resource_path(
            root, fixed_paths["default_profile_path"], "default Profile path"
        ),
        "default Profile",
    )
    defaultspack_lock_bytes = _regular_bytes(
        _safe_resource_path(
            root, fixed_paths["defaultspack_lock_path"], "Defaults lock path"
        ),
        "Defaults lock",
    )
    if not isinstance(release, dict):
        raise RuntimeError("release manifest must be an object")
    if _byte_digest(catalog_bytes) != release.get("catalog_sha256"):
        raise RuntimeError("signed catalog digest mismatch")
    if _byte_digest(index_bytes) != release.get("artifact_index_sha256"):
        raise RuntimeError("signed artifact index digest mismatch")
    if _byte_digest(lock_bytes) != release.get("profile_lock_sha256"):
        raise RuntimeError("signed profile lock digest mismatch")
    if _byte_digest(profile_bytes) != release.get("default_profile_sha256"):
        raise RuntimeError("signed default Profile digest mismatch")
    if _byte_digest(defaultspack_lock_bytes) != release.get(
        "defaultspack_lock_sha256"
    ):
        raise RuntimeError("signed Defaults bundle lock digest mismatch")
    if catalog.get("default_profile_digest") != release.get(
        "default_profile_sha256"
    ):
        raise RuntimeError("catalog default Profile identity mismatch")
    try:
        defaultspack_lock = json.loads(defaultspack_lock_bytes)
    except json.JSONDecodeError as error:
        raise RuntimeError("Defaults bundle lock is malformed") from error
    entries = defaultspack_lock.get("entries") if isinstance(defaultspack_lock, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Defaults bundle lock entries are missing")
    bundle_root = root / "ecosystem" / "defaultspack" / "v4"
    _verify_defaultspack_bundle(entries, bundle_root)
    profile_entries = [
        entry
        for entry in entries
        if entry.get("path") == "defaults.profile.v4.json"
        and entry.get("kind") == "profile"
    ]
    if len(profile_entries) != 1 or profile_entries[0].get("digest") != _byte_digest(
        profile_bytes
    ):
        raise RuntimeError("Defaults bundle lock does not bind the default Profile")
    index = json.loads(index_bytes)
    lock = json.loads(lock_bytes)
    if not isinstance(index, dict) or index.get("schema") != "io.tobkiri.shell.artifact-index.v4":
        raise RuntimeError("artifact index schema is invalid")
    if not isinstance(lock, dict) or lock.get("schema") != "io.tobkiri.shell.profile-lock.v4":
        raise RuntimeError("profile lock schema is invalid")
    if _canonical_digest(index) != binding.get("artifact_index_sha256"):
        raise RuntimeError("catalog artifact index binding mismatch")
    if _canonical_digest(lock) != binding.get("profile_lock_sha256"):
        raise RuntimeError("catalog profile lock binding mismatch")
    lock_body = {key: value for key, value in lock.items() if key != "lock_revision"}
    if _canonical_digest(lock_body) != lock.get("lock_revision"):
        raise RuntimeError("profile lock revision mismatch")
    if index.get("sha256") != lock.get("artifact_sha256"):
        raise RuntimeError("artifact tree digest differs between index and lock")
    if index.get("entrypoint_sha256") != lock.get("entrypoint_sha256"):
        raise RuntimeError("artifact entrypoint digest differs between index and lock")
    catalog_without_binding = {
        key: value for key, value in catalog.items() if key != "release_binding"
    }
    if _canonical_digest(catalog_without_binding) != binding.get("catalog_revision"):
        raise RuntimeError("catalog revision mismatch")
    exact_fields = (
        "artifact_id",
        "platform",
        "architecture",
        "source_identity",
        "source_revision",
    )
    for field in exact_fields:
        if release.get(field) != binding.get(field) or index.get(field) != binding.get(
            field
        ):
            raise RuntimeError(f"release exact field mismatch: {field}")
    try:
        public_key = base64.b64decode(release["public_key"], validate=True)
        signature = base64.b64decode(release["signature"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise RuntimeError("Shell release signing fields are invalid") from error
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, _release_signature_message(release)
        )
    except (InvalidSignature, ValueError, TypeError) as error:
        raise RuntimeError("Shell release signature verification failed") from error
    return {
        "artifact_id": release["artifact_id"],
        "catalog_sha256": release["catalog_sha256"],
        "key_id": release["key_id"],
        "source_identity": release["source_identity"],
        "source_revision": release["source_revision"],
    }


def verify_catalog(
    catalog: dict[str, Any],
    resource_root_path: Path | None = None,
    *,
    require_production: bool = False,
    require_target: bool = False,
) -> dict[str, Any]:
    """Verify Base Pack/Shell compatibility, fail-closed artifacts, and identity."""
    base_packs = catalog.get("base_packs")
    shells = catalog.get("shell_providers")
    if not isinstance(base_packs, list) or len(base_packs) != 1:
        raise RuntimeError("packaged catalog must contain exactly one Base Pack")
    if not isinstance(shells, list) or not shells:
        raise RuntimeError("packaged catalog must contain Shell Providers")

    base = base_packs[0]
    if not isinstance(base, dict):
        raise RuntimeError("packaged Base Pack descriptor is invalid")
    required_capabilities = set(base.get("required_capabilities", []))
    allowed_families = set(base.get("allowed_families", []))
    identity = (
        catalog.get("default_profile_id"),
        catalog.get("default_profile_digest"),
        base.get("backend_identity_digest"),
        tuple(base.get("backend_provider_ids", [])),
        tuple(base.get("state_owners", [])),
    )
    compatible_shells: list[str] = []
    blocked_artifacts: list[str] = []
    verified_artifacts: list[str] = []
    has_installed_artifact = False
    for shell in shells:
        if not isinstance(shell, dict):
            raise RuntimeError("packaged Shell Provider descriptor is invalid")
        capabilities = set(shell.get("capabilities", []))
        approval = shell.get("approval")
        compatible = (
            shell.get("contract_id") == SHELL_CONTRACT
            and required_capabilities.issubset(capabilities)
            and shell.get("presentation_family") in allowed_families
            and isinstance(approval, dict)
            and approval.get("state") == "verified"
        )
        if compatible:
            compatible_shells.append(str(shell["provider_id"]))
        for variant in shell.get("artifact_variants", []):
            if not isinstance(variant, dict):
                raise RuntimeError("packaged artifact variant is invalid")
            path_value = variant.get("path")
            digest_value = variant.get("sha256")
            entrypoint_digest = variant.get("entrypoint_sha256")
            if len(
                {
                    path_value is None,
                    digest_value is None,
                    entrypoint_digest is None,
                }
            ) != 1:
                raise RuntimeError(
                    "packaged artifact path and digests must be supplied together"
                )
            if path_value is None:
                blocked_artifacts.append(str(variant["artifact_id"]))
                continue
            has_installed_artifact = True
            if resource_root_path is None:
                raise RuntimeError(
                    "installed artifact metadata requires a package resource root"
                )
            if not isinstance(path_value, str):
                raise RuntimeError("packaged artifact path must be text")
            if not path_value.startswith("bundled/presentation-artifacts/"):
                raise RuntimeError(
                    f"packaged artifact path is outside presentation-artifacts: {path_value}"
                )
            relative = Path(path_value)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"packaged artifact path is unsafe: {path_value}")
            artifact = _safe_resource_path(
                resource_root_path, path_value, "packaged artifact path"
            )
            if not artifact.exists():
                raise RuntimeError(f"packaged artifact is missing: {artifact}")
            actual_digest, actual_size = artifact_digest_and_size(artifact)
            if actual_digest.lower() != str(digest_value).lower():
                raise RuntimeError(
                    f"packaged artifact digest mismatch for {variant['artifact_id']}"
                )
            if variant.get("size") != actual_size:
                raise RuntimeError(
                    f"packaged artifact size mismatch for {variant['artifact_id']}"
                )
            entrypoint = _entrypoint_path(artifact, variant.get("entrypoint"))
            if _byte_digest(entrypoint.read_bytes()) != entrypoint_digest:
                raise RuntimeError(
                    f"packaged artifact entrypoint digest mismatch for {variant['artifact_id']}"
                )
            if not variant.get("source_identity") or not variant.get("source_revision"):
                raise RuntimeError(
                    "packaged artifact source identity/revision is incomplete"
                )
            if variant.get("artifact_id") != (
                f"{shell.get('provider_id')}.{variant.get('platform')}-"
                f"{variant.get('architecture')}"
            ):
                raise RuntimeError("packaged artifact identity does not match its target")
            if (variant.get("platform"), variant.get("architecture")) not in VALID_TARGETS:
                raise RuntimeError("packaged artifact platform/architecture is invalid")
            _validate_bundle_identity(artifact, variant.get("bundle_identifier"))
            verified_artifacts.append(str(variant["artifact_id"]))

    if require_production and not has_installed_artifact:
        raise RuntimeError(
            "production package must contain a sealed Shell artifact; "
            "null-metadata catalog is not packageable"
        )
    release_report = (
        verify_release_binding(catalog, resource_root_path)
        if has_installed_artifact and resource_root_path
        else None
    )

    default_selection = catalog.get("default_selection")
    if not isinstance(default_selection, dict):
        raise RuntimeError("packaged catalog has no default selection")
    if default_selection.get("base_pack_id") != base.get("pack_id"):
        raise RuntimeError("packaged default selection does not select the Base Pack")
    if default_selection.get("shell_provider_id") not in compatible_shells:
        raise RuntimeError(
            "packaged default selection is not compatible with the Base Pack"
        )
    if release_report is not None:
        selected_shell = next(
            shell
            for shell in shells
            if shell.get("provider_id") == default_selection.get("shell_provider_id")
        )
        selected_artifact_ids = {
            str(variant.get("artifact_id"))
            for variant in selected_shell.get("artifact_variants", [])
            if isinstance(variant, dict)
        }
        if release_report["artifact_id"] not in selected_artifact_ids:
            raise RuntimeError(
                "signed artifact does not match the default Profile Shell"
            )
        selected_shell = next(
            shell
            for shell in shells
            if shell.get("provider_id") == default_selection.get("shell_provider_id")
        )
        try:
            selected_variant = next(
                variant
                for variant in selected_shell.get("artifact_variants", [])
                if isinstance(variant, dict)
                and variant.get("artifact_id") == release_report["artifact_id"]
            )
        except StopIteration as error:
            raise RuntimeError(
                "signed artifact does not match the default Profile Shell"
            ) from error
        index_path = resource_root_path / "bundled" / "shell_artifact_index.v4.json"
        index = json.loads(_regular_bytes(index_path, "artifact index"))
        for field in ("path", "sha256", "entrypoint_sha256", "size"):
            if selected_variant.get(field) != index.get(field):
                raise RuntimeError(
                    f"catalog artifact metadata differs from artifact index: {field}"
                )

        if require_target:
            target = _host_target()
            release = json.loads(
                _regular_bytes(
                    _safe_resource_path(
                        resource_root_path,
                        "bundled/presentation_release.v4.json",
                        "release manifest path",
                    ),
                    "release manifest",
                )
            )
            if (release.get("platform"), release.get("architecture")) != target:
                raise RuntimeError("packaged Shell release targets the wrong host")

    return {
        "base_pack_id": base.get("pack_id"),
        "compatible_shell_provider_ids": compatible_shells,
        "blocked_uninstalled_artifact_count": len(blocked_artifacts),
        "verified_artifact_ids": verified_artifacts,
        "profile_identity": identity,
        "release": release_report,
    }


def verify_binary(binary: Path) -> dict[str, Any]:
    """Verify the release binary contains the presentation commands and ACL entries."""
    try:
        contents = binary.read_bytes()
    except OSError as error:
        raise RuntimeError(
            f"failed to read packaged Launcher binary {binary}: {error}"
        ) from error
    missing = [
        marker
        for marker in (*PRESENTATION_COMMANDS, *PRESENTATION_PERMISSIONS)
        if marker.encode() not in contents
    ]
    if missing:
        raise RuntimeError(
            f"packaged binary is missing presentation IPC markers: {missing}"
        )
    return {
        "binary": str(binary),
        "ipc_commands": list(PRESENTATION_COMMANDS),
        "ipc_permissions": list(PRESENTATION_PERMISSIONS),
    }


def process_ids_with_marker(marker: str) -> list[int]:
    """Find processes that inherited the harness-only environment marker."""
    result = subprocess.run(
        ["ps", "eww", "-axo", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    process_ids: list[int] = []
    for line in result.stdout.splitlines():
        if marker not in line:
            continue
        try:
            process_ids.append(int(line.strip().split(maxsplit=1)[0]))
        except (IndexError, ValueError):
            continue
    return process_ids


def stop_marker_processes(marker: str) -> list[int]:
    """Stop any package children that detached from the launcher's process group."""
    stopped: list[int] = []
    for _ in range(3):
        process_ids = process_ids_with_marker(marker)
        if not process_ids:
            return stopped
        for process_id in process_ids:
            try:
                os.kill(process_id, signal.SIGTERM)
                stopped.append(process_id)
            except ProcessLookupError:
                continue
        time.sleep(0.2)
    remaining = process_ids_with_marker(marker)
    for process_id in remaining:
        try:
            os.kill(process_id, signal.SIGKILL)
            stopped.append(process_id)
        except ProcessLookupError:
            continue
    return stopped


def launch_from_relocated_cwd(binary: Path, seconds: float) -> dict[str, Any]:
    """Start the packaged binary outside the checkout and clean up its process group."""
    if seconds <= 0:
        raise RuntimeError("--launch-seconds must be greater than zero")
    marker_name = "TOBKIRI_RELEASE_HARNESS_ID"
    marker = f"{marker_name}={os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="tobkiri-release-cwd-") as cwd:
        process = subprocess.Popen(
            [os.fspath(binary)],
            cwd=cwd,
            env={**os.environ, "RUST_LOG": "debug", marker_name: str(os.getpid())},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        try:
            deadline = time.monotonic() + seconds
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate(timeout=5)
        detached_children = stop_marker_processes(marker)
        if process.returncode not in (0, -signal.SIGTERM, -signal.SIGKILL):
            raise RuntimeError(
                f"packaged Launcher exited unexpectedly with {process.returncode}:\n"
                f"{output[-4000:]}"
            )
        lines = [line for line in output.splitlines() if line.strip()]
        return {
            "started_from_relocated_cwd": True,
            "return_code_after_cleanup": process.returncode,
            "detached_children_stopped": detached_children,
            "log_tail": lines[-8:],
        }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the resource, IPC, and relocated-cwd package checks."""
    args = parse_args(argv)
    app = args.app.expanduser().resolve()
    root = resource_root(app)
    catalog_path = root / "bundled" / "presentation_catalog.json"
    binary = app / "Contents" / "MacOS" / "tobkiri-launcher"
    report = {
        "catalog": verify_catalog(
            load_catalog(catalog_path),
            root,
            require_production=True,
            require_target=True,
        ),
        "ipc": verify_binary(binary),
        "launch": launch_from_relocated_cwd(binary, args.launch_seconds),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
