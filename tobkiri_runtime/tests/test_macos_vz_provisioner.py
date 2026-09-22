"""Focused no-download tests for direct macOS VZ allocation facts."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest

import ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner as macos_vz_provisioner
from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
    MacOSVZAssetManifest,
    MacOSVZProvisioner,
    VZ_ASSET_MANIFEST_SCHEMA,
    VZ_BUNDLE_MANIFEST_SCHEMA,
    VZ_RAW_EFI_IMAGE_DECLARED_BYTES,
    _MacOSVZHelperProcess,
    _file_digest,
    _parse_image_descriptor,
)
from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.macos_vz_supervisor import (
    MacOSVZAgentIdentity,
    MacOSVZHelperIdentity,
    MacOSVZLaunchAssets,
    MacOSVZSupervisorDriver,
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _private_file(path: Path, payload: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(payload)
    path.chmod(mode)
    return path


def test_packvm_lifecycle_exposes_only_its_verified_backend_registration(
    tmp_path: Path,
) -> None:
    """Production capture reuses the lifecycle-owned provisioner facts."""

    facts = object()

    class Provisioner:
        state_path = tmp_path / "packvm-vz-attestation.json"

        def prepare_direct_vz(self) -> object:
            return facts

    lifecycle = PackVMLifecycleV4(provisioner=Provisioner())  # type: ignore[arg-type]

    assert lifecycle.production_backend_registration() is facts


@pytest.fixture
def provisioner_fixture(tmp_path: Path) -> tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path]:
    """Build tiny verified inputs without a VM image download or VZ helper."""

    assets = tmp_path / "assets"
    assets.mkdir(mode=0o700)
    helper = _private_file(assets / "tobkiri-packvm-vz-helper", b"test-helper", 0o700)
    runner = _private_file(assets / "runner.py", b"print('runner')\n", 0o444)
    service = _private_file(
        assets / "guest_service_template.v1.json", b'{"service":"test"}\n', 0o444
    )
    bubblewrap = _private_file(assets / "bubblewrap_arm64.deb", b"deb", 0o444)
    descriptor = _private_file(
        assets / "bubblewrap_descriptor.v1.json", b'{"descriptor":"test"}\n', 0o444
    )
    cloud = _private_file(assets / "cloud-init.yaml", b"#cloud-config\n", 0o444)
    base = _private_file(tmp_path / "verified-base.raw", b"raw-efi-image")
    manifest = MacOSVZAssetManifest(
        helper_path=helper,
        helper_digest=_digest(helper.read_bytes()),
        helper_bundle_id="dev.tobkiri.launcher.packvm-vz-helper",
        helper_team_id="ABCDEFGHIJ",
        helper_signing_identity="Developer ID Application: Test (ABCDEFGHIJ)",
        agent_path=runner,
        agent_digest=_digest(runner.read_bytes()),
        guest_service_path=service,
        guest_service_digest=_digest(service.read_bytes()),
        bubblewrap_path=bubblewrap,
        bubblewrap_digest=_digest(bubblewrap.read_bytes()),
        bubblewrap_descriptor_path=descriptor,
        bubblewrap_descriptor_digest=_digest(descriptor.read_bytes()),
        config_path=cloud,
        config_digest=_digest(cloud.read_bytes()),
        image_source="https://example.invalid/direct.raw",
        image_digest=_file_digest(base),
        image_sha512=None,
        image_size_bytes=VZ_RAW_EFI_IMAGE_DECLARED_BYTES,
        architecture="aarch64-apple-darwin",
        manifest_digest=_digest(b"fixture"),
    )

    def prepare_efi(_root: Path, domain: str, path: Path, _key: bytes) -> dict[str, object]:
        _private_file(path, b"efi-store")
        metadata = path.stat()
        return {
            "domain_id": domain,
            "state": "prepared",
            "path": str(path),
            "digest": _file_digest(path),
            "device": str(metadata.st_dev),
            "inode": str(metadata.st_ino),
        }

    provisioner = MacOSVZProvisioner(
        state_dir=(tmp_path / "state").resolve(),
        platform_system="darwin",
        machine="arm64",
        clone_file=shutil.copyfile,
        efi_store_preparer=prepare_efi,
        helper_identity_verifier=lambda _manifest: (True, None),
    )
    state = {
        "base_image_path": str(base),
        "image_digest": manifest.image_digest,
        "attestation_digest": _digest(b"attestation"),
    }
    provisioner._require_manifest = lambda: manifest  # type: ignore[method-assign]
    provisioner._load_state = lambda: state  # type: ignore[method-assign]
    provisioner._verify_state_bindings = (  # type: ignore[method-assign]
        lambda _state, _manifest: None
    )
    return provisioner, manifest, base


def test_allocate_creates_per_domain_cow_efi_and_seeds(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
) -> None:
    """Each allocation owns generated keys/seeds and cleans all mutable files."""

    provisioner, manifest, base = provisioner_fixture
    allocation = provisioner.allocate(
        domain_id="domain.conversation",
        reservation_id="reservation-1",
        lease_id="lease-1",
        channel_key=b"k" * 32,
        artifact_digest=_digest(b"artifact"),
        executable_digest=_digest(b"executable"),
        materialization_digest=_digest(b"materialization"),
    )

    root = Path(allocation.run_root)
    assert Path(allocation.cow_disk_path).read_bytes() == base.read_bytes()
    assert allocation.cow_disk_digest == manifest.image_digest
    assert Path(allocation.efi_store_path).is_file()
    assert Path(allocation.agent_seed_path).stat().st_size % 512 == 0
    assert Path(allocation.config_seed_path).stat().st_size % 512 == 0
    agent_seed = Path(allocation.agent_seed_path).read_bytes()
    config_seed = Path(allocation.config_seed_path).read_bytes()
    assert b"guest_service_template.v1.json;1" in agent_seed
    assert b"bubblewrap_descriptor.v1.json;1" in agent_seed
    assert b"bubblewrap_arm64.deb;1" in agent_seed
    assert b"agent-config.json;1" in config_seed
    assert b"agent-ed25519.pem;1" in config_seed
    assert b"BEGIN PRIVATE KEY" in config_seed
    assert _digest(b"artifact") in config_seed.decode("latin1")
    assert allocation.guest_public_key_digest in config_seed.decode("latin1")
    assert not (root / "agent-ed25519.pem").exists()
    assert not (root / "agent-config.json").exists()

    provisioner.release(allocation)
    assert not root.exists()


def test_prepare_declares_three_gib_download_without_downloading(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan asks for explicit consent and capacity before fetching 3 GiB."""

    provisioner, manifest, _base = provisioner_fixture
    monkeypatch.setattr(provisioner, "_load_manifest_for_plan", lambda: (manifest, None))
    monkeypatch.setattr(provisioner.image_cache, "status", lambda _authority: ("absent", None))
    monkeypatch.setattr(
        provisioner,
        "_disk_usage",
        lambda _path: SimpleNamespace(free=20 * 1024 * 1024 * 1024),
    )

    plan = provisioner.prepare()

    assert plan.image_download_required is True
    assert plan.image_download_bytes == VZ_RAW_EFI_IMAGE_DECLARED_BYTES
    assert plan.image_size_bytes == VZ_RAW_EFI_IMAGE_DECLARED_BYTES


