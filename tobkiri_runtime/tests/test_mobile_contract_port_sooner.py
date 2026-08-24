from __future__ import annotations

import base64
import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _x25519_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import x25519

    private = x25519.X25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, "x25519:" + _b64url(public)


def _decrypt_delivery_envelope(private_key, envelope: dict, *, pairing_id: str, device_id: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    epk = str(envelope["ephemeral_public_key"])
    if epk.startswith("x25519:"):
        epk = epk[len("x25519:") :]
    remote = x25519.X25519PublicKey.from_public_bytes(_unb64url(epk))
    shared = private_key.exchange(remote)
    delivery_id = str(envelope["delivery_id"])
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"rumi-mobile-token-delivery-v1",
        info=f"{pairing_id}:{device_id}:{delivery_id}".encode("utf-8"),
    ).derive(shared)
    encrypted = _unb64url(envelope["ciphertext"]) + _unb64url(envelope["tag"])
    clear = AESGCM(key).decrypt(
        _unb64url(envelope["nonce"]),
        encrypted,
        _unb64url(envelope["aad"]),
    )
    return json.loads(clear.decode("utf-8"))


def test_mobile_contract_requires_captured_scoped_operation():
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/mobile/v1/bootstrap",
        "tobkiri.mobile.v1",
        "defaultspack.mobile.bootstrap",
    )


def test_mobile_manifest_route_handler_smoke(monkeypatch):
    from ecosystem.defaultspack.domain.mobile.contract import mobile_route_manifest

    monkeypatch.delenv("RUMI_MOBILE_CREDENTIAL_TRANSFER", raising=False)
    route = next(
        item
        for item in mobile_route_manifest()
        if item["method"] == "GET" and item["pattern"] == "/api/mobile/v1/manifest"
    )

    module = importlib.import_module(route["block_module"])
    result = module.run({}, None)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["kind"] == "tobkiri_mobile_manifest_v1"
    assert data["capabilities"]["credential_transfer"] is False
    assert "credentials.request" not in data["token_roles"]["mobile_client"]["scopes"]
    assert any(
        item["method"] == "GET" and item["path"] == "/api/mobile/v1/manifest"
        for item in data["routes"]
    )


def test_mobile_pairing_approve_delivers_tokens_only_inside_encrypted_pickup(tmp_path):
    from types import SimpleNamespace

    from blocks.p2p.pairing_start import run as pairing_start_run
    from blocks.mobile.pairing import run
    from core_runtime.resolved_profile_scope import (
        activate_resolved_profile,
        restore_resolved_profile,
    )
    from domain.p2p.device_store import DeviceStore

    store_path = str(tmp_path)
    started = pairing_start_run(
        {
            "store_path": store_path,
            "capabilities": ["chat.read", "chat.write"],
        },
        None,
    )
    assert started["status"] == "ok"
    start_pairing = started["data"]["pairing"]
    pairing_id = start_pairing["pairing_id"]
    pairing_code = start_pairing["code"]
    pickup_secret = start_pairing["token_pickup_secret"]
    assert start_pairing["pairing_code"] == pairing_code
    assert start_pairing["pickup_secret"] == pickup_secret
    assert pairing_code
    assert pickup_secret

    private_key, public_key = _x25519_keypair()

    claim = run(
        {
            "action": "claim",
            "store_path": store_path,
            "pairing_id": pairing_id,
            "code": pairing_code,
            "device_id": "mobile-1",
            "device_label": "Phone",
            "public_key": "pk-mobile",
            "encryption_public_key": public_key,
            "requested_capabilities": ["chat.read", "chat.write"],
        },
        None,
    )
    assert claim["status"] == "ok"
    assert set(claim["data"]["pairing"]) == {"pairing_id", "status", "expires_at"}
    assert "code" not in json.dumps(claim["data"], sort_keys=True)
    assert "pickup_secret" not in json.dumps(claim["data"], sort_keys=True)

    review = run(
        {
            "action": "review",
            "store_path": store_path,
            "pairing_id": pairing_id,
        },
        None,
    )
    assert review["status"] == "ok"

    token = activate_resolved_profile(SimpleNamespace(profile_id="defaults"))
    try:
        approved = run(
            {
                "action": "approve",
                "store_path": store_path,
                "pairing_id": pairing_id,
                "claim_hash": review["data"]["claim_hash"],
                "scopes": review["data"]["claim"]["requested_scopes"],
            },
            {"profile_id": "defaults"},
        )
    finally:
        restore_resolved_profile(token)
    assert approved["status"] == "ok"
    public_approval = json.dumps(approved["data"], sort_keys=True)
    assert "dtk_" not in public_approval
    assert "device_token" not in public_approval

    status = run(
        {
            "action": "status",
            "store_path": store_path,
            "pairing_id": pairing_id,
        },
        None,
    )
    assert status["status"] == "ok"
    assert "token_delivery_envelope" not in status["data"]
    public_status = json.dumps(status["data"], sort_keys=True)
    assert "code" not in public_status
    assert "pickup_secret" not in public_status
    assert pickup_secret not in public_status

    pickup = run(
        {
            "action": "pickup_token_delivery",
            "store_path": store_path,
            "pairing_id": pairing_id,
            "pickup_secret": pickup_secret,
            "device_id": "mobile-1",
        },
        None,
    )
    assert pickup["status"] == "ok"
    assert "dtk_" not in json.dumps(pickup["data"], sort_keys=True)

    delivery = _decrypt_delivery_envelope(
        private_key,
        pickup["data"]["token_delivery_envelope"],
        pairing_id=pairing_id,
        device_id="mobile-1",
    )
    assert delivery["device_token"].startswith("dtk_")
    assert delivery["approval_token"] == ""
    assert delivery["scopes"] == ["chat.read", "chat.write"]
    assert DeviceStore(tmp_path).verify_token(delivery["device_token"]) is not None


