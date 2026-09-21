from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

AUTHORITY_APPROVER_SCOPES = [
    "authority.request.approve",
    "authority.request.deny",
    "authority.request.list",
    "authority.request.read",
]


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


def _review_claim(store_path: str, pairing_id: str) -> dict:
    from blocks.mobile.pairing import run

    review = run({
        "action": "review",
        "store_path": store_path,
        "pairing_id": pairing_id,
    }, None)
    assert review["status"] == "ok"
    assert review["data"]["claim_hash"].startswith("sha256:")
    return review["data"]


def _mobile_route_server():
    from transport.http import DefaultsHttpServer
    from transport.registry import build_fallback_http_routes

    server = DefaultsHttpServer.__new__(DefaultsHttpServer)
    server._build_context = lambda: {"request_id": "test-mobile-route"}
    server._routes = build_fallback_http_routes(server)
    return server


def _invoke_mobile_route(server, method: str, path: str, body: dict) -> dict:
    handler, path_params, _source, _path_inject, pattern = server._match_route(
        method,
        path,
    )
    assert handler is not None, f"missing route: {method} {path}"
    expected_pattern = path
    for key, value in path_params.items():
        expected_pattern = expected_pattern.replace(value, f"{{{key}}}")
    assert pattern == expected_pattern
    return handler(body, path_params)


def _assert_no_plain_tokens_outside_envelope(data: dict) -> None:
    public_data = dict(data)
    public_data.pop("token_delivery_envelope", None)
    rendered = json.dumps(public_data, sort_keys=True)
    for forbidden in (
        "device_token",
        "approval_token",
        "client_access_token",
        "approver_access_token",
        "dtk_",
    ):
        assert forbidden not in rendered


def test_pairing_v2_claim_approve_flow():
    from domain.p2p.pairing import PairingManager
    from domain.p2p.device_store import DeviceStore

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing(
        ttl_seconds=300,
        capabilities=["chat.read", "chat.write", "tools.observe"],
    )
    assert session.status == "pending"

    # Claim
    claim = pm.claim_pairing(
        session.pairing_id,
        code=session.code,
        device_id="iphone-1",
        device_label="はるのiPhone",
        device_public_key="pk-abc",
        requested_capabilities=["chat.read", "chat.write", "tools.observe"],
    )
    assert claim["ok"]
    assert claim["pairing"]["status"] == "claimed"
    assert set(claim["pairing"]) == {"pairing_id", "status", "expires_at"}
    assert pm.get_pairing(session.pairing_id).claimed_device_id == "iphone-1"

    # Approve
    approve = pm.approve_pairing_v2(session.pairing_id)
    assert approve["ok"]
    assert approve["pairing"]["status"] == "approved"
    assert approve["device_id"] == "iphone-1"
    assert "chat.read" in approve["scopes"]

    # Issue device token
    ds = DeviceStore(tmp)
    device, token, approval_token = ds.issue_tokens(
        approve["device_id"],
        label=approve["device_label"],
        public_key=approve["device_public_key"],
        scopes=[*approve["scopes"], "tools.approve"],
        pairing_id=session.pairing_id,
    )
    assert token.startswith("dtk_")
    assert approval_token.startswith("dtk_")
    assert device.confirmation_code  # visual code like "🟢・58"
    assert "tools.approve" not in device.scopes
    assert device.approval_scopes == AUTHORITY_APPROVER_SCOPES

    # Verify token
    verified = ds.verify_token(token)
    assert verified is not None
    assert verified.device_id == "iphone-1"
    assert "tools.approve" not in verified.scopes

    verified_approval = ds.verify_token(approval_token)
    assert verified_approval is not None
    assert verified_approval.device_id == "iphone-1"
    assert verified_approval.scopes == AUTHORITY_APPROVER_SCOPES

    # Revoke
    revoked = ds.revoke_device("iphone-1")
    assert revoked.status == "revoked"
    assert ds.verify_token(token) is None
    assert ds.verify_token(approval_token) is None


