"""Host verification for a native, explicitly confirmed signed Pack import."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest

from .external_pack_catalog_v4 import (
    _project_catalog_record,
    _read_json_nofollow,
    admit_signed_external_pack,
    admit_signed_pack_version,
    control_catalog_revision,
    load_external_pack_catalog,
)
from .pack_artifact_integrity import read_host_policy_snapshot, write_host_install_record
from .pack_boundary import load_pack_catalog, resolve_pack_root
from .pack_signature import SIGNED_MANIFEST_RELATIVE, verify_signed_pack


def _read_public_key(path: Path, pack_root: Path) -> tuple[Ed25519PublicKey, str]:
    selected = Path(path)
    if not selected.is_absolute() or selected.is_symlink():
        raise ValueError("select a separate public key file")
    if selected.resolve().is_relative_to(pack_root.resolve()):
        raise ValueError("public key must be selected outside the Pack folder")
    descriptor = os.open(selected, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024:
            raise ValueError("public key file is invalid")
        pem = os.read(descriptor, 64 * 1024 + 1)
        after = os.fstat(descriptor)
        if len(pem) > 64 * 1024 or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("public key file changed while reading")
    finally:
        os.close(descriptor)
    public_key = serialization.load_pem_public_key(pem)
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("public key must be Ed25519")
    canonical_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return public_key, canonical_pem


def preview_signed_pack(source_root: Path, public_key_path: Path) -> dict[str, Any]:
    """Verify the entire selected artifact and separately selected public key."""

    source = Path(source_root)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError("select a real signed Pack folder")
    source = source.resolve()
    public_key, canonical_pem = _read_public_key(public_key_path, source)
    signed = _read_json_nofollow(source / SIGNED_MANIFEST_RELATIVE, 4 * 1024 * 1024)
    if not isinstance(signed, dict):
        raise ValueError("signed Pack manifest is invalid")
    from rumi_ai import __version__ as core_version

    verified = verify_signed_pack(
        source, signed, public_key, core_version=str(core_version)
    )
    _project_catalog_record(source, signed)  # Validates Normal Sandbox and v4 identity.
    compiled = compile_pack_root(source)
    if compiled.artifact.pack_id != verified["pack_id"]:
        raise ValueError("signed and compiled Pack identities differ")
    public_key_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    preview = {
        "pack_id": str(verified["pack_id"]),
        "version": str(verified["version"]),
        "publisher_id": str(verified["publisher_id"]),
        "key_id": str(verified["key_id"]),
        "key_fingerprint": "sha256:" + hashlib.sha256(public_key_raw).hexdigest(),
        "requested_capabilities": list(signed["requested_capabilities"]),
        "contract_versions": dict(signed["contract_versions"]),
        "artifact_digest": compiled.artifact.digest,
    }
    snapshot = load_external_pack_catalog()
    pack_id = str(verified["pack_id"])
    retained = snapshot.versions.get(pack_id, {})
    bundled = load_pack_catalog().get(pack_id)
    predecessors = {
        str(row["version_string"]): str(row["artifact_digest"])
        for row in retained.values()
    }
    if bundled is not None:
        if bundled.get("kind") not in {"normal_sandbox", "application"}:
            raise ValueError("signed Normal Pack cannot replace a Host Extension, Base or Shell")
        root = resolve_pack_root(pack_id)
        manifest = _read_json_nofollow(root / "pack.v4.json", 4 * 1024 * 1024)
        if not isinstance(manifest, dict):
            raise ValueError("bundled update predecessor is unavailable")
        predecessors[str(manifest["pack"]["version"])] = compile_pack_root(root).artifact.digest
    if predecessors:
        from packaging.version import Version
        from .pack_version_selection import verify_version_predecessor

        predecessor = (
            compiled.artifact.digest if compiled.artifact.digest in retained
            else predecessors[max(predecessors, key=Version)]
        )
        verify_version_predecessor(
            pack_id, predecessor, versions=snapshot.versions,
            publisher_id=str(verified["publisher_id"]), version=str(verified["version"]),
            artifact_digest=compiled.artifact.digest,
        )
        preview["predecessor_artifact_digest"] = predecessor
        preview["catalog_revision"] = control_catalog_revision()
    preview["preview_digest"] = canonical_digest(
        {
            "preview": preview,
            "signed_manifest": signed,
            "public_key_pem": canonical_pem,
            "source_root": str(source),
        }
    )
    return preview


def commit_signed_pack(
    source_root: Path,
    public_key_path: Path,
    *,
    expected_preview_digest: str,
    trust_store_path: Path,
) -> dict[str, Any]:
    """Reverify an approved preview, bind exact Host policy, then admit it."""

    preview = preview_signed_pack(source_root, public_key_path)
    actual_digest = str(preview["preview_digest"])
    if not hmac.compare_digest(actual_digest, expected_preview_digest):
        raise ValueError("signed Pack changed after native confirmation")
    source = Path(source_root).resolve()
    public_key, canonical_pem = _read_public_key(public_key_path, source)
    signed = _read_json_nofollow(source / SIGNED_MANIFEST_RELATIVE, 4 * 1024 * 1024)
    if not isinstance(signed, dict):
        raise ValueError("signed Pack manifest is invalid")
    verify_signed_pack(
        source, signed, public_key,
        expected_pack_id=str(preview["pack_id"]),
        expected_publisher_id=str(preview["publisher_id"]),
        expected_version=str(preview["version"]),
        expected_key_id=str(preview["key_id"]),
        expected_contract_versions=dict(preview["contract_versions"]),
        expected_capabilities=list(preview["requested_capabilities"]),
    )
    predecessor = preview.get("predecessor_artifact_digest")
    policy_arguments: dict[str, Any] = {}
    if predecessor is not None:
        policy = read_host_policy_snapshot(trust_store_path) if trust_store_path.exists() else {}
        prior = policy.get("install_records", {}).get(str(preview["pack_id"]))
        if prior is not None:
            policy_arguments["expected_install_record"] = prior
    write_host_install_record(
        trust_store_path,
        pack_id=str(preview["pack_id"]),
        install_path=source,
        record={
            "signature_required": True,
            "publisher_id": preview["publisher_id"],
            "key_id": preview["key_id"],
            "installed_version": preview["version"],
            "signed_manifest_path": SIGNED_MANIFEST_RELATIVE,
            "contract_versions": preview["contract_versions"],
            "requested_capabilities": preview["requested_capabilities"],
        },
        **policy_arguments,
        publisher_record={
            "public_key_pem": canonical_pem,
            "allowed_pack_namespaces": [str(preview["pack_id"])],
            "revoked_key_ids": [],
        },
    )
    if predecessor is not None:
        return dict(admit_signed_pack_version(
            source, trust_store_path=trust_store_path,
            expected_predecessor_digest=str(predecessor),
            expected_catalog_revision=str(preview["catalog_revision"]),
        ))
    return dict(admit_signed_external_pack(source, trust_store_path=trust_store_path))
