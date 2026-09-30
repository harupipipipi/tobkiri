"""Linux lifecycle unit evidence only; these tests do not run a VM."""

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.packvm_lifecycle_v4 import PackVMProvisioningRequest
from ecosystem.defaultspack.backend.sandbox.isolation import linux_qemu_assets as assets
from ecosystem.defaultspack.backend.sandbox.isolation import linux_qemu_provisioner as module


def test_kvm_absence_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "kvm_capability", lambda: (False, "KVM unavailable"))
    p = module.LinuxQemuProvisioner(state_dir=tmp_path / "state")
    assert p.doctor().ready is False
    assert p.prepare().launcher_reason == "KVM unavailable"
    assert p.production_backend_registration() is None


def test_unbound_bundle_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "kvm_capability", lambda: (True, None))
    p = module.LinuxQemuProvisioner(state_dir=tmp_path / "state")
    assert "Launcher-verified" in p.prepare().launcher_reason
    assert p.production_backend_registration() is None


def test_unsafe_assets_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"fixture")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        assets.file_digest(link)
    target.chmod(0o660)
    with pytest.raises(ValueError):
        assets.file_digest(target)
    target.chmod(0o600)
    os.link(target, tmp_path / "hardlink")
    with pytest.raises(ValueError):
        assets.file_digest(target)


@pytest.fixture
def registration(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "kvm_capability", lambda: (True, None))
    p = module.LinuxQemuProvisioner(state_dir=tmp_path / "state")
    fake = SimpleNamespace(
        manifest_digest="sha256:" + "1" * 64,
        image_source="https://example.invalid/pinned.raw",
        files={
            name: SimpleNamespace(digest="sha256:" + "2" * 64, size_bytes=1)
            for name in ("image", "qemu", "agent", "config")
        },
    )
    monkeypatch.setattr(p, "_assets", lambda: fake)
    plan = p.prepare()
    request = PackVMProvisioningRequest(plan.plan_digest, plan.ceremony_nonce, plan.confirmation)
    return p, request


def test_consent_consumed_once_and_state_authenticated(registration):
    p, request = registration
    assert p.provision(request).ready is True
    with pytest.raises(ValueError, match="consumed"):
        p.provision(request)
    original = p.state_path.read_bytes()
    p.state_path.write_bytes(original.replace(b'"stopped":false', b'"stopped":true'))
    assert p.doctor().ready is False
    assert "authentication" in p.doctor().reason


def test_changed_consent_never_writes_state(registration):
    p, request = registration
    with pytest.raises(ValueError, match="consent"):
        p.provision(replace(request, plan_digest="sha256:" + "3" * 64))
    assert not p.state_path.exists()


def test_stop_is_exact_and_orphans_not_erased(registration):
    p, request = registration
    p.provision(request)
    with pytest.raises(ValueError, match="confirmation"):
        p.stop("STOP wrong")
    p.stop(f"STOP {module.INSTANCE}")
    assert p.doctor().ready is False
    orphan = p.state_path.parent / "domains" / "unknown"
    orphan.mkdir(parents=True, mode=0o700)
    with pytest.raises(ValueError, match="recovery"):
        p.cleanup(f"DELETE {module.INSTANCE}")
    assert orphan.exists()


def test_unknown_cleanup_residue_retained(tmp_path):
    root = tmp_path / "allocation"
    root.mkdir(mode=0o700)
    (root / "unrelated.txt").write_text("retain")
    with pytest.raises(ValueError, match="residue"):
        module._remove_allocation(root)
    assert (root / "unrelated.txt").exists()


def test_development_requires_absolute_root(monkeypatch):
    monkeypatch.setenv("RUMI_ENVIRONMENT", "development")
    monkeypatch.setenv("RUMI_USER_DATA", "relative")
    with pytest.raises(ValueError, match="absolute"):
        module.default_linux_packvm_provisioner()


def test_stopped_registration_resumes_only_after_exact_update_consent(registration):
    p, request = registration
    p.provision(request)
    p.stop(f"STOP {module.INSTANCE}")
    plan = p.prepare()
    assert plan.registration_update is not None
    missing = PackVMProvisioningRequest(plan.plan_digest, plan.ceremony_nonce, plan.confirmation)
    with pytest.raises(ValueError, match="update consent"):
        p.provision(missing)
    plan = p.prepare()
    resumed = PackVMProvisioningRequest(
        plan.plan_digest,
        plan.ceremony_nonce,
        plan.confirmation,
        previous_attestation_digest=plan.registration_update["previous_attestation_digest"],
    )
    assert p.provision(resumed).ready is True