def test_pairing_v2_reject():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing()
    pm.claim_pairing(session.pairing_id, code=session.code, device_id="d1")
    result = pm.reject_pairing(session.code)
    assert result["ok"]
    assert result["pairing"]["status"] == "rejected"


def test_pairing_v2_claim_expired():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing(ttl_seconds=1)
    # Force expiry
    result = pm.claim_pairing(
        session.pairing_id,
        code=session.code,
        device_id="d1",
        now_ms=session.expires_at + 1000,
    )
    assert not result["ok"]
    assert result["code"] == "PAIRING_EXPIRED"


def test_device_store_list_and_patch():
    from domain.p2p.device_store import DeviceStore

    tmp = tempfile.mkdtemp()
    ds = DeviceStore(tmp)
    ds.issue_token("d1", label="iPhone")
    ds.issue_token("d2", label="iPad")

    devices = ds.list_devices()
    assert len(devices) == 2
    ids = {d["device_id"] for d in devices}
    assert ids == {"d1", "d2"}

    updated = ds.update_label("d1", "はるのiPhone")
    assert updated.label == "はるのiPhone"


def test_device_token_is_scoped():
    from domain.p2p.device_store import DeviceStore, DEFAULT_SCOPES

    tmp = tempfile.mkdtemp()
    ds = DeviceStore(tmp)
    default_device, default_token, _default_approval_token = ds.issue_tokens("d-default")
    assert {"tools.invoke.basic", "tools.invoke.cloud"} <= set(DEFAULT_SCOPES)
    assert {"tools.invoke.basic", "tools.invoke.cloud"} <= set(default_device.scopes)
    assert {"tools.invoke.basic", "tools.invoke.cloud"} <= set(
        ds.verify_token(default_token).scopes
    )

    device, token, approval_token = ds.issue_tokens(
        "d1",
        scopes=["chat.read", "tools.observe", "tools.approve"],
    )
    assert "chat.read" in device.scopes
    assert "chat.write" not in device.scopes
    assert "tools.approve" not in device.scopes
    assert device.approval_scopes == AUTHORITY_APPROVER_SCOPES
    assert "credentials.request" not in device.scopes
    assert ds.verify_token(token).scopes == ["chat.read", "tools.observe"]
    assert ds.verify_token(approval_token).scopes == AUTHORITY_APPROVER_SCOPES


def test_pairing_default_mobile_scopes_include_tool_invoke_routes():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing()

    assert {"tools.invoke.basic", "tools.invoke.cloud"} <= set(session.capabilities)

    claim = pm.claim_pairing(
        session.pairing_id,
        code=session.code,
        device_id="d-default",
    )
    assert claim["ok"]
    claimed = pm.get_pairing(session.pairing_id)
    assert claimed is not None
    assert {"tools.invoke.basic", "tools.invoke.cloud"} <= set(claimed.claimed_capabilities)

    approve = pm.approve_pairing_v2(session.pairing_id)
    assert approve["ok"]
    assert {"tools.invoke.basic", "tools.invoke.cloud"} <= set(approve["scopes"])


def test_device_store_touch_does_not_revive_revoked_device():
    from domain.p2p.device_store import DeviceStore

    tmp = tempfile.mkdtemp()
    store = DeviceStore(tmp)
    _device, token, _approval_token = store.issue_tokens("d1")
    stale_store = DeviceStore(tmp)

    store.revoke_device("d1")
    stale_store.touch("d1")

    fresh = DeviceStore(tmp)
    assert fresh.get_device("d1").status == "revoked"
    assert fresh.verify_token(token) is None


def test_legacy_device_record_does_not_keep_approval_scope_on_normal_token():
    from domain.p2p.device_store import DeviceRecord

    record = DeviceRecord.from_dict({
        "device_id": "legacy-mobile",
        "token_hash": "hash",
        "scopes": ["chat.read", "tools.approve"],
    })

    assert record.scopes == ["chat.read"]
    assert record.approval_scopes == []


