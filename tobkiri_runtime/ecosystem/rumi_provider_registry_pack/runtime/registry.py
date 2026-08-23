"""Atomic provider-instance registry with conservative health semantics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

STORE_VERSION = "rumi.provider-registry.store.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ProviderRegistryConflict(RuntimeError):
    """Raised when a mutation uses a stale registry revision."""


class ProviderRegistry:
    """Own configured provider instances, not catalogs or adapter code."""

    def __init__(
        self,
        profile_id: str,
        *,
        user_data_root: Path | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        root = Path(user_data_root or USER_DATA_DIR)
        self.root = (
            root
            / "packs"
            / "rumi_provider_registry_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "provider_registry.store.json"
        self.backup_root = self.root / "migration_backups"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return redacted deterministic provider instance state."""
        state = self._read()
        return {
            "version": state["version"],
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "providers": [
                dict(state["providers"][key])
                for key in sorted(state["providers"])
            ],
        }

    def save(
        self,
        record: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Save one provider-neutral connection record atomically."""
        normalized = _provider_record(record)
        with NamedLock(self.lock_root, "provider-registry"):
            state = self._read()
            self._assert_revision(state, expected_revision)
            key = normalized["provider_instance_id"]
            current = state["providers"].get(key, {})
            now = _now()
            normalized["created_at"] = str(current.get("created_at") or now)
            normalized["updated_at"] = now
            normalized["record_revision"] = int(
                current.get("record_revision") or 0
            ) + 1
            state["providers"][key] = normalized
            state["revision"] += 1
            self._write(state)
            return {
                "action": "saved",
                "provider": dict(normalized),
                "store_revision": state["revision"],
            }

    def delete(
        self,
        provider_instance_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Delete one provider connection at an expected revision."""
        key = _identifier(provider_instance_id)
        with NamedLock(self.lock_root, "provider-registry"):
            state = self._read()
            self._assert_revision(state, expected_revision)
            if key not in state["providers"]:
                raise KeyError("provider instance is unknown")
            del state["providers"][key]
            state["revision"] += 1
            self._write(state)
            return {
                "action": "deleted",
                "provider_instance_id": key,
                "store_revision": state["revision"],
            }

    def health(self) -> dict[str, Any]:
        """Report unknown until an adapter supplies fresh verified evidence."""
        providers = []
        for item in self.snapshot()["providers"]:
            evidence = item.get("health_evidence")
            evidence = evidence if isinstance(evidence, Mapping) else {}
            verified = bool(evidence.get("verified"))
            status = str(evidence.get("status") or "unknown")
            if not verified or status not in {
                "available",
                "invalid",
                "limited",
                "unavailable",
            }:
                status = "unknown"
            observed_at = (
                float(evidence["observed_at"])
                if verified and evidence.get("observed_at") is not None
                else None
            )
            providers.append(
                {
                    "provider_instance_id": item["provider_instance_id"],
                    "status": status,
                    "observed_at": observed_at,
                    "verified": verified,
                    "freshness": "fresh" if observed_at is not None else "unknown",
                    "reason_code": status,
                }
            )
        return {"providers": providers}

    def migrate(
        self,
        providers: list[Mapping[str, Any]],
        *,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Migrate deterministic records once and retain an owner-only backup."""
        normalized = sorted(
            (_provider_record(item) for item in providers),
            key=lambda item: item["provider_instance_id"],
        )
        if _hash({"providers": normalized}) != expected_source_hash:
            raise ProviderRegistryConflict("provider migration source changed")
        with NamedLock(self.lock_root, "provider-registry"):
            if self.path.is_file():
                raise RuntimeError("provider registry is already initialized")
            migration_id = f"migration-{uuid.uuid4().hex}"
            backup = self.backup_root / migration_id
            backup.mkdir(parents=True, exist_ok=False)
            os.chmod(backup, 0o700)
            _atomic_json(backup / "legacy-provider-registry.json", {
                "providers": normalized,
            })
            state = self._empty()
            state["providers"] = {
                item["provider_instance_id"]: item for item in normalized
            }
            state["revision"] = 1
            state["migration"] = {
                "migration_id": migration_id,
                "source_hash": expected_source_hash,
                "backup": str(backup),
                "migrated_at": _now(),
            }
            self._write(state)
            return {
                "migration_id": migration_id,
                "providers": len(normalized),
                "source_hash": expected_source_hash,
            }

    def rollback_migration(self, migration_id: str) -> dict[str, Any]:
        """Remove migrated state while preserving backup and rollback evidence."""
        with NamedLock(self.lock_root, "provider-registry"):
            state = self._read()
            migration = state.get("migration")
            if not isinstance(migration, dict) or migration.get(
                "migration_id"
            ) != migration_id:
                raise ValueError("provider registry migration marker mismatch")
            _atomic_json(self.root / f"rollback-{migration_id}.json", state)
            self.path.unlink(missing_ok=True)
            return {"migration_id": migration_id, "rolled_back": True}

    @staticmethod
    def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
        if int(state.get("revision") or 0) != int(expected):
            raise ProviderRegistryConflict("provider registry revision is stale")

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != STORE_VERSION:
            raise ValueError("provider registry store version is invalid")
        if value.get("profile_id") != self.profile_id:
            raise ValueError("provider registry profile binding is invalid")
        if not isinstance(value.get("providers"), dict):
            raise ValueError("provider registry store is invalid")
        for record in value["providers"].values():
            if not isinstance(record, Mapping):
                raise ValueError("provider registry record is invalid")
            _provider_endpoint(record.get("endpoint"), record.get("credential_handle"))
        return value

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)

    def _empty(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "profile_id": self.profile_id,
            "revision": 0,
            "providers": {},
            "migration": None,
        }


def _provider_record(value: Mapping[str, Any]) -> dict[str, Any]:
    provider_instance_id = _identifier(value.get("provider_instance_id"))
    adapter_id = _identifier(value.get("adapter_id"))
    credential_handle = value.get("credential_handle")
    if credential_handle is not None and not str(credential_handle).startswith(
        ("credential:", "opaque:")
    ):
        raise ValueError("provider credential must be an opaque handle")
    endpoint_text = _provider_endpoint(value.get("endpoint"), credential_handle)

    health = value.get("health_evidence")
    health = health if isinstance(health, Mapping) else {}
    return {
        "provider_instance_id": provider_instance_id,
        "adapter_id": adapter_id,
        "display_name": str(value.get("display_name") or provider_instance_id)[:200],
        "credential_handle": credential_handle,
        "endpoint": endpoint_text,
        "enabled": bool(value.get("enabled", True)),
        "data_residency": str(value.get("data_residency") or "unknown")[:100],
        "health_evidence": {
            "status": str(health.get("status") or "unknown"),
            "observed_at": health.get("observed_at"),
            "verified": bool(health.get("verified", False)),
        },
        "metadata": _safe_metadata(value.get("metadata")),
    }


def _provider_endpoint(endpoint: Any, credential_handle: Any) -> str | None:
    """Validate one provider endpoint, requiring TLS whenever credentials exist."""
    endpoint_text = str(endpoint) if endpoint is not None else None
    if endpoint_text is not None:
        parsed_endpoint = urllib.parse.urlsplit(endpoint_text)
        if (
            parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.hostname
            or parsed_endpoint.username is not None
            or parsed_endpoint.password is not None
        ):
            raise ValueError("provider endpoint must be a canonical HTTP(S) URL")
        if credential_handle is not None and parsed_endpoint.scheme != "https":
            raise ValueError("credentialed provider endpoint requires HTTPS")
    return endpoint_text


def _safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    blocked = {"secret", "token", "password", "api_key", "authorization"}
    return {
        str(key): item
        for key, item in value.items()
        if not any(marker in str(key).lower() for marker in blocked)
        and isinstance(item, (str, int, float, bool, type(None)))
    }


def _identifier(value: Any) -> str:
    result = str(value or "").strip()
    if _SAFE_ID.fullmatch(result) is None:
        raise ValueError("provider registry identifier is invalid")
    return result


def _hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