def test_doctor_reports_bounded_reason_before_provisioning(tmp_path: Path) -> None:
    """A fresh install reports not-provisioned without exposing its host path."""

    state_dir = (tmp_path / "private-user-state" / "packvm-vz").resolve()
    provisioner = MacOSVZProvisioner(
        state_dir=state_dir,
        platform_system="darwin",
        machine="arm64",
    )

    doctor = provisioner.doctor()

    assert doctor.ready is False
    assert doctor.reason == "PackVM VZ has not completed authenticated provisioning"
    assert str(tmp_path) not in str(doctor.reason)


def test_seed_and_template_tampering_are_rejected_separately(
    provisioner_fixture: tuple[MacOSVZProvisioner, MacOSVZAssetManifest, Path],
) -> None:
    """Generated CIDATA and immutable cloud-init are independently measured."""

    provisioner, manifest, base = provisioner_fixture
    allocation = provisioner.allocate(
        domain_id="domain.first",
        reservation_id="reservation-1",
        lease_id="lease-1",
        channel_key=b"k" * 32,
        artifact_digest=_digest(b"artifact"),
        executable_digest=_digest(b"executable"),
        materialization_digest=_digest(b"materialization"),
    )
    driver = MacOSVZSupervisorDriver(
        transport_factory=lambda _allocation: None,
        helper_path=manifest.helper_path,
        helper_identity=MacOSVZHelperIdentity(
            binary_digest=manifest.helper_digest,
            code_digest=manifest.helper_digest,
            bundle_id=manifest.helper_bundle_id,
            team_id=manifest.helper_team_id,
            signing_identity=manifest.helper_signing_identity,
        ),
        launch_assets=MacOSVZLaunchAssets(
            base_image_digest=_file_digest(base),
            base_image_path=str(base),
            agent_template_digest=manifest.agent_digest,
            config_template_digest=manifest.config_digest,
            base_image_read_only=True,
        ),
        agent_identity=MacOSVZAgentIdentity(agent_digest=manifest.agent_digest),
        domain_allocator=None,
    )
    driver._verify_launch_assets(allocation)
    config_seed = Path(allocation.config_seed_path)
    _private_file(config_seed, config_seed.read_bytes() + b"tampered")
    with pytest.raises(BackendUnavailableError, match="config seed digest mismatch"):
        driver._verify_launch_assets(allocation)

    provisioner.release(allocation)
    manifest.config_path.chmod(0o600)
    _private_file(manifest.config_path, b"#cloud-config\ntampered\n", 0o444)
    with pytest.raises(ValueError, match="asset changed"):
        provisioner.allocate(
            domain_id="domain.second",
            reservation_id="reservation-2",
            lease_id="lease-2",
            channel_key=b"l" * 32,
            artifact_digest=_digest(b"artifact-2"),
            executable_digest=_digest(b"executable-2"),
            materialization_digest=_digest(b"materialization-2"),
        )