def test_pairing_claim_requires_matching_code():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing()

    result = pm.claim_pairing(
        session.pairing_id,
        code="WRNG-CODE",
        device_id="d1",
    )

    assert not result["ok"]
    assert result["code"] == "PAIRING_CODE_MISMATCH"


def test_pairing_claim_rejects_scopes_outside_pc_grant():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing(capabilities=["chat.read", "chat.write"])

    result = pm.claim_pairing(
        session.pairing_id,
        code=session.code,
        device_id="d1",
        requested_capabilities=["chat.read", "authority.request.approve"],
    )

    assert not result["ok"]
    assert result["code"] == "SCOPE_NOT_ALLOWED"
    assert result["denied_scopes"] == ["authority.request.approve"]


def test_pairing_approve_rejects_scopes_outside_claimed_grant():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing(capabilities=["chat.read", "chat.write"])
    claim = pm.claim_pairing(
        session.pairing_id,
        code=session.code,
        device_id="d1",
        requested_capabilities=["chat.read"],
    )
    assert claim["ok"]

    result = pm.approve_pairing_v2(session.pairing_id, scopes=["chat.read", "chat.write"])

    assert not result["ok"]
    assert result["code"] == "SCOPE_NOT_ALLOWED"
    assert result["denied_scopes"] == ["chat.write"]


def test_pairing_approve_rejects_changed_claim_hash():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    pm = PairingManager(tmp)
    session = pm.start_pairing(capabilities=["chat.read"])
    claim = pm.claim_pairing(
        session.pairing_id,
        code=session.code,
        device_id="d1",
        requested_capabilities=["chat.read"],
    )
    assert claim["ok"]

    result = pm.approve_pairing_v2(
        session.pairing_id,
        claim_hash="sha256:" + "0" * 64,
        scopes=["chat.read"],
    )

    assert not result["ok"]
    assert result["code"] == "PAIRING_CLAIM_CHANGED"


def test_mobile_pairing_review_returns_admin_claim_details_without_secrets():
    from domain.p2p.pairing import PairingManager
    from blocks.mobile.pairing import run

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(
        capabilities=["chat.read", "chat.write", "tools.observe"],
    )
    _private_key, public_key = _x25519_keypair()
    claim = run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "device_label": "Haru iPhone",
        "public_key": "pk-mobile",
        "encryption_public_key": public_key,
        "requested_capabilities": ["chat.read", "chat.write"],
    }, None)
    assert claim["status"] == "ok"

    public_status = run({
        "action": "status",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
    }, None)
    assert public_status["status"] == "ok"
    assert "claimed_device_label" not in public_status["data"]
    assert "requested_scopes" not in public_status["data"]
    assert "claim_hash" not in public_status["data"]

    review = run({
        "action": "review",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
    }, None)

    assert review["status"] == "ok"
    data = review["data"]
    assert data["pairing"]["status"] == "claimed"
    assert data["claim"]["device_label"] == "Haru iPhone"
    assert data["claim"]["device_id_preview"].startswith("mobile")
    assert data["claim"]["requested_scopes"] == ["chat.read", "chat.write"]
    assert data["claim"]["allowed_scopes"] == ["chat.read", "chat.write", "tools.observe"]
    assert data["claim"]["verification_code"]
    assert data["claim"]["signing_key_fingerprint"].startswith("ed25519:")
    assert data["claim"]["encryption_key_fingerprint"].startswith("x25519:")
    assert data["security"]["public_status_minimized"] is True
    assert data["claim_hash"].startswith("sha256:")
    review_blob = json.dumps(data, ensure_ascii=False)
    assert session.code not in review_blob
    assert session.token_pickup_secret not in review_blob


def test_pairing_session_default_dict_is_public_and_storage_is_explicit():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    manager = PairingManager(tmp)
    session = manager.start_pairing(capabilities=["chat.read"])

    public = session.as_dict()
    assert public == session.public_dict()
    assert set(public) == {"pairing_id", "status", "expires_at"}
    assert "code" not in public
    assert "token_pickup_secret_hash" not in public

    storage = session.to_storage_dict()
    assert storage["code"] == session.code
    assert storage["token_pickup_secret_hash"]
    assert "token_pickup_secret" not in storage

    admin = session.admin_dict()
    assert admin["code"] == session.code
    assert admin["token_delivery_ready"] is False


