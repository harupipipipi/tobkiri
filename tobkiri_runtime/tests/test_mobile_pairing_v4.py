"""Crash-recovery regression coverage for captured mobile pairing approval."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from core_runtime.mobile_pairing_v4 import MobilePairingServiceV4
from ecosystem.defaultspack.domain.p2p.device_store import DeviceStore
from ecosystem.defaultspack.domain.p2p.pairing import PairingManager


class _SimulatedCrash(BaseException):
    """Model abrupt process termination outside normal rollback handling."""


def _encryption_public_key() -> str:
    public = x25519.X25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "x25519:" + base64.urlsafe_b64encode(public).decode("ascii").rstrip("=")


def _claimed_pairing(store_path: Path) -> tuple[str, str]:
    manager = PairingManager(store_path)
    pairing = manager.start_pairing(capabilities=["chat.read"])
    claimed = manager.claim_pairing(
        pairing.pairing_id,
        code=pairing.code,
        device_id="mobile-crash-recovery",
        device_label="Recovery phone",
        device_encryption_public_key=_encryption_public_key(),
        requested_capabilities=["chat.read"],
    )
    assert claimed["ok"] is True
    review = manager.get_pairing(pairing.pairing_id)
    assert review is not None
    return pairing.pairing_id, review.claim_hash()


@pytest.mark.parametrize(
    ("checkpoint", "recovery_status"),
    [
        ("after_pairing_prepare", "claimed"),
        ("after_device_issue", "claimed"),
        ("after_delivery_stage", "approved"),
        ("after_pairing_commit", "approved"),
    ],
)
def test_mobile_pairing_recovery_never_activates_or_duplicates_a_partial_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
    recovery_status: str,
) -> None:
    """Recover each durable boundary without activating an orphan token."""

    store_path = tmp_path / "p2p"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_P2P_STORE_PATH", str(store_path))
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    pairing_id, claim_hash = _claimed_pairing(store_path)

    def crash_at(name: str) -> None:
        if name == checkpoint:
            raise _SimulatedCrash(name)

    crashing = MobilePairingServiceV4(
        profile_id="defaults",
        profile_revision="profile-revision",
        plan_digest="plan-digest",
        fault_injector=crash_at,
    )
    payload = {
        "pairing_id": pairing_id,
        "claim_hash": claim_hash,
        "scopes": ["chat.read"],
    }
    with pytest.raises(_SimulatedCrash):
        crashing.invoke("pairing.approve", payload)

    staged_before_recovery = DeviceStore(store_path).get_device(
        "mobile-crash-recovery"
    )
    if checkpoint != "after_pairing_prepare":
        assert staged_before_recovery is not None
        assert staged_before_recovery.active is False
    token_hash_before_recovery = (
        staged_before_recovery.token_hash if staged_before_recovery is not None else ""
    )

    recovered = MobilePairingServiceV4(
        profile_id="defaults",
        profile_revision="profile-revision",
        plan_digest="plan-digest",
    )
    status = recovered.invoke("pairing.status.read", {"pairing_id": pairing_id})
    assert status["status"] == recovery_status
    if recovery_status == "claimed":
        approved = recovered.invoke("pairing.approve", payload)
        assert approved["pairing"]["status"] == "approved"

    manager = PairingManager(store_path)
    final = manager.get_pairing(pairing_id)
    assert final is not None
    assert final.status == "approved"
    assert final.token_delivery_envelope
    assert final.approval_transaction == {}
    devices = DeviceStore(store_path)
    final_device = devices.get_device("mobile-crash-recovery")
    assert final_device is not None and final_device.active
    assert len(devices.list_devices()) == 1
    if checkpoint in {"after_delivery_stage", "after_pairing_commit"}:
        assert final_device.token_hash == token_hash_before_recovery
