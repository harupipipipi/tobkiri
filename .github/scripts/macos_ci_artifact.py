#!/usr/bin/env python3
"""Create and verify non-publishable macOS CI/E2E artifact attestations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import plistlib
import re
import stat
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


SCHEMA = "io.tobkiri.macos-ci-e2e-attestation.v1"
POLICY = "ci-e2e-v1"
BUNDLE_IDENTIFIER = "dev.tobkiri.launcher.ci-e2e"
CERTIFICATE_NAME = "ci-e2e-signing-certificate.der"
ATTESTATION_NAME = "ci-e2e-startup-attestation.v1.json"
PACKVM_HELPER_IDENTIFIER = "dev.tobkiri.launcher.packvm-vz-helper"
PACKVM_HELPER_RELATIVE = "Contents/MacOS/tobkiri-packvm-vz-helper"
PACKVM_PROVISIONING_MANIFEST_RELATIVE = (
    "Contents/Resources/packvm-vz-provisioning.v1.json"
)
PACKVM_BUNDLE_MANIFEST_RELATIVE = "Contents/Resources/packvm-vz-helper.manifest.v1.json"
PACKVM_PROVISIONING_SCHEMA = "io.tobkiri.packvm-vz-provisioning.v1"
PACKVM_BUNDLE_SCHEMA = "io.tobkiri.packvm-vz-bundle-manifest.v1"
PACKVM_REQUIRED_PROVISIONING_NAMES = frozenset(
    {
        "image_descriptor",
        "bubblewrap_descriptor",
        "bubblewrap_package",
        "guest_runner",
        "guest_service_template",
        "cloud_init_template",
        "licenses",
    }
)
PACKVM_BUBBLEWRAP_DESCRIPTOR_SCHEMA = "io.tobkiri.packvm-vz-bubblewrap-descriptor.v1"
PACKVM_BUBBLEWRAP_PACKAGE_BYTES = 50_132
PACKVM_REQUIRED_ENTITLEMENTS = ("com.apple.security.virtualization",)
SIGNED_PATHS = (
    "Contents/MacOS/tobkiri-launcher",
    PACKVM_HELPER_RELATIVE,
    "Contents/Resources/app/python-runtime/sealed-environment.v1.json",
    "Contents/Resources/app/runtime-resource-manifest.v1.json",
    "Contents/Resources/ci-e2e-artifact-policy.v1.json",
    PACKVM_PROVISIONING_MANIFEST_RELATIVE,
    PACKVM_BUNDLE_MANIFEST_RELATIVE,
    f"Contents/Resources/{CERTIFICATE_NAME}",
)
MACHO_SIGNED_PATHS = frozenset(
    {"Contents/MacOS/tobkiri-launcher", PACKVM_HELPER_RELATIVE}
)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the immutable identity fields required for a bound regular file."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
    )


def _open_bound_regular(
    path: Path, label: str, maximum_size: int | None
) -> tuple[int, tuple[int, int, int, int, int]]:
    """Open one regular file without links and bind its identity across a read."""
    try:
        named = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} is unavailable: {path}") from error
    if (
        not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (maximum_size is not None and named.st_size > maximum_size)
    ):
        raise ValueError(f"{label} is not a bounded singly-linked regular file")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise ValueError(f"{label} cannot be opened without following links") from error
    expected = _metadata_identity(named)
    opened = os.fstat(descriptor)
    if _metadata_identity(opened) != expected:
        os.close(descriptor)
        raise ValueError(f"{label} identity changed while opened")
    return descriptor, expected


def _assert_bound_regular_unchanged(
    descriptor: int,
    path: Path,
    expected: tuple[int, int, int, int, int],
    label: str,
) -> None:
    """Reject a replacement, link, or mutation after descriptor-bound reading."""
    try:
        retained = os.fstat(descriptor)
        named = path.lstat()
    except OSError as error:
        raise ValueError(f"{label} became unavailable during read") from error
    if (
        _metadata_identity(retained) != expected
        or _metadata_identity(named) != expected
    ):
        raise ValueError(f"{label} identity changed during read")


def _read_bound_regular(path: Path, label: str, maximum_size: int) -> bytes:
    """Read a bounded regular file through one no-follow descriptor."""
    descriptor, expected = _open_bound_regular(path, label, maximum_size)
    try:
        chunks: list[bytes] = []
        remaining = expected[-1]
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"{label} truncated while read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"{label} grew while read")
        _assert_bound_regular_unchanged(descriptor, path, expected, label)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    """Hash one no-follow, singly-linked regular file with replacement detection."""
    descriptor, expected = _open_bound_regular(path, "attested path", None)
    try:
        digest = hashlib.sha256()
        remaining = expected[-1]
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"attested path truncated while read: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"attested path grew while read: {path}")
        _assert_bound_regular_unchanged(descriptor, path, expected, "attested path")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_regular_json(path: Path, label: str) -> dict[str, Any]:
    """Read one bounded, singly-linked JSON object without following links."""
    try:
        document = json.loads(_read_bound_regular(path, label, 256 * 1024))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is malformed") from error
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def _is_sha256(value: object, *, prefix: bool = False) -> bool:
    """Return whether ``value`` is one fixed lowercase SHA-256 representation."""
    if not isinstance(value, str):
        return False
    required_prefix = "sha256:" if prefix else ""
    if not value.startswith(required_prefix):
        return False
    digest = value.removeprefix(required_prefix)
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _safe_provisioning_relative(value: object) -> str:
    """Validate a provisioning input path below Contents/Resources only."""
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError("PackVM provisioning path is invalid")
    pieces = value.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        raise ValueError("PackVM provisioning path is invalid")
    if not value.startswith("packvm-vz-provisioning/"):
        raise ValueError("PackVM provisioning path escapes the fixed resource root")
    return value


def _verify_packvm_provisioning(app_bundle: Path) -> dict[str, Any]:
    """Verify immutable PackVM provisioning inputs before the app is accepted."""
    manifest_path = app_bundle / PACKVM_PROVISIONING_MANIFEST_RELATIVE
    document = _read_regular_json(manifest_path, "PackVM provisioning manifest")
    if set(document) != {"schema", "target", "boot_mode", "inputs"}:
        raise ValueError("PackVM provisioning manifest fields are invalid")
    if (
        document["schema"] != PACKVM_PROVISIONING_SCHEMA
        or document["target"] != "aarch64-apple-darwin"
        or document["boot_mode"] != "efi"
        or not isinstance(document["inputs"], list)
    ):
        raise ValueError("PackVM provisioning manifest domain is invalid")
    inputs = document["inputs"]
    if len(inputs) != len(PACKVM_REQUIRED_PROVISIONING_NAMES):
        raise ValueError("PackVM provisioning manifest has an invalid input count")
    names: set[str] = set()
    paths: set[str] = set()
    entries_by_name: dict[str, dict[str, object]] = {}
    for entry in inputs:
        if not isinstance(entry, dict) or set(entry) != {"name", "path", "sha256"}:
            raise ValueError("PackVM provisioning manifest entry is invalid")
        name = entry["name"]
        if not isinstance(name, str) or name in names:
            raise ValueError("PackVM provisioning manifest names are invalid")
        names.add(name)
        entries_by_name[name] = entry
        relative = _safe_provisioning_relative(entry["path"])
        if relative in paths:
            raise ValueError("PackVM provisioning manifest paths are not unique")
        paths.add(relative)
        if not _is_sha256(entry["sha256"], prefix=True):
            raise ValueError("PackVM provisioning digest is invalid")
        provisioning_path = app_bundle / "Contents/Resources" / relative
        try:
            provisioning_metadata = provisioning_path.lstat()
        except OSError as error:
            raise ValueError("PackVM provisioning input is unavailable") from error
        if provisioning_metadata.st_size <= 0:
            raise ValueError("PackVM provisioning input is empty")
        if _sha256(provisioning_path) != entry["sha256"].removeprefix("sha256:"):
            raise ValueError("PackVM provisioning identity changed")
    if names != PACKVM_REQUIRED_PROVISIONING_NAMES:
        raise ValueError("PackVM provisioning manifest has missing or extra inputs")
    _verify_packaged_bubblewrap(app_bundle, entries_by_name)
    return document


def _verify_packaged_bubblewrap(
    app_bundle: Path, entries_by_name: Mapping[str, Mapping[str, object]]
) -> None:
    """Bind the packaged, networkless bubblewrap package to its descriptor."""
    descriptor_entry = entries_by_name["bubblewrap_descriptor"]
    package_entry = entries_by_name["bubblewrap_package"]
    descriptor_path = app_bundle / "Contents/Resources" / str(descriptor_entry["path"])
    descriptor = _read_regular_json(descriptor_path, "PackVM bubblewrap descriptor")
    if (
        set(descriptor) != {"schema", "package", "version", "architecture", "source"}
        or descriptor["schema"] != PACKVM_BUBBLEWRAP_DESCRIPTOR_SCHEMA
        or descriptor["package"] != "bubblewrap"
        or not isinstance(descriptor["version"], str)
        or not descriptor["version"]
        or descriptor["architecture"] != "arm64"
        or not isinstance(descriptor["source"], dict)
    ):
        raise ValueError("PackVM bubblewrap descriptor is invalid")
    source = descriptor["source"]
    if (
        set(source) != {"url", "size_bytes", "sha256"}
        or not isinstance(source["url"], str)
        or not source["url"].startswith("https://")
        or source["size_bytes"] != PACKVM_BUBBLEWRAP_PACKAGE_BYTES
        or not _is_sha256(source["sha256"], prefix=True)
        or package_entry["sha256"] != source["sha256"]
    ):
        raise ValueError("PackVM bubblewrap package binding is invalid")
    package_path = app_bundle / "Contents/Resources" / str(package_entry["path"])
    try:
        package_metadata = package_path.lstat()
    except OSError as error:
        raise ValueError("PackVM bubblewrap package is unavailable") from error
    if package_metadata.st_size != PACKVM_BUBBLEWRAP_PACKAGE_BYTES:
        raise ValueError("PackVM bubblewrap package size is invalid")


def _validate_packvm_helper_signing(
    value: Mapping[str, object],
) -> dict[str, str | None]:
    """Normalize the only two allowed helper signing domains."""
    if set(value) != {"signing_mode", "team_id", "authority"}:
        raise ValueError("PackVM helper signing identity is invalid")
    signing_mode = value["signing_mode"]
    team_id = value["team_id"]
    authority = value["authority"]
    if signing_mode == "ad-hoc" and team_id is None and authority is None:
        return {
            "signing_mode": "ad-hoc",
            "team_id": None,
            "authority": None,
        }
    if (
        signing_mode != "developer-id"
        or not isinstance(team_id, str)
        or not re.fullmatch(r"[A-Z0-9]{10}", team_id)
        or not isinstance(authority, str)
        or not authority.startswith("Developer ID Application: ")
        or not authority.endswith(f" ({team_id})")
        or len(authority) > 512
    ):
        raise ValueError("PackVM production helper identity is invalid")
    return {
        "signing_mode": "developer-id",
        "team_id": team_id,
        "authority": authority,
    }


def _inspect_packvm_helper_signing(helper_path: Path) -> dict[str, str | None]:
    """Read the sidecar's actual macOS signing domain after it is signed."""
    details = subprocess.run(
        ["/usr/bin/codesign", "-d", "-r-", "--verbose=4", os.fspath(helper_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    output = f"{details.stdout}\n{details.stderr}"
    if details.returncode != 0:
        raise ValueError("PackVM helper codesign inspection failed")
    fields: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields.setdefault(key, []).append(value)
    if fields.get("Identifier") != [PACKVM_HELPER_IDENTIFIER]:
        raise ValueError("PackVM helper identifier is not build-pinned")
    entitlement = subprocess.run(
        ["/usr/bin/codesign", "-d", "--entitlements", ":-", os.fspath(helper_path)],
        check=False,
        capture_output=True,
    )
    if entitlement.returncode != 0:
        raise ValueError("PackVM helper entitlement inspection failed")
    try:
        entitlements = plistlib.loads(entitlement.stdout)
    except (ValueError, plistlib.InvalidFileException) as error:
        raise ValueError("PackVM helper entitlement plist is invalid") from error
    if entitlements != {"com.apple.security.virtualization": True}:
        raise ValueError("PackVM helper entitlements are not exact")
    if fields.get("Signature") == ["adhoc"]:
        return _validate_packvm_helper_signing(
            {"signing_mode": "ad-hoc", "team_id": None, "authority": None}
        )
    team_ids = fields.get("TeamIdentifier", [])
    authorities = [
        authority
        for authority in fields.get("Authority", [])
        if authority.startswith("Developer ID Application: ")
    ]
    if len(team_ids) != 1 or len(authorities) != 1:
        raise ValueError("PackVM helper Developer ID identity is invalid")
    return _validate_packvm_helper_signing(
        {
            "signing_mode": "developer-id",
            "team_id": team_ids[0],
            "authority": authorities[0],
        }
    )


def write_packvm_bundle_manifest(
    app_bundle: Path,
    *,
    expected_signing_mode: str | None = None,
    expected_team_id: str | None = None,
) -> Path:
    """Bind a signed PackVM helper and its immutable provisioning manifest."""
    helper = app_bundle / PACKVM_HELPER_RELATIVE
    _verify_packvm_provisioning(app_bundle)
    signing = _inspect_packvm_helper_signing(helper)
    if expected_signing_mode not in {None, "ad-hoc", "developer-id"}:
        raise ValueError("PackVM helper expected signing mode is invalid")
    if expected_signing_mode == "developer-id" and not isinstance(
        expected_team_id, str
    ):
        raise ValueError("PackVM helper production team assertion is required")
    if expected_signing_mode == "ad-hoc" and expected_team_id is not None:
        raise ValueError("PackVM ad-hoc helper cannot assert a team")
    if (
        expected_signing_mode is not None
        and signing["signing_mode"] != expected_signing_mode
    ):
        raise ValueError("PackVM helper signing mode differs from the expected mode")
    if expected_team_id is not None and signing["team_id"] != expected_team_id:
        raise ValueError("PackVM helper team differs from the expected identity")
    manifest_path = app_bundle / PACKVM_BUNDLE_MANIFEST_RELATIVE
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ValueError("PackVM bundle manifest must not pre-exist")
    document: dict[str, Any] = {
        "schema": PACKVM_BUNDLE_SCHEMA,
        "helper": {
            "path": PACKVM_HELPER_RELATIVE,
            "code_sha256": "sha256:" + _macho_code_sha256(helper),
            "identifier": PACKVM_HELPER_IDENTIFIER,
            "entitlements": list(PACKVM_REQUIRED_ENTITLEMENTS),
            "signing": signing,
        },
        "provisioning": {
            "path": PACKVM_PROVISIONING_MANIFEST_RELATIVE,
            "sha256": "sha256:"
            + _sha256(app_bundle / PACKVM_PROVISIONING_MANIFEST_RELATIVE),
        },
    }
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o444)
    os.replace(temporary, manifest_path)
    return manifest_path


def verify_packvm_bundle(app_bundle: Path) -> None:
    """Fail closed when helper, asset binding, or manifest provenance drifts."""
    manifest_path = app_bundle / PACKVM_BUNDLE_MANIFEST_RELATIVE
    document = _read_regular_json(manifest_path, "PackVM bundle manifest")
    if set(document) != {"schema", "helper", "provisioning"}:
        raise ValueError("PackVM bundle manifest fields are invalid")
    helper = document["helper"]
    provisioning = document["provisioning"]
    if not isinstance(helper, dict) or set(helper) != {
        "path",
        "code_sha256",
        "identifier",
        "entitlements",
        "signing",
    }:
        raise ValueError("PackVM helper binding is invalid")
    if not isinstance(provisioning, dict) or set(provisioning) != {"path", "sha256"}:
        raise ValueError("PackVM provisioning binding is invalid")
    signing = helper["signing"]
    if not isinstance(signing, dict) or set(signing) != {
        "signing_mode",
        "team_id",
        "authority",
    }:
        raise ValueError("PackVM helper signing identity is invalid")
    try:
        signing_valid = signing == _validate_packvm_helper_signing(signing)
    except ValueError:
        signing_valid = False
    if (
        document["schema"] != PACKVM_BUNDLE_SCHEMA
        or helper["path"] != PACKVM_HELPER_RELATIVE
        or helper["identifier"] != PACKVM_HELPER_IDENTIFIER
        or helper["entitlements"] != list(PACKVM_REQUIRED_ENTITLEMENTS)
        or not signing_valid
        or not _is_sha256(helper["code_sha256"], prefix=True)
        or provisioning["path"] != PACKVM_PROVISIONING_MANIFEST_RELATIVE
        or not _is_sha256(provisioning["sha256"], prefix=True)
    ):
        raise ValueError("PackVM bundle manifest domain is invalid")
    if (
        "sha256:" + _macho_code_sha256(app_bundle / PACKVM_HELPER_RELATIVE)
        != helper["code_sha256"]
    ):
        raise ValueError("PackVM helper code identity changed")
    if (
        "sha256:" + _sha256(app_bundle / PACKVM_PROVISIONING_MANIFEST_RELATIVE)
        != provisioning["sha256"]
    ):
        raise ValueError("PackVM provisioning manifest identity changed")
    _verify_packvm_provisioning(app_bundle)


def _macho_code_sha256(path: Path) -> str:
    """Hash code-bearing Mach-O bytes independent of the final signature size."""
    data = bytearray(_read_bound_regular(path, "attested Mach-O", 128 * 1024 * 1024))
    if len(data) < 32 or data[:4] != b"\xcf\xfa\xed\xfe":
        raise ValueError(f"attested executable is not a thin 64-bit Mach-O: {path}")
    command_count, command_bytes = struct.unpack_from("<II", data, 16)
    command_offset = 32
    command_end = command_offset + command_bytes
    if command_end > len(data):
        raise ValueError("Mach-O load commands exceed the executable")
    signature: tuple[int, int, int] | None = None
    linkedit_command: int | None = None
    for _ in range(command_count):
        if command_offset + 8 > command_end:
            raise ValueError("Mach-O load command is truncated")
        command, command_size = struct.unpack_from("<II", data, command_offset)
        if command_size < 8 or command_offset + command_size > command_end:
            raise ValueError("Mach-O load command size is invalid")
        if command == 0x1D:
            if command_size != 16 or signature is not None:
                raise ValueError("Mach-O code-signature command is invalid")
            data_offset, data_size = struct.unpack_from("<II", data, command_offset + 8)
            signature = (command_offset, data_offset, data_size)
        elif (
            command == 0x19
            and data[command_offset + 8 : command_offset + 24].rstrip(b"\0")
            == b"__LINKEDIT"
        ):
            if command_size < 72 or linkedit_command is not None:
                raise ValueError("Mach-O __LINKEDIT command is invalid")
            linkedit_command = command_offset
        command_offset += command_size
    if command_offset != command_end or signature is None or linkedit_command is None:
        raise ValueError("Mach-O code-signature command is missing")
    signature_command, data_offset, data_size = signature
    if data_offset < command_end or data_offset + data_size != len(data):
        raise ValueError("Mach-O code-signature blob is not the final bounded region")
    data[signature_command + 8 : signature_command + 16] = b"\0" * 8
    # codesign extends __LINKEDIT to contain its SuperBlob. Normalize only the
    # two size fields it necessarily rewrites; all code and other load-command
    # bytes remain authenticated by the CI attestation.
    data[linkedit_command + 32 : linkedit_command + 40] = b"\0" * 8
    data[linkedit_command + 48 : linkedit_command + 56] = b"\0" * 8
    return hashlib.sha256(data[:data_offset]).hexdigest()


def _attested_sha256(app_bundle: Path, relative: str) -> str:
    """Return the policy-specific identity for one fixed attested path."""
    path = app_bundle / relative
    if relative in MACHO_SIGNED_PATHS:
        return _macho_code_sha256(path)
    return _sha256(path)


def _message(certificate_sha256: str, files: list[dict[str, str]]) -> bytes:
    """Build the fixed-field signature domain shared with the Rust verifier."""
    lines = [
        "TOBKIRI-CI-E2E-ATTESTATION-V1",
        f"bundle_identifier={BUNDLE_IDENTIFIER}",
        f"certificate_sha256={certificate_sha256}",
    ]
    lines.extend(f"{entry['path']}\0{entry['sha256']}" for entry in files)
    return ("\n".join(lines) + "\n").encode("utf-8")


def create_identity(output_dir: Path) -> dict[str, str]:
    """Create an ephemeral Ed25519 certificate and private key in one task directory."""
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Tobkiri CI E2E Non-Publishable"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "CI-E2E"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Tobkiri Non-Publishable"),
        ]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=True
        )
        .sign(private_key, algorithm=None)
    )
    key_path = output_dir / "identity.key"
    certificate_path = output_dir / CERTIFICATE_NAME
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    certificate_bytes = certificate.public_bytes(serialization.Encoding.DER)
    certificate_path.write_bytes(certificate_bytes)
    certificate_path.chmod(0o600)
    public_bytes = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "private_key": os.fspath(key_path),
        "certificate": os.fspath(certificate_path),
        "certificate_sha256": hashlib.sha256(certificate_bytes).hexdigest(),
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
    }