def test_mobile_pairing_approve_requires_review_claim_hash():
    from domain.p2p.pairing import PairingManager
    from blocks.mobile.pairing import run

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(capabilities=["chat.read"])
    _private_key, public_key = _x25519_keypair()
    claim = run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "encryption_public_key": public_key,
    }, None)
    assert claim["status"] == "ok"

    approved = run({
        "action": "approve",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
    }, None)

    assert approved["status"] == "error"
    assert approved["error"]["code"] == "CLAIM_HASH_REQUIRED"


def test_mobile_pairing_token_pickup_uses_post_body_not_status_query():
    from domain.p2p.device_store import DeviceStore
    from domain.p2p.pairing import PairingManager
    from blocks.mobile.pairing import run

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(
        capabilities=["chat.read", "chat.write", "tools.observe"],
    )
    private_key, public_key = _x25519_keypair()

    claim = run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "device_label": "iPhone",
        "public_key": "pk-mobile",
        "encryption_public_key": public_key,
        "requested_capabilities": ["chat.read", "chat.write"],
    }, None)
    assert claim["status"] == "ok"
    assert claim["data"]["pairing"]["status"] == "claimed"
    assert "code" not in claim["data"]["pairing"]

    review = _review_claim(tmp, session.pairing_id)
    approved = run({
        "action": "approve",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "claim_hash": review["claim_hash"],
        "scopes": review["claim"]["requested_scopes"],
    }, None)
    assert approved["status"] == "ok"
    assert approved["data"]["pairing"]["status"] == "approved"
    assert "code" not in approved["data"]["pairing"]
    assert "device_token" not in approved["data"]
    assert "approval_token" not in approved["data"]
    assert "client_access_token" not in approved["data"]
    assert "approver_access_token" not in approved["data"]
    assert "public_key" not in approved["data"]["device"]

    without_code = run({
        "action": "status",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
    }, None)
    assert without_code["status"] == "ok"
    assert "device_token" not in without_code["data"]
    assert "code" not in without_code["data"]
    assert "code" not in without_code["data"]["pairing"]
    public_keys = {"pairing_id", "status", "expires_at", "pairing", "pc_label"}
    assert set(without_code["data"]) <= public_keys
    assert set(without_code["data"]["pairing"]) == {"pairing_id", "status", "expires_at"}
    assert "claimed_device_id" not in without_code["data"]
    assert "claimed_device_label" not in without_code["data"]
    assert "requested_scopes" not in without_code["data"]
    assert "token_delivery_ready" not in without_code["data"]

    with_code = run({
        "action": "status",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
    }, None)
    assert with_code["status"] == "ok"
    assert "device_token" not in with_code["data"]
    assert "code" not in with_code["data"]
    assert "code" not in with_code["data"]["pairing"]

    status_with_secret = run({
        "action": "status",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "pickup_secret": session.token_pickup_secret,
        "device_id": "mobile-1",
    }, None)
    assert status_with_secret["status"] == "ok"
    assert "token_delivery_envelope" not in status_with_secret["data"]
    assert "code" not in status_with_secret["data"]
    assert set(status_with_secret["data"]) <= public_keys

    with_secret = run({
        "action": "pickup_token_delivery",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "pickup_secret": session.token_pickup_secret,
        "device_id": "mobile-1",
    }, None)
    assert with_secret["status"] == "ok"
    assert "device_token" not in with_secret["data"]
    assert "approval_token" not in with_secret["data"]
    envelope = with_secret["data"]["token_delivery_envelope"]
    delivery = _decrypt_delivery_envelope(
        private_key,
        envelope,
        pairing_id=session.pairing_id,
        device_id="mobile-1",
    )
    assert delivery["device_token"].startswith("dtk_")
    assert delivery["approval_token"] == ""
    assert delivery["approval_scopes"] == []
    assert delivery["scopes"] == ["chat.read", "chat.write"]
    assert "code" not in with_secret["data"]
    assert "code" not in with_secret["data"]["pairing"]
    first_device = DeviceStore(tmp).get_device("mobile-1")
    assert first_device is not None
    first_token_hash = first_device.token_hash

    replay = run({
        "action": "pickup_token_delivery",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "pickup_secret": session.token_pickup_secret,
        "device_id": "mobile-1",
    }, None)
    assert replay["status"] == "ok"
    assert "device_token" not in replay["data"]
    assert replay["data"]["token_delivery_envelope"] == envelope
    assert "code" not in replay["data"]
    assert "code" not in replay["data"]["pairing"]
    replay_device = DeviceStore(tmp).get_device("mobile-1")
    assert replay_device is not None
    assert replay_device.token_hash == first_token_hash

    ack = run({
        "action": "ack_token_delivery",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "pickup_secret": session.token_pickup_secret,
        "device_id": "mobile-1",
        "delivery_id": envelope["delivery_id"],
    }, None)
    assert ack["status"] == "ok"
    after_ack = run({
        "action": "pickup_token_delivery",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "pickup_secret": session.token_pickup_secret,
        "device_id": "mobile-1",
    }, None)
    assert after_ack["status"] == "error"
    assert after_ack["error"]["code"] == "TOKEN_PICKUP_CONSUMED"


