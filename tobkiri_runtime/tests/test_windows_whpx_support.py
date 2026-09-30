"""WHPX conformance tests, without claiming Windows VM execution."""

import ctypes
import hashlib
import json
import os
import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.packvm_lifecycle_v4 import PackVMProvisioningRequest
from ecosystem.defaultspack.backend.sandbox.isolation import windows_whpx_assets as assets
from ecosystem.defaultspack.backend.sandbox.isolation import windows_whpx_provisioner as provisioner
from ecosystem.defaultspack.backend.sandbox.isolation import windows_whpx_seeds as seeds
from tobkiri_host import windows_whpx_probe as probe
from tobkiri_host import windows_whpx_process as process
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.windows_whpx_models import (
    WindowsWHPXDomainAllocation,
    WindowsWHPXLaunchAssets,
    host_path,
)
from tobkiri_host.windows_whpx_security import file_digest


def digest(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def pe(imports=()):
    result = bytearray(4096)
    result[:2] = b"MZ"
    struct.pack_into("<I", result, 60, 128)
    result[128:132] = b"PE\0\0"
    struct.pack_into("<HH", result, 132, 0x8664, 1)
    struct.pack_into("<H", result, 148, 240)
    struct.pack_into("<H", result, 152, 0x20B)
    struct.pack_into("<I", result, 260, 16)
    struct.pack_into("<IIII", result, 400, 3584, 0x1000, 3584, 512)
    if imports:
        struct.pack_into("<II", result, 272, 0x1000, (len(imports) + 1) * 20)
        cursor = 1024
        for index, name in enumerate(imports):
            encoded = name.encode() + b"\0"
            struct.pack_into("<I", result, 512 + index * 20 + 12, 0x1000 + cursor - 512)
            result[cursor : cursor + len(encoded)] = encoded
            cursor += len(encoded)
    return bytes(result)


def bundle(tmp_path, imports=(), dependencies=()):
    root = tmp_path / "bundle"
    (root / "bin").mkdir(parents=True)
    records = {}
    for slot in assets.SLOTS:
        relative = "bin/qemu-system-x86_64.exe" if slot == "qemu" else f"data/{slot}"
        target = root / relative
        target.parent.mkdir(exist_ok=True)
        content = pe(imports) if slot == "qemu" else ("trusted-" + slot).encode()
        target.write_bytes(content)
        records[slot] = {"path": relative, "sha256": digest(content), "size_bytes": len(content)}
    deps = []
    for name, child_imports in dependencies:
        content = pe(child_imports)
        (root / "bin" / name).write_bytes(content)
        deps.append({"path": f"bin/{name}", "sha256": digest(content), "size_bytes": len(content)})
    raw = json.dumps(
        {
            "schema": assets.SCHEMA,
            "architecture": "amd64",
            "accelerator": "whpx",
            "files": records,
            "image_source": "https://example.invalid/pinned.raw",
            "qemu_dependencies": deps,
        }
    ).encode()
    (root / assets.MANIFEST_NAME).write_bytes(raw)
    return root, digest(raw)


def test_probe_checks_documented_32_bit_bool():
    calls = []

    def query(code, output, capacity, written):
        calls.append((code, capacity))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int32))[0] = 1
        ctypes.cast(written, ctypes.POINTER(ctypes.c_uint32))[0] = 4
        return 0

    assert probe._hypervisor_present(SimpleNamespace(WHvGetCapability=query)) is True
    assert calls == [(0, 8)]


@pytest.mark.parametrize("status,written", [(-2147024809, 4), (0, 0), (0, 16)])
def test_probe_rejects_error_and_wrong_result_size(status, written):
    def query(code, output, capacity, actual):
        ctypes.cast(actual, ctypes.POINTER(ctypes.c_uint32))[0] = written
        return status

    with pytest.raises(OSError):
        probe._hypervisor_present(SimpleNamespace(WHvGetCapability=query))


def test_manifest_requires_external_pin_and_whpx(tmp_path):
    root, expected = bundle(tmp_path)
    assert assets.load_windows_whpx_assets(root, expected).files["qemu"].path.suffix == ".exe"
    with pytest.raises(ValueError, match="trusted identity"):
        assets.load_windows_whpx_assets(root, "sha256:" + "0" * 64)
    value = json.loads((root / assets.MANIFEST_NAME).read_bytes())
    value["accelerator"] = "tcg"
    raw = json.dumps(value).encode()
    (root / assets.MANIFEST_NAME).write_bytes(raw)
    with pytest.raises(ValueError, match="isolation profile"):
        assets.load_windows_whpx_assets(root, digest(raw))


def test_transitive_unbundled_dll_rejected(tmp_path):
    root, expected = bundle(
        tmp_path, ("libglib-2.0-0.dll",), (("libglib-2.0-0.dll", ("libintl-8.dll",)),)
    )
    with pytest.raises(ValueError, match="libintl-8.dll"):
        assets.load_windows_whpx_assets(root, expected)