def test_provision_recovery_requires_complete_exact_proof(registration):
    p, request = registration
    p.provision(request)
    with pytest.raises(ValueError, match="proof"):
        p.recover_provision_operation({})
    state = p._state()
    keys = {
        "backend_id",
        "instance",
        "session_digest",
        "plan_digest",
        "ceremony_nonce_digest",
        "config_digest",
        "executed_config_digest",
        "image_digest",
        "guest_runner_digest",
        "host_build_digest",
        *p.recovery_identity(),
    }
    proof = {key: state[key] for key in keys}
    assert p.recover_provision_operation(proof).ready is True
    proof["ceremony_nonce_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="proof"):
        p.recover_provision_operation(proof)


def test_release_factory_uses_only_sealed_matching_platform(tmp_path, monkeypatch):
    from core_runtime import packaged_application_bundle as packaged

    monkeypatch.setenv("RUMI_ENVIRONMENT", "production")
    monkeypatch.setenv("TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_ROOT", str(tmp_path / "untrusted"))
    monkeypatch.setenv("TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_SHA256", "sha256:" + "f" * 64)
    digest = "sha256:" + "d" * 64
    binding = packaged.PortablePackVMBundleBinding(tmp_path, digest, "linux")
    monkeypatch.setattr(packaged, "packvm_bundle_binding", lambda: binding)
    p = module.default_linux_packvm_provisioner()
    assert p._bundle_root == tmp_path
    assert p._expected_digest == digest
    binding = packaged.PortablePackVMBundleBinding(tmp_path, digest, "windows")
    assert module.default_linux_packvm_provisioner()._bundle_root is None


@pytest.fixture
def allocated(tmp_path, monkeypatch):
    from core_runtime.process_identity import ProcessIdentityEvidence
    from tests.test_macos_vz_supervisor import _artifact

    monkeypatch.setattr(module, "kvm_capability", lambda: (True, None))
    monkeypatch.setattr(
        module, "process_start_identity", lambda _pid: ProcessIdentityEvidence("live", "test-owner")
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir(mode=0o700)
    files = {}
    for slot in assets.SLOTS:
        path = bundle / slot
        path.write_bytes(f"trusted-test-{slot}".encode())
        path.chmod(0o500 if slot == "qemu" else 0o400)
        files[slot] = assets.LinuxQemuAsset(path, assets.file_digest(path), path.stat().st_size)
    fake = SimpleNamespace(
        manifest_digest="sha256:" + "1" * 64,
        image_source="https://example.invalid/pinned.raw",
        files=files,
        verify=lambda: None,
    )
    p = module.LinuxQemuProvisioner(state_dir=tmp_path / "state")
    monkeypatch.setattr(p, "_assets", lambda: fake)
    plan = p.prepare()
    p.provision(PackVMProvisioningRequest(plan.plan_digest, plan.ceremony_nonce, plan.confirmation))
    artifact = _artifact()

    def allocate(domain):
        return p.allocate(
            domain_id=domain,
            reservation_id="reservation.test",
            lease_id="lease.test",
            artifact_digest=artifact.artifact_digest,
            executable_digest=artifact.implementation_digest,
            materialization_digest=artifact.materialization_digest,
            artifact=artifact,
            channel_key=b"k" * 32,
        )

    return p, allocate, artifact


def test_allocations_use_fresh_keys_private_uefi_disks_and_real_seed_bytes(allocated):
    from pathlib import Path

    p, allocate, _artifact = allocated
    first, second = allocate("domain.first"), allocate("domain.second")
    assert first.guest_public_key != second.guest_public_key
    assert Path(first.cow_disk_path).stat().st_ino != Path(second.cow_disk_path).stat().st_ino
    assert Path(first.config_seed_path).stat().st_mode & 0o077 == 0
    assert first.config_seed_digest != second.config_seed_digest
    assert p._allocation_claim(Path(first.run_root))["domain_id"] == "domain.first"
    p.release(first)
    p.release(second)
    assert not Path(first.run_root).exists()


def test_dead_owner_recovery_is_exact_and_never_adopts_live_owner(allocated, monkeypatch):
    from pathlib import Path

    from core_runtime.process_identity import ProcessIdentityEvidence

    p, allocate, artifact = allocated
    allocation = allocate("domain.crashed")
    p._transports.clear()  # Simulate loss of in-memory ownership after a restart.
    args = {
        "domain_id": "domain.crashed",
        "reservation_id": "reservation.test",
        "executable_digest": artifact.implementation_digest,
    }
    assert p.recover_interrupted_allocation(**args) is False
    assert Path(allocation.run_root).exists()
    monkeypatch.setattr(
        module, "process_start_identity", lambda _pid: ProcessIdentityEvidence("unknown")
    )
    assert p.recover_interrupted_allocation(**args) is False
    monkeypatch.setattr(
        module, "process_start_identity", lambda _pid: ProcessIdentityEvidence("dead")
    )
    assert p.recover_interrupted_allocation(**args) is True
    assert not Path(allocation.run_root).exists()