def test_pairing_start_returns_mobile_pickup_secret():
    from blocks.p2p import pairing_start

    tmp = tempfile.mkdtemp()

    result = pairing_start.run({
        "store_path": tmp,
        "_headers": {"Host": "192.168.1.44:8765"},
    }, None)

    assert result["status"] == "ok"
    assert result["data"]["pairing"]["pickup_secret"].startswith("pup_")


def test_mobile_pairing_status_rejects_wrong_pickup_secret_without_rotating():
    from domain.p2p.device_store import DeviceStore
    from domain.p2p.pairing import PairingManager
    from blocks.mobile.pairing import run

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(capabilities=["chat.read"])
    _private_key, public_key = _x25519_keypair()
    assert run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "encryption_public_key": public_key,
    }, None)["status"] == "ok"
    review = _review_claim(tmp, session.pairing_id)
    assert run({
        "action": "approve",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "claim_hash": review["claim_hash"],
        "scopes": review["claim"]["requested_scopes"],
    }, None)["status"] == "ok"
    issued = DeviceStore(tmp).get_device("mobile-1")
    assert issued is not None
    issued_hash = issued.token_hash

    wrong = run({
        "action": "pickup_token_delivery",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "pickup_secret": "pup_wrong",
        "device_id": "mobile-1",
    }, None)
    assert wrong["status"] == "error"
    assert wrong["error"]["code"] == "PICKUP_SECRET_MISMATCH"
    assert DeviceStore(tmp).get_device("mobile-1").token_hash == issued_hash


def test_mobile_pairing_claim_rejects_invalid_encryption_key():
    from domain.p2p.pairing import PairingManager
    from blocks.mobile.pairing import run

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(capabilities=["chat.read"])

    result = run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "encryption_public_key": "not-a-key",
    }, None)

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_ENCRYPTION_KEY"


def test_mobile_pairing_approve_rolls_back_when_delivery_store_fails(monkeypatch):
    from domain.p2p.pairing import PairingManager
    from blocks.mobile import pairing as pairing_block

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(capabilities=["chat.read"])
    _private_key, public_key = _x25519_keypair()
    assert pairing_block.run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "encryption_public_key": public_key,
    }, None)["status"] == "ok"

    def fail_store(*_args, **_kwargs):
        raise RuntimeError("disk unavailable")

    monkeypatch.setattr(PairingManager, "store_token_delivery", fail_store)

    review = _review_claim(tmp, session.pairing_id)
    result = pairing_block.run({
        "action": "approve",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "claim_hash": review["claim_hash"],
        "scopes": review["claim"]["requested_scopes"],
    }, None)

    assert result["status"] == "error"
    assert result["error"]["code"] == "TOKEN_DELIVERY_FAILED"
    restored = PairingManager(tmp).get_pairing(session.pairing_id)
    assert restored is not None
    assert restored.status == "claimed"