def attest(app_bundle: Path, private_key_path: Path, certificate_path: Path) -> Path:
    """Write a certificate-pinned startup attestation before final ad-hoc signing."""
    verify_packvm_bundle(app_bundle)
    resources = app_bundle / "Contents/Resources"
    destination_certificate = resources / CERTIFICATE_NAME
    destination_certificate.write_bytes(
        _read_bound_regular(certificate_path, "CI/E2E certificate", 16 * 1024)
    )
    destination_certificate.chmod(0o444)
    certificate_sha256 = _sha256(destination_certificate)
    files = [
        {"path": relative, "sha256": _attested_sha256(app_bundle, relative)}
        for relative in SIGNED_PATHS
    ]
    private_key = serialization.load_pem_private_key(
        _read_bound_regular(private_key_path, "CI/E2E private key", 32 * 1024),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("CI/E2E attestation key is not Ed25519")
    signature = private_key.sign(_message(certificate_sha256, files))
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "policy": POLICY,
        "bundle_identifier": BUNDLE_IDENTIFIER,
        "certificate_sha256": certificate_sha256,
        "files": files,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    destination = resources / ATTESTATION_NAME
    temporary = resources / f".{ATTESTATION_NAME}.tmp-{os.getpid()}"
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o444)
    os.replace(temporary, destination)
    return destination


