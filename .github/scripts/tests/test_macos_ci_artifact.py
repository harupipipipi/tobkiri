"""Contracts for the non-publishable macOS CI/E2E signing domain."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/macos_ci_artifact.py"
WORKFLOW = ROOT / ".github/workflows/desktop-installers.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
RELEASE_VERIFIER = ROOT / "tobkiri_launcher/scripts/verify_macos_release.sh"
RUST_CI_ATTESTATION = ROOT / "tobkiri_launcher/src-tauri/src/sealed_python.rs"
spec = importlib.util.spec_from_file_location("macos_ci_artifact", SCRIPT)
assert spec is not None and spec.loader is not None
macos_ci_artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(macos_ci_artifact)
_INSPECT_PACKVM_HELPER_SIGNING = macos_ci_artifact._inspect_packvm_helper_signing


@pytest.fixture(autouse=True)
def _fixture_helper_is_ad_hoc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic Mach-O fixtures independent of the host codesign tool."""
    monkeypatch.setattr(
        macos_ci_artifact,
        "_inspect_packvm_helper_signing",
        lambda _path: {
            "signing_mode": "ad-hoc",
            "team_id": None,
            "authority": None,
        },
    )


def _bundle(root: Path) -> Path:
    """Create the exact startup-critical CI bundle paths."""
    bundle = root / "Tobkiri Launcher CI E2E.app"
    for relative in macos_ci_artifact.SIGNED_PATHS:
        if relative == f"Contents/Resources/{macos_ci_artifact.CERTIFICATE_NAME}":
            continue
        if relative == macos_ci_artifact.PACKVM_BUNDLE_MANIFEST_RELATIVE:
            continue
        path = bundle / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative in macos_ci_artifact.MACHO_SIGNED_PATHS:
            header = bytearray(32)
            header[:4] = b"\xcf\xfa\xed\xfe"
            struct.pack_into("<II", header, 16, 2, 88)
            linkedit = bytearray(72)
            struct.pack_into("<II16s", linkedit, 0, 0x19, 72, b"__LINKEDIT")
            struct.pack_into("<QQ", linkedit, 32, 4, 120)
            struct.pack_into("<Q", linkedit, 48, 4)
            signature = struct.pack("<IIII", 0x1D, 16, 120, 4)
            path.write_bytes(header + linkedit + signature + b"SIGN")
        elif relative == macos_ci_artifact.PACKVM_PROVISIONING_MANIFEST_RELATIVE:
            inputs = []
            bubblewrap_package = (
                b"b" * macos_ci_artifact.PACKVM_BUBBLEWRAP_PACKAGE_BYTES
            )
            for name in sorted(macos_ci_artifact.PACKVM_REQUIRED_PROVISIONING_NAMES):
                input_relative = f"packvm-vz-provisioning/{name}.fixture"
                if name == "bubblewrap_package":
                    input_relative = "packvm-vz-provisioning/bubblewrap_arm64.deb"
                input_path = bundle / "Contents/Resources" / input_relative
                input_path.parent.mkdir(parents=True, exist_ok=True)
                if name == "bubblewrap_package":
                    payload = bubblewrap_package
                elif name == "bubblewrap_descriptor":
                    payload = json.dumps(
                        {
                            "schema": macos_ci_artifact.PACKVM_BUBBLEWRAP_DESCRIPTOR_SCHEMA,
                            "package": "bubblewrap",
                            "version": "fixture",
                            "architecture": "arm64",
                            "source": {
                                "url": "https://example.test/bubblewrap.deb",
                                "size_bytes": len(bubblewrap_package),
                                "sha256": "sha256:"
                                + hashlib.sha256(bubblewrap_package).hexdigest(),
                            },
                        },
                        sort_keys=True,
                    ).encode()
                else:
                    payload = f"fixture:{name}".encode()
                input_path.write_bytes(payload)
                inputs.append(
                    {
                        "name": name,
                        "path": input_relative,
                        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    }
                )
            path.write_text(
                json.dumps(
                    {
                        "schema": macos_ci_artifact.PACKVM_PROVISIONING_SCHEMA,
                        "target": "aarch64-apple-darwin",
                        "boot_mode": "efi",
                        "inputs": inputs,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        else:
            path.write_bytes(f"fixture:{relative}".encode())
    macos_ci_artifact.write_packvm_bundle_manifest(bundle)
    return bundle


def test_ephemeral_identity_attests_and_detects_tampering(tmp_path: Path) -> None:
    """Certificate, signature, and every fixed startup identity fail closed."""
    identity = macos_ci_artifact.create_identity(tmp_path / "identity")
    bundle = _bundle(tmp_path)
    macos_ci_artifact.attest(
        bundle, Path(identity["private_key"]), Path(identity["certificate"])
    )
    macos_ci_artifact.verify(bundle, identity["certificate_sha256"])

    executable = bundle / macos_ci_artifact.SIGNED_PATHS[0]
    executable_bytes = bytearray(executable.read_bytes())
    executable_bytes[4] ^= 1
    executable.write_bytes(executable_bytes)
    with pytest.raises(ValueError, match="attested file identity changed"):
        macos_ci_artifact.verify(bundle, identity["certificate_sha256"])


def test_codesign_size_rewrite_preserves_canonical_executable_identity(
    tmp_path: Path,
) -> None:
    """Only signature-blob and __LINKEDIT size changes are canonicalized."""
    bundle = _bundle(tmp_path)
    executable = bundle / macos_ci_artifact.SIGNED_PATHS[0]
    before = macos_ci_artifact._macho_code_sha256(executable)
    data = bytearray(executable.read_bytes())
    struct.pack_into("<Q", data, 32 + 32, 8192)
    struct.pack_into("<Q", data, 32 + 48, 8192)
    struct.pack_into("<II", data, 32 + 72 + 8, 120, 8)
    data[120:] = b"NEWSIGN!"
    executable.write_bytes(data)
    assert macos_ci_artifact._macho_code_sha256(executable) == before


def test_identity_and_domain_swaps_are_rejected(tmp_path: Path) -> None:
    """A different certificate and a rewritten domain cannot reuse authority."""
    identity = macos_ci_artifact.create_identity(tmp_path / "identity")
    other = macos_ci_artifact.create_identity(tmp_path / "other")
    bundle = _bundle(tmp_path)
    macos_ci_artifact.attest(
        bundle, Path(identity["private_key"]), Path(identity["certificate"])
    )
    with pytest.raises(ValueError, match="certificate differs"):
        macos_ci_artifact.verify(bundle, other["certificate_sha256"])

    attestation_path = (
        bundle / "Contents/Resources" / macos_ci_artifact.ATTESTATION_NAME
    )
    document = json.loads(attestation_path.read_text(encoding="utf-8"))
    document["bundle_identifier"] = "dev.tobkiri.launcher"
    attestation_path.chmod(0o644)
    attestation_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="attestation domain is invalid"):
        macos_ci_artifact.verify(bundle, identity["certificate_sha256"])


def test_packvm_asset_or_helper_tampering_is_rejected(tmp_path: Path) -> None:
    """The helper manifest seals every immutable launch asset independently."""
    bundle = _bundle(tmp_path)
    provisioning = (
        bundle / "Contents/Resources/packvm-vz-provisioning/guest_runner.fixture"
    )
    provisioning.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="PackVM provisioning identity changed"):
        macos_ci_artifact.verify_packvm_bundle(bundle)

    bundle = _bundle(tmp_path / "helper")
    helper = bundle / macos_ci_artifact.PACKVM_HELPER_RELATIVE
    helper_bytes = bytearray(helper.read_bytes())
    helper_bytes[4] ^= 1
    helper.write_bytes(helper_bytes)
    with pytest.raises(ValueError, match="PackVM helper code identity changed"):
        macos_ci_artifact.verify_packvm_bundle(bundle)


def test_packvm_bundle_manifest_matches_production_runtime_digest_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feed the generated Developer ID manifest through the real runtime parser."""

    monkeypatch.setattr(
        macos_ci_artifact,
        "_inspect_packvm_helper_signing",
        lambda _path: {
            "signing_mode": "developer-id",
            "team_id": "ABCDEFGHIJ",
            "authority": "Developer ID Application: Tobkiri Test (ABCDEFGHIJ)",
        },
    )
    bundle = _bundle(tmp_path)
    document = json.loads(
        (bundle / macos_ci_artifact.PACKVM_BUNDLE_MANIFEST_RELATIVE).read_text(
            encoding="utf-8"
        )
    )
    assert document["helper"]["code_sha256"].startswith("sha256:")
    assert document["provisioning"]["sha256"].startswith("sha256:")

    runtime_root = ROOT / "tobkiri_runtime"
    sys.path.insert(0, os.fspath(runtime_root))
    try:
        from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
            MacOSVZProvisioner,
        )

        parsed = MacOSVZProvisioner(
            state_dir=tmp_path / "state",
            bundle_root=bundle,
            platform_system="Darwin",
            machine="arm64",
        )._parse_bundle_helper_manifest(
            bundle / "Contents/Resources",
            bundle / "Contents/Resources/packvm-vz-provisioning.v1.json",
        )
    finally:
        sys.path.remove(os.fspath(runtime_root))

    assert parsed["helper_digest"] == document["helper"]["code_sha256"]
    assert parsed["helper_bundle_id"] == macos_ci_artifact.PACKVM_HELPER_IDENTIFIER
    assert parsed["helper_team_id"] == "ABCDEFGHIJ"


def test_packvm_bubblewrap_package_is_present_sized_and_descriptor_pinned(
    tmp_path: Path,
) -> None:
    """The offline guest isolation package cannot drift from its descriptor."""
    bundle = _bundle(tmp_path)
    package = bundle / "Contents/Resources/packvm-vz-provisioning/bubblewrap_arm64.deb"
    package.unlink()
    with pytest.raises(ValueError, match="PackVM provisioning input is unavailable"):
        macos_ci_artifact.verify_packvm_bundle(bundle)

    bundle = _bundle(tmp_path / "descriptor")
    package = bundle / "Contents/Resources/packvm-vz-provisioning/bubblewrap_arm64.deb"
    package.write_bytes(b"x" * macos_ci_artifact.PACKVM_BUBBLEWRAP_PACKAGE_BYTES)
    manifest_path = bundle / macos_ci_artifact.PACKVM_PROVISIONING_MANIFEST_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["inputs"]:
        if entry["name"] == "bubblewrap_package":
            entry["sha256"] = (
                "sha256:" + hashlib.sha256(package.read_bytes()).hexdigest()
            )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        ValueError, match="PackVM bubblewrap package binding is invalid"
    ):
        macos_ci_artifact._verify_packvm_provisioning(bundle)


@pytest.mark.parametrize(
    "relative",
    [
        macos_ci_artifact.PACKVM_PROVISIONING_MANIFEST_RELATIVE,
        macos_ci_artifact.PACKVM_BUNDLE_MANIFEST_RELATIVE,
        macos_ci_artifact.PACKVM_HELPER_RELATIVE,
    ],
)
def test_packvm_bundle_rejects_symlinked_or_hardlinked_bound_material(
    tmp_path: Path, relative: str
) -> None:
    """Every helper/manifest read binds one regular inode rather than a pathname."""
    bundle = _bundle(tmp_path / "hardlink")
    path = bundle / relative
    os.link(path, path.with_name(f"{path.name}.linked"))
    with pytest.raises(ValueError, match="malformed|singly-linked"):
        macos_ci_artifact.verify_packvm_bundle(bundle)

    bundle = _bundle(tmp_path / "symlink")
    path = bundle / relative
    replacement = path.with_name(f"{path.name}.replacement")
    path.rename(replacement)
    path.symlink_to(replacement)
    with pytest.raises(ValueError, match="malformed|singly-linked"):
        macos_ci_artifact.verify_packvm_bundle(bundle)


def test_descriptor_bound_hash_rejects_a_path_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor retaining old bytes cannot mask a post-open pathname swap."""
    target = tmp_path / "bound-input"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"original-input")
    replacement.write_bytes(b"replacement-input")
    read = macos_ci_artifact.os.read
    swapped = False

    def swap_then_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            replacement.replace(target)
        return read(descriptor, size)

    monkeypatch.setattr(macos_ci_artifact.os, "read", swap_then_read)
    with pytest.raises(ValueError, match="identity changed during read"):
        macos_ci_artifact._sha256(target)