def test_pairing_concurrent_approve_is_compare_and_set():
    import threading

    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(capabilities=["chat.read"])
    assert PairingManager(tmp).claim_pairing(
        session.pairing_id,
        code=session.code,
        device_id="mobile-1",
        requested_capabilities=["chat.read"],
    )["ok"]

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def approve_once() -> None:
        manager = PairingManager(tmp)
        barrier.wait(timeout=5)
        results.append(manager.approve_pairing_v2(session.pairing_id))

    threads = [threading.Thread(target=approve_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 2
    assert sum(1 for result in results if result.get("ok")) == 1
    assert sorted(result.get("code", "") for result in results if not result.get("ok")) == ["PAIRING_NOT_CLAIMED"]


def test_mobile_pairing_status_splits_normal_and_approval_tokens():
    from domain.p2p.device_store import DeviceStore
    from domain.p2p.pairing import PairingManager
    from blocks.mobile.pairing import run

    tmp = tempfile.mkdtemp()
    session = PairingManager(tmp).start_pairing(
        capabilities=["chat.read", "chat.write", "tools.observe", *AUTHORITY_APPROVER_SCOPES],
    )
    private_key, public_key = _x25519_keypair()

    claim = run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "device_label": "iPhone",
        "encryption_public_key": public_key,
        "requested_capabilities": [
            "chat.read",
            "chat.write",
            "tools.observe",
            *AUTHORITY_APPROVER_SCOPES,
        ],
    }, None)
    assert claim["status"] == "ok"
    assert "code" not in claim["data"]["pairing"]

    review = _review_claim(tmp, session.pairing_id)
    approved = run({
        "action": "approve",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "claim_hash": review["claim_hash"],
        "scopes": review["claim"]["requested_scopes"],
    }, None)
    assert approved["status"] == "ok"
    assert "code" not in approved["data"]["pairing"]
    assert "device_token" not in approved["data"]
    assert "approval_token" not in approved["data"]
    assert "client_access_token" not in approved["data"]
    assert "approver_access_token" not in approved["data"]
    assert "public_key" not in approved["data"]["device"]

    status = run({
        "action": "pickup_token_delivery",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "pickup_secret": session.token_pickup_secret,
        "device_id": "mobile-1",
    }, None)

    assert status["status"] == "ok"
    data = status["data"]
    assert "device_token" not in data
    assert "approval_token" not in data
    delivery = _decrypt_delivery_envelope(
        private_key,
        data["token_delivery_envelope"],
        pairing_id=session.pairing_id,
        device_id="mobile-1",
    )
    assert delivery["device_token"].startswith("dtk_")
    assert delivery["approval_token"].startswith("dtk_")
    assert delivery["client_access_token"] == delivery["device_token"]
    assert delivery["approver_access_token"] == delivery["approval_token"]
    assert delivery["device_token"] != delivery["approval_token"]
    assert "tools.approve" not in delivery["scopes"]
    assert delivery["approval_scopes"] == AUTHORITY_APPROVER_SCOPES

    store = DeviceStore(tmp)
    normal = store.verify_token(delivery["device_token"])
    approver = store.verify_token(delivery["approval_token"])
    assert normal is not None
    assert "tools.approve" not in normal.scopes
    assert approver is not None
    assert approver.scopes == AUTHORITY_APPROVER_SCOPES