def test_closed_dll_inventory_rejects_injection(tmp_path):
    root, expected = bundle(
        tmp_path,
        ("KERNEL32.dll", "libglib-2.0-0.dll"),
        (("libglib-2.0-0.dll", ("api-ms-win-core-file-l1-1-0.dll",)),),
    )
    verified = assets.load_windows_whpx_assets(root, expected)
    (root / "bin" / "injected.dll").write_bytes(pe())
    with pytest.raises(ValueError, match="unpinned"):
        verified.verify()


def test_symlink_and_hardlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"bytes")
    link = tmp_path / "symlink"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        file_digest(link)
    os.link(target, tmp_path / "hardlink")
    with pytest.raises(ValueError):
        file_digest(target)


@pytest.fixture
def registration(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "whpx_capability", lambda: (True, None))
    p = provisioner.WindowsWHPXProvisioner(state_dir=tmp_path / "state")
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
    return p, PackVMProvisioningRequest(plan.plan_digest, plan.ceremony_nonce, plan.confirmation)


def test_consent_single_use_and_authenticated_state(registration):
    p, request = registration
    assert p.provision(request).ready is True
    with pytest.raises(ValueError, match="consumed"):
        p.provision(request)
    p.state_path.write_bytes(
        p.state_path.read_bytes().replace(b'"stopped":false', b'"stopped":true')
    )
    assert "authentication" in p.doctor().reason


def test_changed_consent_rejected(registration):
    p, request = registration
    with pytest.raises(ValueError, match="consent"):
        p.provision(replace(request, plan_digest="sha256:" + "3" * 64))
    assert not p.state_path.exists()


def test_unavailable_whpx_never_promotes(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioner, "whpx_capability", lambda: (False, "WHPX unavailable"))
    p = provisioner.WindowsWHPXProvisioner(state_dir=tmp_path / "state")
    assert p.doctor().ready is False
    assert p.prepare().launcher_reason == "WHPX unavailable"
    assert p.production_backend_registration() is None


def test_orphan_cleanup_does_not_erase(registration):
    p, request = registration
    p.provision(request)
    orphan = p.state_path.parent / "domains" / "unknown"
    orphan.mkdir(parents=True, mode=0o700)
    with pytest.raises(ValueError, match="recovery"):
        p.cleanup(f"DELETE {provisioner.INSTANCE}")
    assert orphan.exists()


def config(tmp_path):
    d = "sha256:" + "1" * 64
    return process.WindowsWHPXLaunchConfig(
        qemu_path=tmp_path / "qemu.exe",
        qemu_digest=d,
        run_root=tmp_path / "domain",
        private_disk_path=tmp_path / "domain/boot.raw",
        agent_image_path=tmp_path / "domain/agent.iso",
        seed_image_path=tmp_path / "domain/config.iso",
        firmware_code_path=tmp_path / "firmware.fd",
        private_firmware_vars_path=tmp_path / "domain/vars.fd",
        firmware_code_digest=d,
        private_disk_digest=d,
        agent_image_digest=d,
        seed_image_digest=d,
        private_firmware_vars_digest=d,
    )


def test_command_whpx_only_private_pipes_no_network(tmp_path):
    command = process.WindowsWHPXProcess(config(tmp_path)).command()
    assert command[command.index("-accel") + 1] == "whpx"
    assert command[command.index("-nic") + 1] == "none"
    assert command[command.index("-chardev") + 1] == "stdio,id=agent,signal=off,mux=off"
    assert not any(
        value in " ".join(command) for value in ("tcg", "tcp:", "/proc/", "-sandbox", "ssd=off")
    )


@pytest.mark.parametrize(
    "field,value", [("vcpus", True), ("memory_mib", 0), ("max_response_bytes", 1 << 30)]
)
def test_bounded_configuration(tmp_path, field, value):
    with pytest.raises(BackendUnavailableError, match="invalid"):
        replace(config(tmp_path), **{field: value})


def test_portable_iso_has_real_cidata_volume(tmp_path):
    root = tmp_path / "domain"
    root.mkdir(mode=0o700)
    target = root / "config.iso"
    seeds._write_iso_seed(
        target, "CIDATA", {"user-data": b"#cloud-config\n", "meta-data": b"instance-id: one\n"}
    )
    content = target.read_bytes()
    assert content[16 * 2048 + 1 : 16 * 2048 + 6] == b"CD001"
    assert b"CIDATA" in content and b"#cloud-config\n" in content
    assert len(content) % 2048 == 0


