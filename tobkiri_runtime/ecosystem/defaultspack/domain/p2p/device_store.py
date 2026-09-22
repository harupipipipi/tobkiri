"""Device token store for scoped mobile authentication.

Each paired mobile device receives a scoped device token (not the full-power
HMAC key). Tokens are stored as hashes; the plaintext is returned only once
at approval time. Tokens carry scopes and can be revoked individually.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .json_store import file_lock, load_json_object, save_json_object
from .settings import default_store_path

DEVICE_ACTIVE = "active"
DEVICE_STAGED = "staged"
DEVICE_REVOKED = "revoked"

DEFAULT_SCOPES = [
    "chat.read",
    "chat.write",
    "tools.observe",
    "tools.invoke.basic",
    "tools.invoke.cloud",
]
APPROVER_SCOPES = [
    "authority.request.list",
    "authority.request.read",
    "authority.request.approve",
    "authority.request.deny",
]
LEGACY_APPROVER_SCOPES = {"tools.approve"}
ALL_SCOPES = DEFAULT_SCOPES + APPROVER_SCOPES + ["credentials.request"]
_TOUCH_THROTTLE_MS = 30_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _devices_file(store_path: Path | None = None) -> Path:
    root = Path(store_path).expanduser() if store_path is not None else default_store_path()
    if root.name == "devices.json":
        return root
    return root / "devices.json"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    return "dtk_" + secrets.token_urlsafe(32)


def _confirmation_emoji() -> str:
    emojis = ["🔵", "🌟", "🔴", "🟢", "🟡", "🟣", "⚡", "🌙", "☀️", "🔑"]
    return secrets.choice(emojis)


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({str(v).strip() for v in values if str(v).strip()})


def _split_device_scopes(scopes: list[str]) -> tuple[list[str], list[str]]:
    normal: list[str] = []
    approver: list[str] = []
    wants_approver = False
    allowed_normal = set(DEFAULT_SCOPES + ["credentials.request"])
    allowed_approver = set(APPROVER_SCOPES)
    for scope in scopes:
        if scope in LEGACY_APPROVER_SCOPES:
            wants_approver = True
            continue
        if scope in allowed_approver:
            wants_approver = True
            if scope not in approver:
                approver.append(scope)
            continue
        if scope in allowed_normal and scope not in normal:
            normal.append(scope)
    if wants_approver and not approver:
        approver = list(APPROVER_SCOPES)
    return sorted(normal), sorted(approver)


@dataclass
class DeviceRecord:
    device_id: str
    profile_id: str = "default"
    label: str = ""
    public_key: str = ""
    encryption_public_key: str = ""
    fingerprint: str = ""
    token_hash: str = ""
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    approval_token_hash: str = ""
    approval_scopes: list[str] = field(default_factory=list)
    status: str = DEVICE_ACTIVE
    pairing_id: str = ""
    confirmation_code: str = ""
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)
    last_seen_at: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeviceRecord":
        raw_scopes = _string_list(value.get("scopes")) or list(DEFAULT_SCOPES)
        approval_token_hash = str(value.get("approval_token_hash") or "")
        explicit_approval_scopes = _string_list(value.get("approval_scopes"))
        normal_scopes, legacy_approval_scopes = _split_device_scopes(raw_scopes)
        normalized_approval_scopes: list[str] = []
        if approval_token_hash:
            if explicit_approval_scopes:
                _normal_from_approval, normalized_approval_scopes = _split_device_scopes(explicit_approval_scopes)
            else:
                normalized_approval_scopes = legacy_approval_scopes or list(APPROVER_SCOPES)
        return cls(
            device_id=str(value.get("device_id") or value.get("id") or ""),
            profile_id=str(value.get("profile_id") or "default"),
            label=str(value.get("label") or ""),
            public_key=str(value.get("public_key") or value.get("device_public_key") or ""),
            encryption_public_key=str(value.get("encryption_public_key") or value.get("device_encryption_public_key") or ""),
            fingerprint=str(value.get("fingerprint") or ""),
            token_hash=str(value.get("token_hash") or ""),
            scopes=normal_scopes,
            approval_token_hash=approval_token_hash,
            approval_scopes=normalized_approval_scopes,
            status=str(value.get("status") or DEVICE_ACTIVE),
            pairing_id=str(value.get("pairing_id") or ""),
            confirmation_code=str(value.get("confirmation_code") or ""),
            created_at=int(value.get("created_at") or _now_ms()),
            updated_at=int(value.get("updated_at") or _now_ms()),
            last_seen_at=int(value.get("last_seen_at") or 0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "profile_id": self.profile_id,
            "label": self.label,
            "public_key": self.public_key[:16] + "…" if len(self.public_key) > 16 else self.public_key,
            "encryption_key_configured": bool(self.encryption_public_key),
            "fingerprint": self.fingerprint,
            "scopes": list(self.scopes),
            "approval_scopes": list(self.approval_scopes),
            "has_approval_token": bool(self.approval_token_hash),
            "status": self.status,
            "pairing_id": self.pairing_id,
            "confirmation_code": self.confirmation_code,
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
            "last_seen_at": int(self.last_seen_at),
        }

    @property
    def active(self) -> bool:
        return self.status == DEVICE_ACTIVE


class DeviceStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = _devices_file(path)
        self._data = self._load()

    def list_devices(self) -> list[dict[str, Any]]:
        self._data = self._load()
        return [d.as_dict() for d in self._devices().values()]

    def get_device(self, device_id: str) -> DeviceRecord | None:
        self._data = self._load()
        return self._devices().get(str(device_id or "").strip())

    def verify_token(self, token: str) -> DeviceRecord | None:
        """Look up a device by token hash. Returns the device if active."""
        self._data = self._load()
        token_hash = _hash_token(token)
        for device in self._devices().values():
            if device.token_hash == token_hash and device.active:
                return device
            if device.approval_token_hash == token_hash and device.active:
                return replace(
                    device,
                    token_hash=device.approval_token_hash,
                    scopes=list(device.approval_scopes or APPROVER_SCOPES),
                )
        return None

    def issue_tokens(
        self,
        device_id: str,
        *,
        label: str = "",
        public_key: str = "",
        encryption_public_key: str = "",
        fingerprint: str = "",
        scopes: list[str] | None = None,
        pairing_id: str = "",
        profile_id: str = "default",
        activate: bool = True,
    ) -> tuple[DeviceRecord, str, str]:
        """Create or refresh split device tokens.

        Returns ``(record, device_token, approval_token)``. The approval token is
        only issued when the requested scopes include an approver scope. Plaintext
        tokens are returned ONLY here and never stored.
        """
        clean_id = str(device_id or "").strip()
        if not clean_id:
            raise ValueError("device_id is required")
        with self._file_lock():
            self._data = self._load()
            devices = self._devices()
            now = _now_ms()
            plaintext = _generate_token()
            requested_scopes = _string_list(scopes) if scopes else list(DEFAULT_SCOPES)
            resolved_scopes, approval_scopes = _split_device_scopes(requested_scopes)
            if not resolved_scopes and not scopes:
                resolved_scopes = list(DEFAULT_SCOPES)
            approval_plaintext = _generate_token() if approval_scopes else ""
            code = f"{_confirmation_emoji()}・{secrets.randbelow(90) + 10}"
            device = DeviceRecord(
                device_id=clean_id,
                profile_id=str(profile_id or "default").strip() or "default",
                label=label,
                public_key=public_key,
                encryption_public_key=encryption_public_key,
                fingerprint=fingerprint,
                token_hash=_hash_token(plaintext),
                scopes=resolved_scopes,
                approval_token_hash=_hash_token(approval_plaintext) if approval_plaintext else "",
                approval_scopes=approval_scopes,
                status=DEVICE_ACTIVE if activate else DEVICE_STAGED,
                pairing_id=pairing_id,
                confirmation_code=code,
                created_at=devices[clean_id].created_at if clean_id in devices else now,
                updated_at=now,
            )
            devices[clean_id] = device
            self._save_devices(devices)
        return device, plaintext, approval_plaintext

    def activate_staged_device(
        self,
        device_id: str,
        *,
        pairing_id: str,
        profile_id: str,
    ) -> DeviceRecord | None:
        """Activate only the exact device record staged by a pairing commit."""

        clean_id = str(device_id or "").strip()
        with self._file_lock():
            self._data = self._load()
            devices = self._devices()
            device = devices.get(clean_id)
            if (
                device is None
                or device.status not in {DEVICE_STAGED, DEVICE_ACTIVE}
                or device.pairing_id != str(pairing_id or "").strip()
                or device.profile_id != str(profile_id or "").strip()
            ):
                return None
            if device.status == DEVICE_STAGED:
                device.status = DEVICE_ACTIVE
                device.updated_at = _now_ms()
                devices[clean_id] = device
                self._save_devices(devices)
            return device

    def issue_token(
        self,
        device_id: str,
        *,
        label: str = "",
        public_key: str = "",
        encryption_public_key: str = "",
        fingerprint: str = "",
        scopes: list[str] | None = None,
        pairing_id: str = "",
        profile_id: str = "default",
    ) -> tuple[DeviceRecord, str]:
        """Create or refresh the normal device token.

        Backwards-compatible wrapper around ``issue_tokens``. If approver scopes
        are requested, their token is stored and can be returned by callers that
        use ``issue_tokens`` directly.
        """
        device, plaintext, _approval_plaintext = self.issue_tokens(
            device_id,
            label=label,
            public_key=public_key,
            encryption_public_key=encryption_public_key,
            fingerprint=fingerprint,
            scopes=scopes,
            pairing_id=pairing_id,
            profile_id=profile_id,
        )
        return device, plaintext

    def revoke_device(self, device_id: str) -> DeviceRecord | None:
        clean_id = str(device_id or "").strip()
        with self._file_lock():
            self._data = self._load()
            devices = self._devices()
            device = devices.get(clean_id)
            if device is None:
                return None
            device.status = DEVICE_REVOKED
            device.updated_at = _now_ms()
            devices[clean_id] = device
            self._save_devices(devices)
            return device

    def update_label(self, device_id: str, label: str) -> DeviceRecord | None:
        clean_id = str(device_id or "").strip()
        with self._file_lock():
            self._data = self._load()
            devices = self._devices()
            device = devices.get(clean_id)
            if device is None:
                return None
            device.label = label
            device.updated_at = _now_ms()
            devices[clean_id] = device
            self._save_devices(devices)
            return device

    def touch(self, device_id: str) -> None:
        clean_id = str(device_id or "").strip()
        if not clean_id:
            return
        with self._file_lock():
            self._data = self._load()
            devices = self._devices()
            device = devices.get(clean_id)
            if device is None or not device.active:
                return
            now = _now_ms()
            if device.last_seen_at and now - int(device.last_seen_at) < _TOUCH_THROTTLE_MS:
                return
            device.last_seen_at = now
            devices[clean_id] = device
            self._save_devices(devices)

    def _devices(self) -> dict[str, DeviceRecord]:
        raw = self._data.setdefault("devices", {})
        if not isinstance(raw, dict):
            raw = {}
            self._data["devices"] = raw
        return {
            key: DeviceRecord.from_dict(value)
            for key, value in raw.items()
            if isinstance(value, dict) and str(value.get("device_id") or key).strip()
        }

    def _load(self) -> dict[str, Any]:
        data = load_json_object(self.path)
        data.setdefault("schema_version", 1)
        data.setdefault("devices", {})
        return data

    def _save_devices(self, devices: dict[str, DeviceRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["schema_version"] = 1
        self._data["updated_at"] = _now_ms()
        self._data["devices"] = {did: d.as_dict() for did, d in devices.items()}
        # Store token_hash separately (as_dict doesn't include it for safety)
        for did, d in devices.items():
            self._data["devices"][did]["token_hash"] = d.token_hash
            self._data["devices"][did]["approval_token_hash"] = d.approval_token_hash
            self._data["devices"][did]["public_key"] = d.public_key
            self._data["devices"][did]["encryption_public_key"] = d.encryption_public_key
            self._data["devices"][did]["profile_id"] = d.profile_id
        save_json_object(self.path, self._data)

    def _file_lock(self) -> AbstractContextManager[None]:
        return file_lock(self.path, lock_name="device store")