def test_helper_identity_is_measured_from_codesign_not_cli_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only actual codesign output can establish a Developer ID helper."""
    helper = tmp_path / "tobkiri-packvm-vz-helper"
    helper.write_bytes(b"fixture")

    class Completed:
        """Small completed-process substitute for the inspected command output."""

        def __init__(self, stdout: str | bytes, stderr: str | bytes = b"") -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = stderr

    entitlement = (
        b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        b"<key>com.apple.security.virtualization</key><true/></dict></plist>"
    )
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> Completed:
        calls.append(command)
        if "--entitlements" in command:
            return Completed(entitlement)
        return Completed(
            "\n".join(
                (
                    "Identifier=dev.tobkiri.launcher.packvm-vz-helper",
                    "TeamIdentifier=ABCDE12345",
                    "Authority=Developer ID Application: Tobkiri (ABCDE12345)",
                )
            ),
            "",
        )

    monkeypatch.setattr(macos_ci_artifact.subprocess, "run", run)
    assert _INSPECT_PACKVM_HELPER_SIGNING(helper) == {
        "signing_mode": "developer-id",
        "team_id": "ABCDE12345",
        "authority": "Developer ID Application: Tobkiri (ABCDE12345)",
    }
    assert any("--entitlements" in command for command in calls)
    with pytest.raises(ValueError, match="production helper identity"):
        macos_ci_artifact._validate_packvm_helper_signing(
            {
                "signing_mode": "developer-id",
                "team_id": "ABCDE12345",
                "authority": "Developer ID Application: Tobkiri (ZZZZZ99999)",
            }
        )


def test_helper_identity_rejects_nonexact_entitlements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A helper with extra privilege cannot be represented by the manifest."""
    helper = tmp_path / "tobkiri-packvm-vz-helper"
    helper.write_bytes(b"fixture")

    class Completed:
        def __init__(self, stdout: str | bytes, stderr: str | bytes = b"") -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = stderr

    def run(command: list[str], **_kwargs: object) -> Completed:
        if "--entitlements" in command:
            return Completed(
                b"<plist><dict><key>com.apple.security.virtualization</key>"
                b"<true/><key>com.apple.security.get-task-allow</key>"
                b"<true/></dict></plist>"
            )
        return Completed(
            "Identifier=dev.tobkiri.launcher.packvm-vz-helper\nSignature=adhoc",
            "",
        )

    monkeypatch.setattr(macos_ci_artifact.subprocess, "run", run)
    with pytest.raises(ValueError, match="entitlements are not exact"):
        _INSPECT_PACKVM_HELPER_SIGNING(helper)