def test_development_factory_environment_names(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_ENVIRONMENT", "development")
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    monkeypatch.setenv("TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_ROOT", str(tmp_path / "bundle"))
    monkeypatch.setenv("TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_SHA256", "sha256:" + "7" * 64)
    result = provisioner.default_windows_packvm_provisioner()
    assert result._bundle_root == tmp_path / "bundle"
    assert result._expected_digest == "sha256:" + "7" * 64


def test_real_dos_paths_are_accepted_in_shared_wire_model():
    d = "sha256:" + "1" * 64
    assets = WindowsWHPXLaunchAssets(d, r"C:\Users\Owner\Tobkiri\base.raw", d, d, True)
    assert assets.base_image_path.startswith("C:")
    allocation = WindowsWHPXDomainAllocation(
        "domain",
        "reservation",
        "lease",
        r"C:\Tobkiri\domain",
        r"C:\Tobkiri\domain\boot.raw",
        d,
        r"C:\Tobkiri\domain\vars.fd",
        d,
        r"C:\Tobkiri\domain\agent.iso",
        d,
        r"C:\Tobkiri\domain\config.iso",
        d,
        b"x" * 32,
    )
    assert allocation.to_dict()["run_root"] == r"C:\Tobkiri\domain"


@pytest.mark.parametrize(
    "path",
    [
        r"C:\VM\..\secret",
        r"\\server\share\vm",
        r"\\?\GLOBALROOT\Device\disk",
        r"C:\VM\file:ads",
        r"C:\VM\NUL",
        r"C:relative",
    ],
)
def test_unsafe_windows_paths_fail(path):
    with pytest.raises(BackendUnavailableError):
        host_path(path)


def test_launcher_extended_dos_spelling_is_same_local_path():
    assert host_path(r"\\?\C:\Tobkiri\VM") == host_path(r"C:\Tobkiri\VM")


def test_stopped_registration_requires_fresh_bound_resume_consent(registration):
    p, request = registration
    p.provision(request)
    p.stop(f"STOP {provisioner.INSTANCE}")
    assert p.doctor().ready is False
    plan = p.prepare()
    assert plan.registration_update is not None
    missing = PackVMProvisioningRequest(plan.plan_digest, plan.ceremony_nonce, plan.confirmation)
    with pytest.raises(ValueError, match="stale or missing"):
        p.provision(missing)
    plan = p.prepare()
    approved = PackVMProvisioningRequest(
        plan.plan_digest,
        plan.ceremony_nonce,
        plan.confirmation,
        previous_attestation_digest=plan.registration_update["previous_attestation_digest"],
    )
    assert p.provision(approved).ready is True


def test_recovery_rejects_missing_and_invented_proof_fields(registration):
    p, request = registration
    p.provision(request)
    with pytest.raises(ValueError, match="proof"):
        p.recover_provision_operation({})
    with pytest.raises(ValueError, match="proof"):
        p.recover_provision_operation({"imaginary-field": "anything"})
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
    assert p.recover_provision_operation({key: state[key] for key in keys}).ready


def test_full_private_allocation_and_release_preserves_guest_bindings(tmp_path, monkeypatch):
    from tobkiri_host import windows_whpx_supervisor
    from tobkiri_host.artifact_materialization import (
        MaterializedArtifactFile,
        MaterializedPackArtifact,
        _materialization_digest,
    )

    monkeypatch.setattr(provisioner, "whpx_capability", lambda: (True, None))
    monkeypatch.setattr(windows_whpx_supervisor, "whpx_capability", lambda: (True, None))
    root, expected = bundle(tmp_path)
    p = provisioner.WindowsWHPXProvisioner(
        state_dir=tmp_path / "state", bundle_root=root, expected_manifest_digest=expected
    )
    plan = p.prepare()
    assert p.provision(
        PackVMProvisioningRequest(plan.plan_digest, plan.ceremony_nonce, plan.confirmation)
    ).ready
    facts = p.production_backend_registration()
    backend = facts.build_backend()
    assert backend._driver.platform == "windows-amd64"
    runtime = backend._driver._runtime.to_dict()
    assert runtime["accelerator"] == "whpx"
    assert runtime["guest_transport"] == "virtio-serial"
    assert "guest_vsock_port" not in runtime
    content = b"def tobkiri_packvm_invoke(*args): return {'ok': True}\n"
    implementation = digest(content)
    artifact_digest = digest(b"test-artifact")
    files = (MaterializedArtifactFile("runtime/entry.py", implementation, False, content),)
    materialization = _materialization_digest(
        "fixture-pack",
        artifact_digest,
        "fixture-pack.entry",
        implementation,
        "runtime/entry.py",
        files,
    )
    artifact = MaterializedPackArtifact(
        "fixture-pack",
        artifact_digest,
        "fixture-pack.entry",
        implementation,
        "runtime/entry.py",
        materialization,
        1,
        1,
        files,
    )
    allocation = p.allocate(
        domain_id="domain.test",
        reservation_id="reservation.test",
        lease_id="lease.test",
        artifact_digest=artifact_digest,
        executable_digest=implementation,
        materialization_digest=materialization,
        artifact=artifact,
        channel_key=b"k" * 32,
    )
    assert allocation.guest_public_key_digest.startswith("sha256:")
    assert allocation.cow_disk_digest == facts.assets.files["image"].digest
    transport = p.transport_for_allocation(allocation)
    assert transport is not None and transport.platform == "windows-amd64"
    assert p.transport_for_allocation(allocation) is None
    assert transport._process.config.seed_image_digest == allocation.config_seed_digest
    # Allocation is prepared without executing any host/guest Pack code.
    assert transport._process.pid is None
    p.release(allocation)
    from pathlib import Path

    assert not Path(allocation.run_root).exists()