def test_image_descriptor_rejects_redirecting_debian_origin(tmp_path: Path) -> None:
    """The downloader only accepts the fixed direct-200 source URL."""

    descriptor = _private_file(
        tmp_path / "image.json",
        (
            b'{"schema":"io.tobkiri.packvm-vz-image-descriptor.v1",'
            b'"boot_mode":"efi","architecture":"arm64","format":"raw",'
            b'"source":{"url":"https://cloud.debian.org/redirect.raw",'
            b'"size_bytes":3221225472,'
            b'"sha256":"sha256:9440bc19285b9e0ccb217fd5ac818a253a3c0bfd46c9ac83241959c78f90ad71",'
            b'"sha512":"f21843e29eade9747b1b7bb7d9622c30613eb3d875fbb6a7f9bd76acaadfdbfe0ef68137da4eb7520e440a6cd3bbb248db41aa322f58d11e71fea667eb569a2c"},'
            b'"license":{"spdx_id":"LicenseRef-Debian-Distribution",'
            b'"url":"https://www.debian.org/legal/licenses/"}}'
        ),
        0o444,
    )

    with pytest.raises(ValueError, match="image descriptor"):
        _parse_image_descriptor(descriptor)


def test_authenticated_bundle_binding_rehashes_both_resource_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each manifest load rejects a post-launch resource-manifest replacement."""

    provisioner, provisioning_path, _resources, binding = _bound_bundle_fixture(tmp_path)
    sentinel = object()
    monkeypatch.setattr(provisioner, "_parse_provisioning_manifest", lambda *_args: sentinel)
    monkeypatch.setattr(provisioner, "_verify_helper_identity", lambda _manifest: (True, None))

    assert provisioner._require_manifest() is sentinel
    provisioning_path.chmod(0o600)
    _private_file(provisioning_path, b'{"replaced":true}', 0o444)

    with pytest.raises(ValueError, match="bundle binding changed"):
        provisioner._require_manifest()

    assert binding.provisioning_sha256 != _file_digest(provisioning_path)


def test_authenticated_bundle_binding_is_the_expected_helper_team_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutable helper manifest cannot select a different Developer ID team."""

    provisioner, provisioning_path, resources, binding = _bound_bundle_fixture(tmp_path)
    monkeypatch.setattr(
        macos_vz_provisioner,
        "_macho_code_digest",
        lambda _path: _digest(b"helper-code"),
    )

    parsed = provisioner._parse_bundle_helper_manifest(resources, provisioning_path)
    assert parsed["helper_team_id"] == binding.helper_team_id

    mismatched_binding = SimpleNamespace(
        root=binding.root,
        provisioning_sha256=binding.provisioning_sha256,
        helper_manifest_sha256=binding.helper_manifest_sha256,
        helper_team_id="KLMNOPQRST",
    )
    mismatched = MacOSVZProvisioner(
        state_dir=(tmp_path / "mismatched-state").resolve(),
        bundle_binding=mismatched_binding,
        platform_system="darwin",
        machine="arm64",
    )
    with pytest.raises(ValueError, match="Team ID binding changed"):
        mismatched._parse_bundle_helper_manifest(resources, provisioning_path)