def test_mobile_pairing_token_pickup_and_ack_routes_return_only_encrypted_envelope():
    from domain.p2p.pairing import PairingManager

    tmp = tempfile.mkdtemp()
    server = _mobile_route_server()
    session = PairingManager(tmp).start_pairing(capabilities=["chat.read", "chat.write"])
    private_key, public_key = _x25519_keypair()

    claim = _invoke_mobile_route(
        server,
        "POST",
        f"/api/mobile/v1/pairings/{session.pairing_id}/claim",
        {
            "store_path": tmp,
            "code": session.code,
            "device_id": "mobile-route-1",
            "device_label": "Route Phone",
            "encryption_public_key": public_key,
            "requested_capabilities": ["chat.read", "chat.write"],
        },
    )
    assert claim["status"] == "ok"

    review = _invoke_mobile_route(
        server,
        "GET",
        f"/api/mobile/v1/pairings/{session.pairing_id}/review",
        {"store_path": tmp},
    )
    assert review["status"] == "ok"

    approved = _invoke_mobile_route(
        server,
        "POST",
        f"/api/mobile/v1/pairings/{session.pairing_id}/approve",
        {
            "store_path": tmp,
            "claim_hash": review["data"]["claim_hash"],
            "scopes": review["data"]["claim"]["requested_scopes"],
        },
    )
    assert approved["status"] == "ok"
    _assert_no_plain_tokens_outside_envelope(approved["data"])

    pickup = _invoke_mobile_route(
        server,
        "POST",
        f"/api/mobile/v1/pairings/{session.pairing_id}/token/pickup",
        {
            "store_path": tmp,
            "pickup_secret": session.token_pickup_secret,
            "device_id": "mobile-route-1",
        },
    )
    assert pickup["status"] == "ok"
    pickup_data = pickup["data"]
    _assert_no_plain_tokens_outside_envelope(pickup_data)
    envelope = pickup_data["token_delivery_envelope"]
    delivery = _decrypt_delivery_envelope(
        private_key,
        envelope,
        pairing_id=session.pairing_id,
        device_id="mobile-route-1",
    )
    assert delivery["device_token"].startswith("dtk_")
    assert delivery["scopes"] == ["chat.read", "chat.write"]
    rendered_pickup = json.dumps(pickup_data, sort_keys=True)
    assert delivery["device_token"] not in rendered_pickup

    missing_delivery_id = _invoke_mobile_route(
        server,
        "POST",
        f"/api/mobile/v1/pairings/{session.pairing_id}/token/ack",
        {
            "store_path": tmp,
            "pickup_secret": session.token_pickup_secret,
            "device_id": "mobile-route-1",
        },
    )
    assert missing_delivery_id["status"] == "error"
    assert missing_delivery_id["error"]["code"] == "DELIVERY_ID_REQUIRED"

    ack = _invoke_mobile_route(
        server,
        "POST",
        f"/api/mobile/v1/pairings/{session.pairing_id}/token/ack",
        {
            "store_path": tmp,
            "pickup_secret": session.token_pickup_secret,
            "device_id": "mobile-route-1",
            "delivery_id": envelope["delivery_id"],
        },
    )
    assert ack["status"] == "ok"
    _assert_no_plain_tokens_outside_envelope(ack["data"])
    assert "token_delivery_envelope" not in ack["data"]

    replay = _invoke_mobile_route(
        server,
        "POST",
        f"/api/mobile/v1/pairings/{session.pairing_id}/token/pickup",
        {
            "store_path": tmp,
            "pickup_secret": session.token_pickup_secret,
            "device_id": "mobile-route-1",
        },
    )
    assert replay["status"] == "error"
    assert replay["error"]["code"] == "TOKEN_PICKUP_CONSUMED"


def test_mobile_pairing_status_returns_pc_label(monkeypatch):
    from domain.p2p.pairing import PairingManager
    from blocks.mobile.pairing import run

    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("RUMI_PC_LABEL", "Haru MacBook")
    session = PairingManager(tmp).start_pairing(capabilities=["chat.read"])
    _private_key, public_key = _x25519_keypair()

    claim = run({
        "action": "claim",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
        "device_label": "iPhone",
        "encryption_public_key": public_key,
    }, None)
    assert claim["status"] == "ok"

    review = _review_claim(tmp, session.pairing_id)
    approved = run({
        "action": "approve",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "claim_hash": review["claim_hash"],
        "scopes": review["claim"]["requested_scopes"],
    }, None)
    assert approved["status"] == "ok"

    status = run({
        "action": "status",
        "store_path": tmp,
        "pairing_id": session.pairing_id,
        "code": session.code,
        "device_id": "mobile-1",
    }, None)

    assert status["status"] == "ok"
    assert status["data"]["pc_label"] == "Haru MacBook"


