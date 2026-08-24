from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .json_store import file_lock, load_json_object, save_json_object
from .peer_store import PeerStore, generate_shared_secret
from .settings import P2PSettings, default_store_path


PAIRING_PENDING = "pending"
PAIRING_ACCEPTED = "accepted"
PAIRING_CLAIMED = "claimed"
PAIRING_APPROVED = "approved"
PAIRING_REJECTED = "rejected"
PAIRING_EXPIRED = "expired"
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_DEFAULT_MOBILE_SCOPES = [
    "chat.read",
    "chat.write",
    "tools.observe",
    "tools.invoke.basic",
    "tools.invoke.cloud",
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _pairings_file(store_path: Path | None = None) -> Path:
    root = Path(store_path).expanduser() if store_path is not None else default_store_path()
    if root.name == "pairings.json":
        return root
    return root / "pairings.json"


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def hmac_compare_code(left: str, right: str) -> bool:
    return hmac.compare_digest(
        str(left or "").strip().upper(),
        str(right or "").strip().upper(),
    )


def _hash_pickup_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _new_pickup_secret() -> str:
    return "pup_" + secrets.token_urlsafe(32)


def _scope_not_allowed(requested: list[str], allowed: list[str]) -> list[str]:
    allowed_set = set(allowed)
    return sorted(scope for scope in requested if scope not in allowed_set)


def _stable_claim_payload(session: "PairingSession") -> dict[str, Any]:
    return {
        "pairing_id": session.pairing_id,
        "claimed_device_id": session.claimed_device_id,
        "claimed_device_public_key": session.claimed_device_public_key,
        "claimed_device_encryption_public_key": session.claimed_device_encryption_public_key,
        "claimed_capabilities": list(session.claimed_capabilities),
    }


def _digest_b32(value: bytes, *, length: int = 8) -> str:
    return base64.b32encode(value).decode("ascii").rstrip("=")[:length]


def _claim_digest(session: "PairingSession") -> bytes:
    payload = json.dumps(
        _stable_claim_payload(session),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _claim_hash(session: "PairingSession") -> str:
    return "sha256:" + _claim_digest(session).hex()


def _claim_verification_code(session: "PairingSession") -> str:
    code = _digest_b32(_claim_digest(session), length=8)
    return f"{code[:4]}-{code[4:]}"


def _fingerprint(value: str, *, prefix: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    digest = hashlib.sha256(cleaned.encode("utf-8")).digest()
    code = _digest_b32(digest, length=8)
    return f"{prefix}:{code[:4]}-{code[4:]}"


def _preview_id(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 12:
        return cleaned
    return f"{cleaned[:8]}...{cleaned[-4:]}"


@dataclass
class PairingSession:
    pairing_id: str
    code: str
    status: str
    expires_at: int
    created_at: int
    peer_id: str = ""
    peer_fingerprint: str = ""
    peer_label: str = ""
    capabilities: list[str] = field(default_factory=lambda: list(_DEFAULT_MOBILE_SCOPES))
    allowed_company_ids: list[str] = field(default_factory=list)
    accepted_at: int = 0
    rejected_at: int = 0
    reason: str = ""
    # v2 claim fields
    claimed_device_id: str = ""
    claimed_device_label: str = ""
    claimed_device_public_key: str = ""
    claimed_device_encryption_public_key: str = ""
    claimed_capabilities: list[str] = field(default_factory=list)
    claimed_at: int = 0
    token_pickup_secret_hash: str = ""
    token_pickup_consumed_at: int = 0
    token_delivery_envelope: dict[str, Any] = field(default_factory=dict)
    token_delivery_created_at: int = 0
    token_pickup_secret: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PairingSession":
        return cls(
            pairing_id=str(value.get("pairing_id") or ""),
            code=str(value.get("code") or ""),
            status=str(value.get("status") or PAIRING_PENDING),
            expires_at=int(value.get("expires_at") or 0),
            created_at=int(value.get("created_at") or _now_ms()),
            peer_id=str(value.get("peer_id") or ""),
            peer_fingerprint=str(value.get("peer_fingerprint") or value.get("fingerprint") or ""),
            peer_label=str(value.get("peer_label") or value.get("label") or ""),
            capabilities=_string_list(value.get("capabilities")) or list(_DEFAULT_MOBILE_SCOPES),
            allowed_company_ids=_string_list(value.get("allowed_company_ids")),
            accepted_at=int(value.get("accepted_at") or 0),
            rejected_at=int(value.get("rejected_at") or 0),
            reason=str(value.get("reason") or ""),
            claimed_device_id=str(value.get("claimed_device_id") or ""),
            claimed_device_label=str(value.get("claimed_device_label") or ""),
            claimed_device_public_key=str(value.get("claimed_device_public_key") or ""),
            claimed_device_encryption_public_key=str(value.get("claimed_device_encryption_public_key") or ""),
            claimed_capabilities=_string_list(value.get("claimed_capabilities")),
            claimed_at=int(value.get("claimed_at") or 0),
            token_pickup_secret_hash=str(value.get("token_pickup_secret_hash") or ""),
            token_pickup_consumed_at=int(value.get("token_pickup_consumed_at") or 0),
            token_delivery_envelope=dict(value.get("token_delivery_envelope") or {})
            if isinstance(value.get("token_delivery_envelope"), dict)
            else {},
            token_delivery_created_at=int(value.get("token_delivery_created_at") or 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return self.public_dict()

    def admin_dict(self) -> dict[str, Any]:
        return {
            "pairing_id": self.pairing_id,
            "code": self.code,
            "status": self.status,
            "expires_at": int(self.expires_at),
            "created_at": int(self.created_at),
            "peer_id": self.peer_id,
            "peer_fingerprint": self.peer_fingerprint,
            "peer_label": self.peer_label,
            "capabilities": list(self.capabilities),
            "allowed_company_ids": list(self.allowed_company_ids),
            "accepted_at": int(self.accepted_at),
            "approved_at": int(self.accepted_at),
            "rejected_at": int(self.rejected_at),
            "reason": self.reason,
            "claimed_device_id": self.claimed_device_id,
            "claimed_device_label": self.claimed_device_label,
            "claimed_capabilities": list(self.claimed_capabilities),
            "requested_scopes": list(self.claimed_capabilities),
            "claimed_at": int(self.claimed_at),
            "token_pickup_consumed_at": int(self.token_pickup_consumed_at),
            "token_delivery_ready": bool(self.token_delivery_envelope),
            "token_delivery_created_at": int(self.token_delivery_created_at),
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "pairing_id": self.pairing_id,
            "status": self.status,
            "expires_at": int(self.expires_at),
        }

    def start_response_dict(self) -> dict[str, Any]:
        response = self.public_dict()
        response.update(
            {
                "code": self.code,
                "pairing_code": self.code,
                "pickup_secret": self.token_pickup_secret,
                "token_pickup_secret": self.token_pickup_secret,
            }
        )
        return response

    def claim_hash(self) -> str:
        return _claim_hash(self)

    def review_dict(self) -> dict[str, Any]:
        requested_scopes = list(self.claimed_capabilities)
        allowed_scopes = list(self.capabilities)
        return {
            "pairing": {
                "pairing_id": self.pairing_id,
                "status": self.status,
                "expires_at": int(self.expires_at),
                "claimed_at": int(self.claimed_at),
            },
            "claim": {
                "device_label": self.claimed_device_label or "Rumi Mobile",
                "device_id_preview": _preview_id(self.claimed_device_id),
                "requested_scopes": requested_scopes,
                "allowed_scopes": allowed_scopes,
                "denied_scopes": _scope_not_allowed(requested_scopes, allowed_scopes),
                "signing_key_fingerprint": _fingerprint(
                    self.claimed_device_public_key,
                    prefix="ed25519",
                ),
                "encryption_key_fingerprint": _fingerprint(
                    self.claimed_device_encryption_public_key,
                    prefix="x25519",
                ),
                "verification_code": _claim_verification_code(self),
            },
            "security": {
                "token_delivery": "x25519-aes-gcm",
                "pickup": "post-body-only",
                "public_status_minimized": True,
            },
            "claim_hash": self.claim_hash(),
        }

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "pairing_id": self.pairing_id,
            "code": self.code,
            "status": self.status,
            "expires_at": int(self.expires_at),
            "created_at": int(self.created_at),
            "peer_id": self.peer_id,
            "peer_fingerprint": self.peer_fingerprint,
            "peer_label": self.peer_label,
            "capabilities": list(self.capabilities),
            "allowed_company_ids": list(self.allowed_company_ids),
            "accepted_at": int(self.accepted_at),
            "approved_at": int(self.accepted_at),
            "rejected_at": int(self.rejected_at),
            "reason": self.reason,
            "claimed_device_id": self.claimed_device_id,
            "claimed_device_label": self.claimed_device_label,
            "claimed_device_public_key": self.claimed_device_public_key,
            "claimed_device_encryption_public_key": self.claimed_device_encryption_public_key,
            "claimed_capabilities": list(self.claimed_capabilities),
            "requested_scopes": list(self.claimed_capabilities),
            "claimed_at": int(self.claimed_at),
            "token_pickup_secret_hash": self.token_pickup_secret_hash,
            "token_pickup_consumed_at": int(self.token_pickup_consumed_at),
            "token_delivery_envelope": dict(self.token_delivery_envelope),
            "token_delivery_ready": bool(self.token_delivery_envelope),
            "token_delivery_created_at": int(self.token_delivery_created_at),
        }

    def expired(self, now: int | None = None) -> bool:
        return int(self.expires_at) <= int(now if now is not None else _now_ms())


class PairingManager:
    def __init__(self, store_path: Path | None = None, *, peer_store: PeerStore | None = None) -> None:
        self.store_path = Path(store_path).expanduser() if store_path is not None else default_store_path()
        self.path = _pairings_file(self.store_path)
        self.peer_store = peer_store or PeerStore(self.store_path)
        self._data = self._load()

    def start_pairing(
        self,
        *,
        peer_id: str = "",
        peer_fingerprint: str = "",
        peer_label: str = "",
        ttl_seconds: int | None = None,
        capabilities: list[str] | None = None,
        allowed_company_ids: list[str] | None = None,
        settings: P2PSettings | None = None,
    ) -> PairingSession:
        ttl = int(ttl_seconds or (settings.pairing_ttl_seconds if settings is not None else 300))
        now = _now_ms()
        pickup_secret = _new_pickup_secret()
        session = PairingSession(
            pairing_id="pair-" + uuid.uuid4().hex,
            code=self._new_unique_code(),
            status=PAIRING_PENDING,
            expires_at=now + max(1, ttl) * 1000,
            created_at=now,
            token_pickup_secret_hash=_hash_pickup_secret(pickup_secret),
            token_pickup_secret=pickup_secret,
            peer_id=str(peer_id or "").strip(),
            peer_fingerprint=str(peer_fingerprint or "").strip(),
            peer_label=str(peer_label or "").strip(),
            capabilities=_string_list(capabilities) or list(_DEFAULT_MOBILE_SCOPES),
            allowed_company_ids=_string_list(allowed_company_ids),
        )
        with self._file_lock():
            self._data = self._load()
            session.code = self._new_unique_code()
            sessions = self._sessions()
            sessions[session.pairing_id] = session
            self._save_sessions(sessions)
        return session

    def accept_pairing(
        self,
        code: str,
        *,
        peer_id: str = "",
        peer_fingerprint: str = "",
        peer_label: str = "",
        hmac_secret: str = "",
        capabilities: list[str] | None = None,
        allowed_company_ids: list[str] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        with self._file_lock():
            self._data = self._load()
            session = self._find_by_code(code)
            if session is None:
                return {"ok": False, "reason": "pairing code not found", "code": "PAIRING_NOT_FOUND"}
            now = int(now_ms if now_ms is not None else _now_ms())
            if session.status != PAIRING_PENDING:
                return {"ok": False, "reason": f"pairing is {session.status}", "code": "PAIRING_NOT_PENDING"}
            if session.expired(now):
                session.status = PAIRING_EXPIRED
                session.reason = "expired"
                self._replace(session)
                return {"ok": False, "reason": "pairing code expired", "code": "PAIRING_EXPIRED"}

            resolved_peer_id = str(peer_id or session.peer_id or "").strip()
            if not resolved_peer_id:
                return {"ok": False, "reason": "peer_id is required", "code": "INVALID_INPUT"}
            secret = str(hmac_secret or "").strip() or generate_shared_secret()
            resolved_capabilities = (
                _string_list(capabilities)
                or session.capabilities
                or list(_DEFAULT_MOBILE_SCOPES)
            )
            resolved_allowed_companies = _string_list(allowed_company_ids) or session.allowed_company_ids
            peer = self.peer_store.approve_peer(
                resolved_peer_id,
                fingerprint=str(peer_fingerprint or session.peer_fingerprint or "").strip(),
                hmac_secret=secret,
                capabilities=resolved_capabilities,
                allowed_company_ids=resolved_allowed_companies,
                label=str(peer_label or session.peer_label or "").strip(),
                metadata={"pairing_id": session.pairing_id},
            )
            session.status = PAIRING_ACCEPTED
            session.accepted_at = now
            session.peer_id = peer.peer_id
            session.peer_fingerprint = peer.fingerprint
            session.peer_label = peer.label
            session.capabilities = list(peer.capabilities)
            session.allowed_company_ids = list(peer.allowed_company_ids)
            self._replace(session)
            return {
                "ok": True,
                "pairing": session.admin_dict(),
                "peer": peer.as_dict(),
                "hmac_secret": secret,
            }

    def reject_pairing(self, code: str, *, reason: str = "", now_ms: int | None = None) -> dict[str, Any]:
        with self._file_lock():
            self._data = self._load()
            session = self._find_by_code(code)
            if session is None:
                return {"ok": False, "reason": "pairing code not found", "code": "PAIRING_NOT_FOUND"}
            if session.status not in {PAIRING_PENDING, PAIRING_CLAIMED}:
                return {"ok": False, "reason": f"pairing is {session.status}", "code": "PAIRING_NOT_PENDING"}
            session.status = PAIRING_REJECTED
            session.rejected_at = int(now_ms if now_ms is not None else _now_ms())
            session.reason = str(reason or "rejected")
            self._replace(session)
            return {"ok": True, "pairing": session.admin_dict()}

    def claim_pairing(
        self,
        pairing_id: str,
        *,
        code: str = "",
        device_id: str,
        device_label: str = "",
        device_public_key: str = "",
        device_encryption_public_key: str = "",
        requested_capabilities: list[str] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record a mobile device's claim against a pending pairing session.

        The PC operator must still approve before a device token is issued.
        """
        with self._file_lock():
            self._data = self._load()
            sessions = self._sessions()
            session = sessions.get(str(pairing_id or "").strip())
            if session is None:
                return {"ok": False, "reason": "pairing not found", "code": "PAIRING_NOT_FOUND"}
            now = int(now_ms if now_ms is not None else _now_ms())
            if session.status != PAIRING_PENDING:
                return {"ok": False, "reason": f"pairing is {session.status}", "code": "PAIRING_NOT_PENDING"}
            if not str(code or "").strip() or not hmac_compare_code(code, session.code):
                return {"ok": False, "reason": "pairing code does not match", "code": "PAIRING_CODE_MISMATCH"}
            if session.expired(now):
                session.status = PAIRING_EXPIRED
                session.reason = "expired"
                self._replace(session)
                return {"ok": False, "reason": "pairing code expired", "code": "PAIRING_EXPIRED"}
            requested = _string_list(requested_capabilities)
            allowed = list(session.capabilities or _DEFAULT_MOBILE_SCOPES)
            denied = _scope_not_allowed(requested, allowed)
            if denied:
                return {
                    "ok": False,
                    "reason": "requested capabilities exceed pairing grant",
                    "code": "SCOPE_NOT_ALLOWED",
                    "denied_scopes": denied,
                }
            session.claimed_device_id = str(device_id or "").strip()
            session.claimed_device_label = str(device_label or "").strip()
            session.claimed_device_public_key = str(device_public_key or "").strip()
            session.claimed_device_encryption_public_key = str(device_encryption_public_key or "").strip()
            session.claimed_capabilities = requested or allowed
            session.claimed_at = now
            session.status = PAIRING_CLAIMED
            self._replace(session)
            return {"ok": True, "pairing": session.public_dict()}

    def get_pairing(self, pairing_id: str) -> PairingSession | None:
        """Return one pairing after durably normalizing authoritative expiry."""

        self.cleanup_expired()
        self._data = self._load()
        return self._sessions().get(str(pairing_id or "").strip())

    def approve_pairing_v2(
        self,
        pairing_id: str,
        *,
        claim_hash: str = "",
        scopes: list[str] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Approve a claimed pairing and mark it accepted (v2 flow).

        The actual device token is issued by the caller (DeviceStore) since
        PairingManager should not depend on crypto. This method just flips
        the session status and returns claim info for token issuance.
        """
        with self._file_lock():
            self._data = self._load()
            sessions = self._sessions()
            session = sessions.get(str(pairing_id or "").strip())
            if session is None:
                return {"ok": False, "reason": "pairing not found", "code": "PAIRING_NOT_FOUND"}
            now = int(now_ms if now_ms is not None else _now_ms())
            if session.status != PAIRING_CLAIMED:
                return {"ok": False, "reason": f"pairing is {session.status}", "code": "PAIRING_NOT_CLAIMED"}
            if not session.claimed_device_id:
                return {"ok": False, "reason": "pairing has not been claimed", "code": "NOT_CLAIMED"}
            if session.expired(now):
                session.status = PAIRING_EXPIRED
                session.reason = "expired"
                self._replace(session)
                return {"ok": False, "reason": "pairing code expired", "code": "PAIRING_EXPIRED"}
            expected_claim_hash = session.claim_hash()
            provided_claim_hash = str(claim_hash or "").strip()
            if provided_claim_hash and not hmac.compare_digest(provided_claim_hash, expected_claim_hash):
                return {
                    "ok": False,
                    "reason": "pairing claim changed; refresh approval details",
                    "code": "PAIRING_CLAIM_CHANGED",
                    "claim_hash": expected_claim_hash,
                }
            allowed = list(session.claimed_capabilities or session.capabilities or _DEFAULT_MOBILE_SCOPES)
            requested = _string_list(scopes)
            denied = _scope_not_allowed(requested, allowed)
            if denied:
                return {
                    "ok": False,
                    "reason": "approved scopes exceed claimed pairing grant",
                    "code": "SCOPE_NOT_ALLOWED",
                    "denied_scopes": denied,
                }
            resolved_scopes = requested or allowed
            session.status = PAIRING_APPROVED
            session.accepted_at = now
            session.peer_id = session.claimed_device_id
            session.peer_label = session.claimed_device_label
            session.capabilities = resolved_scopes
            self._replace(session)
            return {
                "ok": True,
                "pairing": session.admin_dict(),
                "device_id": session.claimed_device_id,
                "device_label": session.claimed_device_label,
                "device_public_key": session.claimed_device_public_key,
                "device_encryption_public_key": session.claimed_device_encryption_public_key,
                "scopes": resolved_scopes,
            }

    def store_token_delivery(
        self,
        pairing_id: str,
        *,
        envelope: dict[str, Any],
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        with self._file_lock():
            self._data = self._load()
            sessions = self._sessions()
            session = sessions.get(str(pairing_id or "").strip())
            if session is None:
                return {"ok": False, "reason": "pairing not found", "code": "PAIRING_NOT_FOUND"}
            if session.status != PAIRING_APPROVED:
                return {"ok": False, "reason": f"pairing is {session.status}", "code": "PAIRING_NOT_APPROVED"}
            if not isinstance(envelope, dict) or not envelope:
                return {"ok": False, "reason": "token delivery envelope is required", "code": "INVALID_INPUT"}
            session.token_delivery_envelope = dict(envelope)
            session.token_delivery_created_at = int(now_ms if now_ms is not None else _now_ms())
            session.token_pickup_consumed_at = 0
            self._replace(session)
            return {"ok": True, "pairing": session.admin_dict()}

    def rollback_approved_pairing(
        self,
        pairing_id: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        with self._file_lock():
            self._data = self._load()
            sessions = self._sessions()
            session = sessions.get(str(pairing_id or "").strip())
            if session is None:
                return {"ok": False, "reason": "pairing not found", "code": "PAIRING_NOT_FOUND"}
            if session.status != PAIRING_APPROVED:
                return {"ok": False, "reason": f"pairing is {session.status}", "code": "PAIRING_NOT_APPROVED"}
            session.status = PAIRING_CLAIMED
            session.accepted_at = 0
            session.peer_id = ""
            session.peer_label = ""
            session.capabilities = list(session.claimed_capabilities or session.capabilities)
            session.reason = str(reason or "approval rolled back")
            session.token_delivery_envelope = {}
            session.token_delivery_created_at = 0
            session.token_pickup_consumed_at = 0
            self._replace(session)
            return {"ok": True, "pairing": session.admin_dict()}

    def peek_token_delivery(
        self,
        pairing_id: str,
        *,
        pickup_secret: str,
        device_id: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        with self._file_lock():
            self._data = self._load()
            checked = self._validate_token_pickup(pairing_id, pickup_secret=pickup_secret, device_id=device_id, now_ms=now_ms)
            if not checked.get("ok"):
                return checked
            session = checked["session"]
            return {
                "ok": True,
                "pairing": session.public_dict(),
                "device_id": session.claimed_device_id,
                "device_label": session.claimed_device_label,
                "device_public_key": session.claimed_device_public_key,
                "device_encryption_public_key": session.claimed_device_encryption_public_key,
                "scopes": list(session.capabilities),
                "token_delivery_envelope": dict(session.token_delivery_envelope),
            }

    def ack_token_delivery(
        self,
        pairing_id: str,
        *,
        pickup_secret: str,
        device_id: str,
        delivery_id: str = "",
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        with self._file_lock():
            self._data = self._load()
            checked = self._validate_token_pickup(pairing_id, pickup_secret=pickup_secret, device_id=device_id, now_ms=now_ms)
            if not checked.get("ok"):
                return checked
            session = checked["session"]
            expected_delivery_id = str(session.token_delivery_envelope.get("delivery_id") or "")
            provided_delivery_id = str(delivery_id or "").strip()
            if not provided_delivery_id:
                return {"ok": False, "reason": "delivery_id is required", "code": "DELIVERY_ID_REQUIRED"}
            if not hmac.compare_digest(provided_delivery_id, expected_delivery_id):
                return {"ok": False, "reason": "delivery_id does not match", "code": "DELIVERY_ID_MISMATCH"}
            session.token_pickup_consumed_at = int(now_ms if now_ms is not None else _now_ms())
            self._replace(session)
            return {
                "ok": True,
                "pairing": session.public_dict(),
            }

    def consume_token_pickup(
        self,
        pairing_id: str,
        *,
        pickup_secret: str,
        device_id: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        return self.ack_token_delivery(
            pairing_id,
            pickup_secret=pickup_secret,
            device_id=device_id,
            now_ms=now_ms,
        )

    def _validate_token_pickup(
        self,
        pairing_id: str,
        *,
        pickup_secret: str,
        device_id: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        sessions = self._sessions()
        session = sessions.get(str(pairing_id or "").strip())
        if session is None:
            return {"ok": False, "reason": "pairing not found", "code": "PAIRING_NOT_FOUND"}
        now = int(now_ms if now_ms is not None else _now_ms())
        if session.status != PAIRING_APPROVED:
            return {"ok": False, "reason": f"pairing is {session.status}", "code": "PAIRING_NOT_APPROVED"}
        if session.expired(now):
            session.status = PAIRING_EXPIRED
            session.reason = "expired"
            self._replace(session)
            return {"ok": False, "reason": "pairing code expired", "code": "PAIRING_EXPIRED"}
        if session.token_pickup_consumed_at:
            return {"ok": False, "reason": "token pickup was already used", "code": "TOKEN_PICKUP_CONSUMED"}
        if not session.token_delivery_envelope:
            return {"ok": False, "reason": "token delivery is not ready", "code": "TOKEN_DELIVERY_NOT_READY"}
        if not str(device_id or "").strip() or not hmac_compare_code(device_id, session.claimed_device_id):
            return {"ok": False, "reason": "device_id does not match", "code": "DEVICE_MISMATCH"}
        expected_hash = str(session.token_pickup_secret_hash or "")
        provided_hash = _hash_pickup_secret(str(pickup_secret or "").strip())
        if not expected_hash or not hmac.compare_digest(expected_hash, provided_hash):
            return {"ok": False, "reason": "pickup secret does not match", "code": "PICKUP_SECRET_MISMATCH"}
        return {"ok": True, "session": session}

    def list_pairings(self) -> list[dict[str, Any]]:
        self.cleanup_expired()
        self._data = self._load()
        return [session.admin_dict() for session in self._sessions().values()]

    def cleanup_expired(self, *, now_ms: int | None = None) -> None:
        now = int(now_ms if now_ms is not None else _now_ms())
        with self._file_lock():
            self._data = self._load()
            sessions = self._sessions()
            changed = False
            for session in sessions.values():
                if session.status in {PAIRING_PENDING, PAIRING_CLAIMED} and session.expired(now):
                    session.status = PAIRING_EXPIRED
                    session.reason = "expired"
                    changed = True
            if changed:
                self._save_sessions(sessions)

    def _find_by_code(self, code: str) -> PairingSession | None:
        target = str(code or "").strip().upper()
        if not target:
            return None
        for session in self._sessions().values():
            if session.code.upper() == target:
                return session
        return None

    def _replace(self, session: PairingSession) -> None:
        sessions = self._sessions()
        sessions[session.pairing_id] = session
        self._save_sessions(sessions)

    def _new_unique_code(self) -> str:
        existing = {session.code for session in self._sessions().values()}
        while True:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
            code = code[:4] + "-" + code[4:]
            if code not in existing:
                return code

    def _sessions(self) -> dict[str, PairingSession]:
        raw = self._data.setdefault("pairings", {})
        if not isinstance(raw, dict):
            raw = {}
            self._data["pairings"] = raw
        return {
            key: PairingSession.from_dict(value)
            for key, value in raw.items()
            if isinstance(value, dict) and str(value.get("pairing_id") or key).strip()
        }

    def _load(self) -> dict[str, Any]:
        data = load_json_object(self.path)
        data.setdefault("schema_version", 1)
        data.setdefault("pairings", {})
        return data

    def _save_sessions(self, sessions: dict[str, PairingSession]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["schema_version"] = 1
        self._data["updated_at"] = _now_ms()
        self._data["pairings"] = {session_id: session.to_storage_dict() for session_id, session in sessions.items()}
        save_json_object(self.path, self._data)

    def _file_lock(self) -> AbstractContextManager[None]:
        return file_lock(self.path, lock_name="pairing store")