def _bound_bundle_fixture(
    tmp_path: Path,
) -> tuple[MacOSVZProvisioner, Path, Path, SimpleNamespace]:
    """Build only the two Launcher-attested resource manifests for a unit test."""

    root = tmp_path / "Tobkiri Launcher.app"
    resources = root / "Contents" / "Resources"
    helper_path = root / "Contents" / "MacOS" / "tobkiri-packvm-vz-helper"
    resources.mkdir(parents=True)
    helper_path.parent.mkdir()
    _private_file(helper_path, b"helper", 0o700)
    provisioning_path = _private_file(
        resources / "packvm-vz-provisioning.v1.json",
        json.dumps(
            {
                "schema": VZ_ASSET_MANIFEST_SCHEMA,
                "target": "aarch64-apple-darwin",
                "boot_mode": "efi",
                "inputs": [],
            }
        ).encode("utf-8"),
        0o444,
    )
    team_id = "ABCDEFGHIJ"
    helper_manifest = {
        "schema": VZ_BUNDLE_MANIFEST_SCHEMA,
        "helper": {
            "path": "Contents/MacOS/tobkiri-packvm-vz-helper",
            "code_sha256": _digest(b"helper-code"),
            "identifier": "dev.tobkiri.launcher.packvm-vz-helper",
            "entitlements": ["com.apple.security.virtualization"],
            "signing": {
                "signing_mode": "developer-id",
                "team_id": team_id,
                "authority": f"Developer ID Application: Tobkiri ({team_id})",
            },
        },
        "provisioning": {
            "path": "Contents/Resources/packvm-vz-provisioning.v1.json",
            "sha256": _file_digest(provisioning_path),
        },
    }
    helper_manifest_path = _private_file(
        resources / "packvm-vz-helper.manifest.v1.json",
        json.dumps(helper_manifest, sort_keys=True).encode("utf-8"),
        0o444,
    )
    binding = SimpleNamespace(
        root=root,
        provisioning_sha256=_file_digest(provisioning_path),
        helper_manifest_sha256=_file_digest(helper_manifest_path),
        helper_team_id=team_id,
    )
    return (
        MacOSVZProvisioner(
            state_dir=(tmp_path / "state").resolve(),
            bundle_binding=binding,
            platform_system="darwin",
            machine="arm64",
        ),
        provisioning_path,
        resources,
        binding,
    )


def test_transport_requires_explicit_fd_key_binding_before_exchange() -> None:
    """A domain helper cannot accept an outer request before enrollment."""

    process = _helper_process_for_test(b'{"ok":true}\n')
    request = {
        "operation": "launch",
        "domain_id": "domain.test",
        "launch_binding_digest": _digest(b"launch"),
    }
    with pytest.raises(ValueError, match="transport binding"):
        process.exchange(request)

    process.enroll_launch_secret(
        domain_id="domain.test",
        host_nonce="a" * 64,
        launch_binding_digest=_digest(b"launch"),
        secret=b"k" * 32,
    )
    assert process.exchange(request) == {"ok": True}


def test_transport_accepts_one_mebibyte_protocol_lines_not_state_limit() -> None:
    """Helper protocol frames may carry bounded bridge payloads above 128 KiB."""

    valid = json.dumps({"payload": "x" * (200 * 1024)}).encode() + b"\n"
    process = _helper_process_for_test(valid)
    assert len(process._exchange_line({"request": "ok"})["payload"]) == 200 * 1024

    oversized = b"x" * (1024 * 1024 + 1)
    with pytest.raises(ValueError, match="response exceeds"):
        _helper_process_for_test(oversized)._exchange_line({"request": "ok"})


def test_lifecycle_never_selects_lima_as_its_default() -> None:
    """A normal lifecycle chooses direct VZ and fails closed off supported hosts."""

    lifecycle = PackVMLifecycleV4()

    assert isinstance(lifecycle._provisioner, MacOSVZProvisioner)


def _helper_process_for_test(response: bytes) -> _MacOSVZHelperProcess:
    """Construct a process-free transport shell for framing tests."""

    instance = object.__new__(_MacOSVZHelperProcess)
    instance._process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=_Readline(response),
        poll=lambda: None,
    )
    instance._key = bytearray(b"k" * 32)
    instance._lock = threading.RLock()
    instance._closed = False
    instance._domain_id = None
    instance._launch_binding_digest = None
    return instance


class _Readline:
    """Bound-aware binary stdout fixture for a helper process."""

    def __init__(self, response: bytes) -> None:
        self._response = response

    def readline(self, _limit: int) -> bytes:
        return self._response