def test_mobile_pairing_base_urls_require_https_by_default():
    from domain.mobile.base_urls import mobile_base_urls_from_headers

    urls = mobile_base_urls_from_headers(
        {"Host": "localhost:8765"},
        local_addresses=["127.0.0.1", "192.168.1.44"],
    )

    assert urls == []


def test_mobile_pairing_base_urls_can_allow_cleartext_for_debug():
    from domain.mobile.base_urls import mobile_base_urls_from_headers

    urls = mobile_base_urls_from_headers(
        {"Host": "localhost:8765"},
        local_addresses=["127.0.0.1", "192.168.1.44"],
        allow_cleartext=True,
    )

    assert urls == ["http://192.168.1.44:8765"]


def test_pairing_start_returns_mobile_reachable_base_urls(monkeypatch):
    from blocks.p2p import pairing_start

    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(
        pairing_start,
        "mobile_base_urls_from_headers",
        lambda headers, **kwargs: ["https://rumi.example.com"],
    )

    result = pairing_start.run({
        "store_path": tmp,
        "_headers": {"Host": "localhost:8765"},
    }, None)

    assert result["status"] == "ok"
    assert result["data"]["pairing"]["base_urls"] == ["https://rumi.example.com"]


def test_mobile_conversations_list_create_get():
    from blocks.mobile.conversations import run

    r = run({"action": "list"}, None)
    assert r["status"] == "ok"
    assert "conversations" in r["data"]

    r = run({"action": "create", "title": "Mobile Test"}, None)
    assert r["status"] == "ok"
    cid = r["data"]["conversation"]["id"]

    r = run({"action": "get", "conversation_id": cid}, None)
    assert r["status"] == "ok"


def test_mobile_credentials_reject_client_supplied_ciphertext(monkeypatch):
    from blocks.mobile.credentials import run

    monkeypatch.setenv("RUMI_MOBILE_CREDENTIAL_TRANSFER", "1")
    r = run({
        "action": "create",
        "device_id": "d1",
        "provider_id": "openai",
        "ciphertext": "encrypted-payload",
        "nonce": "nonce-1",
    }, None)
    assert r["status"] == "error"
    assert r["error"]["code"] == "INVALID_INPUT"


def test_mobile_credentials_disabled_by_default(monkeypatch):
    from blocks.mobile.credentials import run

    monkeypatch.delenv("RUMI_MOBILE_CREDENTIAL_TRANSFER", raising=False)
    r = run({
        "action": "create",
        "device_id": "d1",
        "provider_id": "openai",
        "ciphertext": "encrypted-payload",
        "nonce": "nonce-1",
    }, None)

    assert r["status"] == "error"
    assert r["error"]["code"] == "FEATURE_DISABLED"


def test_mobile_credentials_reject_plaintext_fallback(monkeypatch):
    from blocks.mobile.credentials import run

    monkeypatch.setenv("RUMI_MOBILE_CREDENTIAL_TRANSFER", "1")
    r = run({
        "action": "create",
        "device_id": "d1",
        "provider_id": "openai",
        "api_key": "sk-test1234",
    }, None)

    assert r["status"] == "error"


def test_mobile_credentials_requires_reviewed_provider_reference(monkeypatch):
    from blocks.mobile.credentials import run

    monkeypatch.setenv("RUMI_MOBILE_CREDENTIAL_TRANSFER", "1")
    r = run({
        "action": "create",
        "device_id": "d1",
        "provider_id": "openai",
        "ciphertext": "encrypted-payload",
        "nonce": "nonce-1",
    }, None)
    assert r["status"] == "error"
    assert r["error"]["code"] == "INVALID_INPUT"
