from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .settings import default_store_path


PEER_PENDING = "pending"
PEER_APPROVED = "approved"
PEER_BLOCKED = "blocked"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _peers_file(store_path: Path | None = None) -> Path:
    root = Path(store_path).expanduser() if store_path is not None else default_store_path()
    if root.name == "peers.json":
        return root
    return root / "peers.json"


def generate_shared_secret() -> str:
    return secrets.token_urlsafe(48)


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


@dataclass
class PeerRecord:
    peer_id: str
    fingerprint: str
    hmac_secret: str
    status: str = PEER_PENDING
    capabilities: list[str] = field(default_factory=list)
    allowed_company_ids: list[str] = field(default_factory=list)
    label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=_now_ms)
    updated_at: int = field(default_factory=_now_ms)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PeerRecord":
        return cls(
            peer_id=str(value.get("peer_id") or value.get("id") or ""),
            fingerprint=str(value.get("fingerprint") or ""),
            hmac_secret=str(value.get("hmac_secret") or value.get("shared_secret") or ""),
            status=str(value.get("status") or PEER_PENDING),
            capabilities=_string_list(value.get("capabilities")),
            allowed_company_ids=_string_list(value.get("allowed_company_ids")),
            label=str(value.get("label") or ""),
            metadata=(
                dict(value["metadata"])
                if isinstance(value.get("metadata"), dict)
                else {}
            ),
            created_at=int(value.get("created_at") or _now_ms()),
            updated_at=int(value.get("updated_at") or _now_ms()),
        )

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "fingerprint": self.fingerprint,
            "hmac_secret": "***" if redact and self.hmac_secret else self.hmac_secret,
            "status": self.status,
            "capabilities": list(self.capabilities),
            "allowed_company_ids": list(self.allowed_company_ids),
            "label": self.label,
            "metadata": dict(self.metadata),
            "created_at": int(self.created_at),
            "updated_at": int(self.updated_at),
        }

    @property
    def approved(self) -> bool:
        return self.status == PEER_APPROVED

    @property
    def blocked(self) -> bool:
        return self.status == PEER_BLOCKED


class PeerStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = _peers_file(path)
        self._data = self._load()

    def list_peers(self, *, redact: bool = True) -> list[dict[str, Any]]:
        return [peer.as_dict(redact=redact) for peer in self._peers().values()]

    def get_peer(self, peer_id: str) -> PeerRecord | None:
        return self._peers().get(str(peer_id or "").strip())

    def get_by_fingerprint(self, fingerprint: str) -> PeerRecord | None:
        target = str(fingerprint or "").strip()
        for peer in self._peers().values():
            if peer.fingerprint == target:
                return peer
        return None

    def upsert_peer(
        self,
        peer_id: str,
        *,
        fingerprint: str = "",
        hmac_secret: str = "",
        status: str = PEER_PENDING,
        capabilities: list[str] | None = None,
        allowed_company_ids: list[str] | None = None,
        label: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PeerRecord:
        clean_peer_id = str(peer_id or "").strip()
        if not clean_peer_id:
            raise ValueError("peer_id is required")
        peers = self._peers()
        existing = peers.get(clean_peer_id)
        now = _now_ms()
        peer = PeerRecord(
            peer_id=clean_peer_id,
            fingerprint=str(fingerprint or (existing.fingerprint if existing else "")),
            hmac_secret=str(hmac_secret or (existing.hmac_secret if existing else "") or generate_shared_secret()),
            status=str(status or (existing.status if existing else PEER_PENDING)),
            capabilities=_string_list(capabilities if capabilities is not None else (existing.capabilities if existing else [])),
            allowed_company_ids=_string_list(
                allowed_company_ids if allowed_company_ids is not None else (existing.allowed_company_ids if existing else [])
            ),
            label=str(label or (existing.label if existing else "")),
            metadata=dict(metadata if isinstance(metadata, dict) else (existing.metadata if existing else {})),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        peers[clean_peer_id] = peer
        self._save_peers(peers)
        return peer

    def approve_peer(
        self,
        peer_id: str,
        *,
        fingerprint: str = "",
        hmac_secret: str = "",
        capabilities: list[str] | None = None,
        allowed_company_ids: list[str] | None = None,
        label: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PeerRecord:
        return self.upsert_peer(
            peer_id,
            fingerprint=fingerprint,
            hmac_secret=hmac_secret,
            status=PEER_APPROVED,
            capabilities=capabilities if capabilities is not None else ["message"],
            allowed_company_ids=allowed_company_ids,
            label=label,
            metadata=metadata,
        )

    def block_peer(self, peer_id: str, *, reason: str = "") -> PeerRecord:
        peer = self.get_peer(peer_id)
        metadata = dict(peer.metadata if peer else {})
        if reason:
            metadata["blocked_reason"] = str(reason)
        return self.upsert_peer(
            peer_id,
            fingerprint=peer.fingerprint if peer else "",
            hmac_secret=peer.hmac_secret if peer else "",
            status=PEER_BLOCKED,
            capabilities=peer.capabilities if peer else [],
            allowed_company_ids=peer.allowed_company_ids if peer else [],
            label=peer.label if peer else "",
            metadata=metadata,
        )

    def _peers(self) -> dict[str, PeerRecord]:
        raw = self._data.setdefault("peers", {})
        if not isinstance(raw, dict):
            raw = {}
            self._data["peers"] = raw
        return {
            key: PeerRecord.from_dict(value)
            for key, value in raw.items()
            if isinstance(value, dict) and str(value.get("peer_id") or key).strip()
        }

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema_version", 1)
        data.setdefault("peers", {})
        return data

    def _save_peers(self, peers: dict[str, PeerRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["schema_version"] = 1
        self._data["updated_at"] = _now_ms()
        self._data["peers"] = {peer_id: peer.as_dict(redact=False) for peer_id, peer in peers.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)