def verify(app_bundle: Path, expected_certificate_sha256: str) -> None:
    """Verify the pinned certificate, fixed file identities, and Ed25519 signature."""
    verify_packvm_bundle(app_bundle)
    resources = app_bundle / "Contents/Resources"
    certificate_path = resources / CERTIFICATE_NAME
    certificate_bytes = _read_bound_regular(
        certificate_path, "CI/E2E certificate", 16 * 1024
    )
    actual_certificate_sha256 = hashlib.sha256(certificate_bytes).hexdigest()
    if actual_certificate_sha256 != expected_certificate_sha256:
        raise ValueError("CI/E2E certificate differs from the expected identity")
    certificate = x509.load_der_x509_certificate(certificate_bytes)
    public_key = certificate.public_key()
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("CI/E2E certificate public key is not Ed25519")
    public_key.verify(certificate.signature, certificate.tbs_certificate_bytes)
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if [attribute.value for attribute in common_names] != [
        "Tobkiri CI E2E Non-Publishable"
    ]:
        raise ValueError("CI/E2E certificate subject is outside its trust domain")
    document = _read_regular_json(
        resources / ATTESTATION_NAME, "CI/E2E startup attestation"
    )
    if set(document) != {
        "schema",
        "policy",
        "bundle_identifier",
        "certificate_sha256",
        "files",
        "signature",
    }:
        raise ValueError("CI/E2E attestation fields are invalid")
    if (
        document["schema"] != SCHEMA
        or document["policy"] != POLICY
        or document["bundle_identifier"] != BUNDLE_IDENTIFIER
        or document["certificate_sha256"] != expected_certificate_sha256
    ):
        raise ValueError("CI/E2E attestation domain is invalid")
    expected_files = [
        {"path": relative, "sha256": _attested_sha256(app_bundle, relative)}
        for relative in SIGNED_PATHS
    ]
    if document["files"] != expected_files:
        raise ValueError("CI/E2E attested file identity changed")
    signature = base64.b64decode(document["signature"], validate=True)
    public_key.verify(signature, _message(expected_certificate_sha256, expected_files))