def test_mobile_pairing_approve_validates_profile_before_state_transition(tmp_path):
    from blocks.mobile.pairing import run
    from core_runtime.resolved_profile_scope import (
        activate_resolved_profile,
        restore_resolved_profile,
    )
    from domain.p2p.pairing import PairingManager

    manager = PairingManager(tmp_path)
    pairing = manager.start_pairing(capabilities=["chat.read"])
    claimed = manager.claim_pairing(
        pairing.pairing_id,
        code=pairing.code,
        device_id="mobile-profile-guard",
        device_encryption_public_key="not-used-before-profile-validation",
        requested_capabilities=["chat.read"],
    )
    assert claimed["ok"] is True
    review = manager.get_pairing(pairing.pairing_id)
    assert review is not None

    token = activate_resolved_profile(None)
    try:
        rejected = run(
            {
                "action": "approve",
                "store_path": str(tmp_path),
                "pairing_id": pairing.pairing_id,
                "claim_hash": review.claim_hash(),
                "scopes": ["chat.read"],
            },
            None,
        )
    finally:
        restore_resolved_profile(token)

    assert rejected["status"] == "error"
    assert rejected["error"]["code"] == "PROFILE_CONTEXT_INVALID"
    authoritative = PairingManager(tmp_path).get_pairing(pairing.pairing_id)
    assert authoritative is not None
    assert authoritative.status == "claimed"


def test_mobile_pairing_status_durably_normalizes_expired_claim(
    tmp_path,
    monkeypatch,
):
    from blocks.mobile.pairing import run
    from domain.p2p import pairing as pairing_domain

    current_time = 1_000_000
    monkeypatch.setattr(pairing_domain, "_now_ms", lambda: current_time)
    manager = pairing_domain.PairingManager(tmp_path)
    pairing = manager.start_pairing(ttl_seconds=1)
    claimed = manager.claim_pairing(
        pairing.pairing_id,
        code=pairing.code,
        device_id="mobile-expiry",
        device_encryption_public_key="not-used-by-status",
        now_ms=current_time,
    )
    assert claimed["ok"] is True

    current_time = pairing.expires_at + 1
    status = run(
        {
            "action": "status",
            "store_path": str(tmp_path),
            "pairing_id": pairing.pairing_id,
        },
        None,
    )

    assert status["status"] == "ok"
    assert status["data"]["status"] == "expired"
    persisted = pairing_domain.PairingManager(tmp_path).get_pairing(pairing.pairing_id)
    assert persisted is not None
    assert persisted.status == "expired"


def test_device_token_auth_is_limited_by_mobile_route_scope(tmp_path, monkeypatch):
    from core_runtime.api.auth_gate import AuthGateMixin
    from tests.v4_batch_support import assert_route_cutover

    del tmp_path, monkeypatch
    assert not hasattr(AuthGateMixin, "_check_bearer_auth")
    assert hasattr(AuthGateMixin, "_check_panel_session")
    assert_route_cutover(
        "GET",
        "/api/mobile/v1/conversations",
        "conversation.turn.v1",
        "complete",
    )
    assert_route_cutover(
        "POST",
        "/api/mobile/v1/conversations",
        "conversation.turn.v1",
        "complete",
    )
