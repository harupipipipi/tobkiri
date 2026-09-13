"""Exercise registration updates with real private files and authenticated state."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
from typing import Any

import pytest

from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4, PackVMProvisioningRequest
from ecosystem.defaultspack.backend.sandbox.isolation import macos_vz_provisioner as vz
from ecosystem.defaultspack.backend.sandbox.isolation import macos_vz_registration as history
from tests.test_macos_vz_provisioner import (
    _digest,
    _private_file,
    attested_provisioner,  # noqa: F401
    provisioner_fixture,  # noqa: F401
)


@pytest.fixture(params=["registration", "storage_rebind"])
def registration_case(attested_provisioner: Any, request: Any) -> Any:  # noqa: F811
    manager, manifest, root = attested_provisioner
    state = manager._load_state()
    state["protocol_ready"] = True
    state["vz_provisioner_digest"] = _digest(b"previous source")
    if request.param == "storage_rebind":
        state["vz_state_root_device"] -= 1
        state["instance_root_device"] -= 1
    manager._write_attested_state(state)
    helper = _private_file(manifest.helper_path.with_name("updated-helper"), b"new helper", 0o700)
    runner = _private_file(manifest.agent_path.with_name("updated-runner"), b"new runner", 0o444)
    current = replace(
        manifest,
        helper_path=helper,
        helper_digest=vz._file_digest(helper),
        agent_path=runner,
        agent_digest=vz._file_digest(runner),
        manifest_digest=_digest(b"new manifest"),
    )
    manager._require_manifest = lambda: current
    return manager, current, root


def _request(manager: Any) -> tuple[Any, PackVMProvisioningRequest]:
    plan = manager.prepare()
    assert plan.launcher_reason is None
    assert plan.registration_update is not None
    return plan, PackVMProvisioningRequest(
        plan_digest=plan.plan_digest,
        ceremony_nonce=plan.ceremony_nonce,
        confirmation=plan.confirmation,
        previous_attestation_digest=plan.registration_update["previous_attestation_digest"],
        storage_rebind_digest=plan.storage_rebind["digest"] if plan.storage_rebind else None,
        session_digest=_digest(b"session"),
    )


def _proof(manager: Any, plan: Any, request: PackVMProvisioningRequest) -> dict[str, Any]:
    return {
        "backend_id": plan.backend_id,
        "instance": plan.instance,
        "session_digest": request.session_digest,
        "plan_digest": plan.plan_digest,
        "ceremony_nonce_digest": _digest(request.ceremony_nonce.encode()),
        "config_digest": plan.config_digest,
        "image_digest": plan.image_digest,
        "guest_runner_digest": plan.guest_runner_digest,
        "host_build_digest": plan.host_build_digest,
        "previous_attestation_digest": request.previous_attestation_digest,
        "storage_rebind_digest": request.storage_rebind_digest,
        **manager.recovery_identity(),
    }


def test_registration_updates_only_attested_metadata_and_recovers_without_replay(
    registration_case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, manifest, root = registration_case
    domain = manager.state_path.parent / "domains" / "existing"
    domain.mkdir(parents=True, mode=0o700)
    preserved = [root / "base-image.json", Path(manager._load_state()["base_image_path"])]
    for name in ("boot-cow.raw", "efi.bin", "seed", "user-data", "reservations.json"):
        preserved.append(_private_file(domain / name, name.encode()))
    before = {path: (path.read_bytes(), path.stat().st_ino) for path in preserved}
    old = manager._load_state()
    old_bytes = manager.state_path.read_bytes()
    assert not manager.doctor().ready
    plan, request = _request(manager)
    assert not plan.image_download_required
    assert plan.image_download_bytes == 0

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("registration update must not download, clone, or remove VM files")

    monkeypatch.setattr(manager.image_cache, "provisioning_image", forbidden)
    monkeypatch.setattr(manager, "_create_instance", forbidden)
    monkeypatch.setattr(manager, "_clone_file", forbidden)
    monkeypatch.setattr(manager, "_remove_exact_instance", forbidden)
    assert manager.provision(request).ready
    updated = manager._load_state()
    assert updated["helper_digest"] == manifest.helper_digest
    assert updated["previous_attestation_digest"] == old["attestation_digest"]
    assert updated["instance_root_inode"] == old["instance_root_inode"]
    assert updated["instance_root_device"] == root.stat().st_dev
    assert updated["vz_state_root_device"] == manager.state_path.parent.stat().st_dev
    assert updated["storage_rebind_digest"] == request.storage_rebind_digest
    retained = manager.state_path.parent / "registration-history" / (
        old["attestation_digest"][7:] + ".json"
    )
    assert retained.read_bytes() == old_bytes
    assert before == {path: (path.read_bytes(), path.stat().st_ino) for path in preserved}
    new_bytes = manager.state_path.read_bytes()
    proof = _proof(manager, plan, request)
    assert manager.recover_provision_operation(proof).ready
    assert manager.state_path.read_bytes() == new_bytes
    with pytest.raises(ValueError, match="already consumed"):
        manager.provision(request)
    with pytest.raises(ValueError, match="recovery proof changed"):
        manager.recover_provision_operation({**proof, "previous_attestation_digest": _digest(b"other")})
    with pytest.raises(ValueError, match="recovery proof changed"):
        manager.recover_provision_operation({**proof, "storage_rebind_digest": _digest(b"other")})


@pytest.mark.parametrize("ack", [None, _digest(b"other"), "correct"])
def test_update_consent_requires_previous_registration_before_host_operation(
    registration_case: Any, ack: str | None,
) -> None:
    manager, _manifest, _root = registration_case
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare(session_id="panel")
    payload = {key: plan[key] for key in ("plan_digest", "ceremony_nonce", "confirmation")}
    payload["approve_image_download"] = False
    if plan["storage_rebind"]:
        payload["storage_rebind_digest"] = plan["storage_rebind"]["digest"]
    if ack is not None:
        payload["previous_attestation_digest"] = (
            plan["registration_update"]["previous_attestation_digest"] if ack == "correct" else ack
        )
    before = manager.state_path.read_bytes()
    if ack != "correct":
        with pytest.raises(ValueError, match="exact explicit consent|typed contract"):
            lifecycle.consent(payload, session_id="panel")
        assert manager.state_path.read_bytes() == before
        return
    consent = lifecycle.consent(payload, session_id="panel")
    assert consent["previous_attestation_digest"] == payload["previous_attestation_digest"]
    operation_id = "11111111-1111-4111-8111-111111111111"
    body = {"operation_id": operation_id, "consent_id": consent["consent_id"]}
    lifecycle.provision(body, session_id="panel")
    for _ in range(200):
        operation = lifecycle.progress(operation_id, session_id="panel")
        if operation["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert operation["state"] == "succeeded", operation
    updated = manager.state_path.read_bytes()
    assert lifecycle.provision(body, session_id="panel")["state"] == "succeeded"
    assert manager.state_path.read_bytes() == updated
    with pytest.raises(ValueError, match="another consent"):
        lifecycle.provision(body, session_id="another-panel")


@pytest.mark.parametrize("fault", [
    "state_tamper", "stopped", "incomplete", "root_replaced", "image_changed",
    "image_source_changed", "base_changed", "runner_changed",
])
def test_update_preparation_rejects_unverified_existing_registration(
    registration_case: Any, fault: str,
) -> None:
    manager, manifest, root = registration_case
    state = manager._load_state()
    if fault == "state_tamper":
        state["helper_digest"] = _digest(b"tampered")
        vz._atomic_private_json(manager.state_path, state)
    elif fault in {"stopped", "incomplete"}:
        state["stopped" if fault == "stopped" else "protocol_ready"] = fault == "stopped"
        manager._write_attested_state(state)
    elif fault == "root_replaced":
        root.rename(root.with_name("retained"))
        root.mkdir(mode=0o700)
    elif fault in {"image_changed", "image_source_changed"}:
        field = "image_digest" if fault == "image_changed" else "image_source"
        current = replace(manifest, **{field: _digest(b"other") if field == "image_digest" else "https://example.invalid/other.raw"})
        manager._require_manifest = lambda: current
    elif fault == "base_changed":
        Path(state["base_image_path"]).write_bytes(b"changed")
    else:
        manifest.agent_path.chmod(0o600)
        _private_file(manifest.agent_path, b"changed", 0o444)
    before = manager.state_path.read_bytes()
    plan = manager.prepare()
    assert plan.launcher_reason
    assert plan.runtime_path_status == "unsafe"
    assert manager.state_path.read_bytes() == before


@pytest.mark.parametrize("fault", ["stale", "manifest", "missing_ack", "history_full", "write_failed", "cancelled"])
def test_update_failure_retains_existing_registration(
    registration_case: Any, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    manager, manifest, _root = registration_case
    plan, request = _request(manager)
    if fault == "stale":
        state = manager._load_state()
        state["created_unix"] = 123
        manager._write_attested_state(state)
    elif fault == "manifest":
        manager._require_manifest = lambda: replace(manifest, manifest_digest=_digest(b"other"))
    elif fault == "missing_ack":
        request = replace(request, previous_attestation_digest=None)
    elif fault == "history_full":
        monkeypatch.setattr(history, "MAX_REGISTRATIONS", 0)
    elif fault == "write_failed":
        def fail_write(*args: Any, **kwargs: Any) -> None:
            raise OSError("simulated state publication failure")
        monkeypatch.setattr(manager, "_write_attested_state", fail_write)
    before = manager.state_path.read_bytes()
    with pytest.raises((OSError, ValueError, RuntimeError)):
        manager.provision(request, cancelled=lambda: fault == "cancelled")
    assert manager.state_path.read_bytes() == before
    assert not manager.mutation_claim_path.exists()
    assert not manager.recovery_path.exists()


@pytest.mark.parametrize("ack", [None, _digest(b"other"), True])
def test_storage_rebind_requires_separate_exact_consent_in_host_and_provisioner(
    registration_case: Any, ack: Any,
) -> None:
    manager, _manifest, _root = registration_case
    # Exercise the storage path for either fixture, without changing real mounts.
    state = manager._load_state()
    state["vz_state_root_device"] = manager.state_path.parent.stat().st_dev - 1
    state["instance_root_device"] = state["vz_state_root_device"]
    manager._write_attested_state(state)
    before = manager.state_path.read_bytes()
    lifecycle = PackVMLifecycleV4(manager)
    plan = lifecycle.prepare(session_id="panel")
    assert plan["storage_rebind"]
    assert manager.state_path.read_bytes() == before
    assert not manager.doctor().ready
    with pytest.raises(ValueError, match="changed"):
        manager._verify_registration_roots(manager._load_state())
    payload = {key: plan[key] for key in ("plan_digest", "ceremony_nonce", "confirmation")}
    payload.update({
        "approve_image_download": False,
        "previous_attestation_digest": plan["registration_update"]["previous_attestation_digest"],
    })
    if ack is not None:
        payload["storage_rebind_digest"] = ack
    with pytest.raises(ValueError, match="exact explicit consent"):
        lifecycle.consent(payload, session_id="panel")
    _plan, request = _request(manager)
    with pytest.raises(ValueError, match="exact explicit consent"):
        manager.provision(replace(request, storage_rebind_digest=ack))
    assert manager.state_path.read_bytes() == before


@pytest.mark.parametrize("fault", ["inode", "path", "private_mode", "owner", "mixed_device", "boolean_device"])
def test_storage_proposal_does_not_approve_changed_ownership_or_directories(
    registration_case: Any, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    manager, _manifest, root = registration_case
    state = manager._load_state()
    if fault == "inode":
        state["vz_state_root_inode"] += 1
    elif fault == "path":
        state["instance_root"] = str(root.parent / "different")
    elif fault == "private_mode":
        root.chmod(0o750)
    elif fault == "owner":
        monkeypatch.setattr(history.os, "getuid", lambda: root.stat().st_uid + 1)
    elif fault == "mixed_device":
        state["instance_root_device"] += 1
    else:
        state["vz_state_root_device"] = True
    before = manager.state_path.read_bytes()
    with pytest.raises(ValueError, match="storage re-registration"):
        history.prepare_storage_rebind(manager.state_path.parent, state)
    assert manager.state_path.read_bytes() == before


def test_storage_update_revalidates_files_after_history_retention(
    registration_case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _manifest, _root = registration_case
    state = manager._load_state()
    state["vz_state_root_device"] = manager.state_path.parent.stat().st_dev - 1
    state["instance_root_device"] = state["vz_state_root_device"]
    manager._write_attested_state(state)
    _plan, request = _request(manager)
    before = manager.state_path.read_bytes()
    original = vz.retain_registration

    def replace_image_after_retention(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        Path(state["base_image_path"]).write_bytes(b"changed after initial verification")

    monkeypatch.setattr(vz, "retain_registration", replace_image_after_retention)
    with pytest.raises(ValueError, match="immutable base image changed"):
        manager.provision(request)
    assert manager.state_path.read_bytes() == before


@pytest.mark.parametrize("published", [False, True])
def test_crash_claim_recovery_does_not_repeat_registration_write(
    registration_case: Any, monkeypatch: pytest.MonkeyPatch, published: bool,
) -> None:
    manager, _manifest, _root = registration_case
    plan, request = _request(manager)
    proof = _proof(manager, plan, request)
    if published:
        manager.provision(request)
    before = manager.state_path.read_bytes()
    claim = {
        "version": 1, "operation": "provision", "instance": vz.VZ_INSTANCE,
        "owner_pid": 123456789,
        "binding": {key: proof[key] for key in ("session_digest", "plan_digest", "ceremony_nonce_digest")},
    }
    vz._atomic_private_json(manager.mutation_claim_path, claim)
    monkeypatch.setattr(vz, "_process_is_alive", lambda pid: False)
    if published:
        assert manager.recover_provision_operation(proof).ready
    else:
        with pytest.raises(ValueError, match="recovery proof changed"):
            manager.recover_provision_operation(proof)
    assert manager.state_path.read_bytes() == before
    assert not manager.mutation_claim_path.exists()