def test_workflow_never_mutates_keychain_or_trust_state() -> None:
    """The no-Apple-identity path remains file-scoped and non-publishable."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in (
        "add-trusted-cert",
        "create-keychain",
        "import identity.p12",
        "set-key-partition-list",
        "sudo ",
    ):
        assert forbidden not in workflow
    assert "TOBKIRI_MACOS_ARTIFACT_POLICY=ci-e2e-v1" in workflow
    assert "--sign -" in workflow
    assert "tobkiri-non-publishable-ci-e2e-" in workflow
    assert "write-packvm-bundle-manifest" in workflow
    assert "--expected-signing-mode ad-hoc" in workflow
    assert "--entitlements tobkiri_launcher/packvm-vz-helper/Entitlements/" in workflow
    release_workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "--expected-signing-mode developer-id" in release_workflow
    assert '--expected-team-id "$APPLE_TEAM_ID"' in release_workflow


def test_rust_cold_boot_attestation_mirrors_the_python_path_policy() -> None:
    """The packaged Launcher independently verifies the sidecar attestations."""
    source = RUST_CI_ATTESTATION.read_text(encoding="utf-8")
    for relative in macos_ci_artifact.SIGNED_PATHS:
        assert f'"{relative}",' in source
    for relative in macos_ci_artifact.MACHO_SIGNED_PATHS:
        assert f'"{relative}",' in source
    assert "MACOS_CI_MACHO_ATTESTED_PATHS.contains(relative)" in source


def test_production_release_guard_rejects_ci_domain_artifacts() -> None:
    """Production publication rejects all CI names and signed policy files."""
    verifier = RELEASE_VERIFIER.read_text(encoding="utf-8")
    assert "dev.tobkiri.launcher.ci-e2e" not in verifier
    for marker in (
        "NON_PUBLISHABLE_CI_E2E_ARTIFACT.txt",
        "ci-e2e-artifact-policy.v1.json",
        "ci-e2e-signing-certificate.der",
        "ci-e2e-startup-attestation.v1.json",
    ):
        assert marker in verifier