def main() -> int:
    """Run the requested CI artifact identity operation."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create-identity")
    create_parser.add_argument("--output-dir", type=Path, required=True)
    attest_parser = subparsers.add_parser("attest")
    attest_parser.add_argument("--app-bundle", type=Path, required=True)
    attest_parser.add_argument("--private-key", type=Path, required=True)
    attest_parser.add_argument("--certificate", type=Path, required=True)
    packvm_write_parser = subparsers.add_parser("write-packvm-bundle-manifest")
    packvm_write_parser.add_argument("--app-bundle", type=Path, required=True)
    packvm_write_parser.add_argument(
        "--expected-signing-mode",
        choices=("ad-hoc", "developer-id"),
        required=True,
    )
    packvm_write_parser.add_argument("--expected-team-id")
    packvm_verify_parser = subparsers.add_parser("verify-packvm-bundle")
    packvm_verify_parser.add_argument("--app-bundle", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--app-bundle", type=Path, required=True)
    verify_parser.add_argument("--expected-certificate-sha256", required=True)
    args = parser.parse_args()
    if args.command == "create-identity":
        print(json.dumps(create_identity(args.output_dir), sort_keys=True))
    elif args.command == "attest":
        print(attest(args.app_bundle, args.private_key, args.certificate))
    elif args.command == "verify":
        verify(args.app_bundle, args.expected_certificate_sha256)
    elif args.command == "write-packvm-bundle-manifest":
        print(
            write_packvm_bundle_manifest(
                args.app_bundle,
                expected_signing_mode=args.expected_signing_mode,
                expected_team_id=args.expected_team_id,
            )
        )
    else:
        verify_packvm_bundle(args.app_bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
