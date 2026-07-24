"""Encrypted credential handle store with caller and operation scope binding."""

from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

from core_runtime.paths import USER_DATA_DIR
from core_runtime.runtime_locks import NamedLock

STORE_VERSION = "rumi.credential-broker.store.v1"


class CredentialBrokerStore:
    """Own encrypted credentials while exposing only opaque public handles."""

    def __init__(self, *, user_data_root: Path | None = None) -> None:
        root = Path(user_data_root or USER_DATA_DIR)
        self.root = root / "packs" / "rumi_credential_broker_pack"
        self.path = self.root / "credentials.store.json"
        self.key_path = self.root / ".credential-store.key"
        self.lock_root = self.root / "locks"
        self.backup_root = self.root / "migration_backups"

    def create(
        self,
        *,
        secret_material: Mapping[str, Any],
        consumer_pack_id: str,
        provider_instance_id: str,
        scopes: list[str],
        label: str = "",
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        """Encrypt material and create one non-secret handle record."""
        consumer_pack_id = _identifier(consumer_pack_id, "consumer_pack_id")
        provider_instance_id = _identifier(
            provider_instance_id,
            "provider_instance_id",
        )
        normalized_scopes = _scopes(scopes)
        if not normalized_scopes:
            raise ValueError("at least one credential scope is required")
        if not isinstance(secret_material, Mapping) or not secret_material:
            raise ValueError("secret_material is required")
        handle = f"credential:{uuid.uuid4().hex}"
        with NamedLock(self.lock_root, "credential-broker"):
            state = self._read()
            state["credentials"][handle] = {
                "handle": handle,
                "consumer_pack_id": consumer_pack_id,
                "provider_instance_id": provider_instance_id,
                "scopes": normalized_scopes,
                "label": str(label)[:160],
                "expires_at": expires_at,
                "created_at": _now(),
                "updated_at": _now(),
                "ciphertext": self._fernet().encrypt(
                    json.dumps(
                        dict(secret_material),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).decode("ascii"),
            }
            state["revision"] += 1
            self._write(state)
        return self._public(state["credentials"][handle])

    def resolve(
        self,
        handle: str,
        *,
        consumer_pack_id: str,
        provider_instance_id: str,
        scope: str,
    ) -> dict[str, Any]:
        """Decrypt only when caller, provider, scope, and expiry all match."""
        with NamedLock(self.lock_root, "credential-broker"):
            return self._resolve_unlocked(
                handle,
                consumer_pack_id=consumer_pack_id,
                provider_instance_id=provider_instance_id,
                scope=scope,
            )

    def _resolve_unlocked(
        self,
        handle: str,
        *,
        consumer_pack_id: str,
        provider_instance_id: str,
        scope: str,
    ) -> dict[str, Any]:
        state = self._read()
        record = state["credentials"].get(str(handle))
        if not isinstance(record, dict):
            raise KeyError("credential handle is unknown")
        if str(record.get("consumer_pack_id")) != consumer_pack_id:
            raise PermissionError("credential consumer is not bound")
        if str(record.get("provider_instance_id")) != provider_instance_id:
            raise PermissionError("credential provider is not bound")
        if scope not in set(record.get("scopes") or []):
            raise PermissionError("credential scope is denied")
        expires_at = record.get("expires_at")
        if isinstance(expires_at, (int, float)) and float(expires_at) <= time.time():
            raise PermissionError("credential handle expired")
        try:
            plaintext = self._fernet().decrypt(
                str(record.get("ciphertext") or "").encode("ascii")
            )
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("credential material cannot be decrypted") from exc
        material = json.loads(plaintext.decode("utf-8"))
        if not isinstance(material, dict):
            raise RuntimeError("credential material is invalid")
        return material

    def list(self) -> dict[str, Any]:
        """Return redacted status records only."""
        state = self._read()
        values = [
            self._public(item)
            for item in state["credentials"].values()
            if isinstance(item, dict)
        ]
        values.sort(key=lambda item: item["handle"])
        return {"credentials": values, "count": len(values)}

    def revoke(self, handle: str) -> dict[str, Any]:
        """Delete encrypted material for one exact handle."""
        with NamedLock(self.lock_root, "credential-broker"):
            state = self._read()
            if str(handle) not in state["credentials"]:
                raise KeyError("credential handle is unknown")
            del state["credentials"][str(handle)]
            state["revision"] += 1
            self._write(state)
        return {"handle": str(handle), "revoked": True}

    def migrate(
        self,
        records: list[Mapping[str, Any]],
        *,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Atomically import explicit legacy records into encrypted handles."""
        source = {"records": [dict(item) for item in records]}
        if _hash(source) != expected_source_hash:
            raise ValueError("credential migration source changed")
        with NamedLock(self.lock_root, "credential-broker"):
            state = self._read()
            if state.get("migration") is not None:
                raise RuntimeError("credential migration is already applied")
            migration_id = f"migration-{uuid.uuid4().hex}"
            backup = self.backup_root / migration_id
            backup.mkdir(parents=True, exist_ok=False)
            os.chmod(backup, 0o700)
            self._write_backup(backup / "pre-migration.store.json", state)
            handles: list[str] = []
            for item in records:
                consumer_pack_id = _identifier(
                    item.get("consumer_pack_id"), "consumer_pack_id"
                )
                provider_instance_id = _identifier(
                    item.get("provider_instance_id"), "provider_instance_id"
                )
                scopes = _scopes([str(value) for value in item.get("scopes", [])])
                material = item.get("secret_material")
                if not scopes or not isinstance(material, Mapping) or not material:
                    raise ValueError("credential migration record is invalid")
                handle = f"credential:{uuid.uuid4().hex}"
                state["credentials"][handle] = {
                    "handle": handle,
                    "consumer_pack_id": consumer_pack_id,
                    "provider_instance_id": provider_instance_id,
                    "scopes": scopes,
                    "label": str(item.get("label") or "legacy migration")[:160],
                    "expires_at": item.get("expires_at"),
                    "created_at": _now(),
                    "updated_at": _now(),
                    "ciphertext": self._fernet().encrypt(
                        json.dumps(
                            dict(material),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).decode("ascii"),
                }
                handles.append(handle)
            state["revision"] += 1
            state["migration"] = {
                "migration_id": migration_id,
                "source_hash": expected_source_hash,
                "backup": str(backup),
                "handles": handles,
                "migrated_at": _now(),
            }
            self._write(state)
            return {
                "migration_id": migration_id,
                "source_hash": expected_source_hash,
                "credentials": [
                    self._public(state["credentials"][item]) for item in handles
                ],
            }

    def rollback_migration(self, migration_id: str) -> dict[str, Any]:
        """Restore the exact encrypted pre-migration owner state."""
        with NamedLock(self.lock_root, "credential-broker"):
            state = self._read()
            migration = state.get("migration")
            if not isinstance(migration, Mapping) or migration.get(
                "migration_id"
            ) != migration_id:
                raise ValueError("credential migration marker mismatch")
            backup = Path(str(migration.get("backup") or ""))
            backup_path = backup / "pre-migration.store.json"
            restored = json.loads(backup_path.read_text(encoding="utf-8"))
            if (
                not isinstance(restored, dict)
                or restored.get("version") != STORE_VERSION
            ):
                raise RuntimeError("credential migration backup is invalid")
            self._write_backup(self.root / f"rollback-{migration_id}.json", state)
            self._write(restored)
            return {"migration_id": migration_id, "rolled_back": True}

    def _fernet(self) -> Fernet:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        if self.key_path.is_file():
            key = self.key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            temporary = self.key_path.with_suffix(".tmp")
            temporary.write_bytes(key)
            os.chmod(temporary, 0o600)
            try:
                temporary.replace(self.key_path)
            except OSError:
                temporary.unlink(missing_ok=True)
                key = self.key_path.read_bytes().strip()
        os.chmod(self.key_path, 0o600)
        return Fernet(key)

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "version": STORE_VERSION,
                "revision": 0,
                "credentials": {},
                "migration": None,
            }
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            raise ValueError("credential store version is invalid")
        if not isinstance(payload.get("credentials"), dict):
            raise ValueError("credential store records are invalid")
        return payload

    def _write(self, state: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        temporary = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @staticmethod
    def _write_backup(path: Path, state: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    @staticmethod
    def _public(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "handle": record.get("handle"),
            "consumer_pack_id": record.get("consumer_pack_id"),
            "provider_instance_id": record.get("provider_instance_id"),
            "scopes": list(record.get("scopes") or []),
            "label": record.get("label"),
            "expires_at": record.get("expires_at"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "configured": True,
        }


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 200 or any(
        item in normalized for item in ("\x00", "\r", "\n")
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _scopes(values: list[str]) -> list[str]:
    return sorted(
        {
            _identifier(value, "scope")
            for value in values
            if str(value or "").strip()
        }
    )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()

