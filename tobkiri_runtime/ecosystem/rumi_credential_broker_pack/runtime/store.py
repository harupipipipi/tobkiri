"""Encrypted credential handle store with caller and operation scope binding."""

from __future__ import annotations

import builtins
import json
import hashlib
import hmac
import os
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

from core_runtime.paths import USER_DATA_DIR
from core_runtime.runtime_locks import NamedLock

STORE_VERSION = "rumi.credential-broker.store.v1"
KEY_VERSION = "rumi.credential-broker.key.v1"


class CredentialBrokerStore:
    """Own encrypted credentials while exposing only opaque public handles."""

    def __init__(self, *, user_data_root: Path | None = None) -> None:
        root = Path(user_data_root or USER_DATA_DIR)
        self.user_data_root = root
        self.root = root / "packs" / "rumi_credential_broker_pack"
        self.path = self.root / "credentials.store.json"
        self.key_path = self.root / ".credential-store.key"
        self.lock_root = self.root / "locks"
        self.backup_root = self.root / "migration_backups"
        self._windows_acl_secured = False

    def create(
        self,
        *,
        secret_material: Mapping[str, Any],
        consumer_pack_id: str,
        provider_instance_id: str,
        scopes: list[str],
        profile_id: str,
        purpose: str = "provider.invoke",
        label: str = "",
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        """Encrypt material and create one non-secret handle record."""
        self._prepare_storage()
        consumer_pack_id = _identifier(consumer_pack_id, "consumer_pack_id")
        provider_instance_id = _identifier(
            provider_instance_id,
            "provider_instance_id",
        )
        profile_id = _identifier(profile_id, "profile_id")
        purpose = _identifier(purpose, "purpose")
        normalized_scopes = _scopes(scopes)
        if not normalized_scopes:
            raise ValueError("at least one credential scope is required")
        if not isinstance(secret_material, Mapping) or not secret_material:
            raise ValueError("secret_material is required")
        handle = f"credential:{uuid.uuid4().hex}"
        with NamedLock(self.lock_root, "credential-broker"):
            state = self._read()
            record = {
                "handle": handle,
                "consumer_pack_id": consumer_pack_id,
                "provider_instance_id": provider_instance_id,
                "profile_id": profile_id,
                "key_version": KEY_VERSION,
                "purpose": purpose,
                "scopes": normalized_scopes,
                "label": str(label)[:160],
                "expires_at": expires_at,
                "created_at": _now(),
                "updated_at": _now(),
                "ciphertext": self._fernet()
                .encrypt(
                    json.dumps(
                        dict(secret_material),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                .decode("ascii"),
            }
            record["record_mac"] = self._record_mac(record)
            state["credentials"][handle] = record
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
        profile_id: str,
        key_version: str = "",
        purpose: str = "provider.invoke",
    ) -> dict[str, Any]:
        """Decrypt only when caller, provider, scope, and expiry all match."""
        self._prepare_storage()
        with NamedLock(self.lock_root, "credential-broker"):
            return self._resolve_unlocked(
                handle,
                consumer_pack_id=consumer_pack_id,
                provider_instance_id=provider_instance_id,
                profile_id=profile_id,
                scope=scope,
                key_version=key_version,
                purpose=purpose,
            )

    def _resolve_unlocked(
        self,
        handle: str,
        *,
        consumer_pack_id: str,
        provider_instance_id: str,
        profile_id: str,
        scope: str,
        key_version: str,
        purpose: str,
    ) -> dict[str, Any]:
        state = self._read()
        record = state["credentials"].get(str(handle))
        if not isinstance(record, dict):
            raise KeyError("credential handle is unknown")
        if not hmac.compare_digest(str(record.get("record_mac") or ""), self._record_mac(record)):
            raise PermissionError("credential record integrity check failed")
        if str(record.get("consumer_pack_id")) != consumer_pack_id:
            raise PermissionError("credential consumer is not bound")
        if str(record.get("provider_instance_id")) != provider_instance_id:
            raise PermissionError("credential provider is not bound")
        if str(record.get("profile_id")) != profile_id:
            raise PermissionError("credential profile is not bound")
        if key_version and str(record.get("key_version")) != key_version:
            raise PermissionError("credential key version is not bound")
        if str(record.get("purpose") or "provider.invoke") != purpose:
            raise PermissionError("credential purpose is not bound")
        if scope not in set(record.get("scopes") or []):
            raise PermissionError("credential scope is denied")
        expires_at = record.get("expires_at")
        if isinstance(expires_at, (int, float)) and float(expires_at) <= time.time():
            raise PermissionError("credential handle expired")
        try:
            plaintext = self._fernet().decrypt(str(record.get("ciphertext") or "").encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("credential material cannot be decrypted") from exc
        material = json.loads(plaintext.decode("utf-8"))
        if not isinstance(material, dict):
            raise RuntimeError("credential material is invalid")
        return material

    def list(self, *, profile_id: str) -> dict[str, Any]:
        """Return redacted status records only."""
        self._prepare_storage()
        profile_id = _identifier(profile_id, "profile_id")
        state = self._read()
        values = [
            self._public(item)
            for item in state["credentials"].values()
            if isinstance(item, dict) and item.get("profile_id") == profile_id
        ]
        values.sort(key=lambda item: item["handle"])
        return {"credentials": values, "count": len(values)}

    def revoke(self, handle: str, *, profile_id: str) -> dict[str, Any]:
        """Delete encrypted material for one exact handle."""
        self._prepare_storage()
        profile_id = _identifier(profile_id, "profile_id")
        with NamedLock(self.lock_root, "credential-broker"):
            state = self._read()
            record = state["credentials"].get(str(handle))
            if not isinstance(record, dict):
                raise KeyError("credential handle is unknown")
            if not hmac.compare_digest(
                str(record.get("record_mac") or ""), self._record_mac(record)
            ):
                raise PermissionError("credential record integrity check failed")
            if record.get("profile_id") != profile_id:
                raise PermissionError("credential profile is not bound")
            del state["credentials"][str(handle)]
            state["revision"] += 1
            self._write(state)
        return {"handle": str(handle), "revoked": True}

    def migrate(
        self,
        records: builtins.list[Mapping[str, Any]],
        *,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Atomically import explicit legacy records into encrypted handles."""
        self._prepare_storage()
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
            handles: builtins.list[str] = []
            for item in records:
                consumer_pack_id = _identifier(item.get("consumer_pack_id"), "consumer_pack_id")
                provider_instance_id = _identifier(
                    item.get("provider_instance_id"), "provider_instance_id"
                )
                profile_id = _identifier(item.get("profile_id"), "profile_id")
                scopes = _scopes([str(value) for value in item.get("scopes", [])])
                material = item.get("secret_material")
                if not scopes or not isinstance(material, Mapping) or not material:
                    raise ValueError("credential migration record is invalid")
                handle = f"credential:{uuid.uuid4().hex}"
                record = {
                    "handle": handle,
                    "consumer_pack_id": consumer_pack_id,
                    "provider_instance_id": provider_instance_id,
                    "profile_id": profile_id,
                    "key_version": KEY_VERSION,
                    "purpose": str(item.get("purpose") or "provider.invoke"),
                    "scopes": scopes,
                    "label": str(item.get("label") or "legacy migration")[:160],
                    "expires_at": item.get("expires_at"),
                    "created_at": _now(),
                    "updated_at": _now(),
                    "ciphertext": self._fernet()
                    .encrypt(
                        json.dumps(
                            dict(material),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                    .decode("ascii"),
                }
                record["record_mac"] = self._record_mac(record)
                state["credentials"][handle] = record
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
                "credentials": [self._public(state["credentials"][item]) for item in handles],
            }

    def rollback_migration(self, migration_id: str) -> dict[str, Any]:
        """Restore the exact encrypted pre-migration owner state."""
        self._prepare_storage()
        with NamedLock(self.lock_root, "credential-broker"):
            state = self._read()
            migration = state.get("migration")
            if not isinstance(migration, Mapping) or migration.get("migration_id") != migration_id:
                raise ValueError("credential migration marker mismatch")
            backup = Path(str(migration.get("backup") or ""))
            try:
                backup.relative_to(self.backup_root)
            except ValueError as exc:
                raise RuntimeError("credential migration backup escapes storage") from exc
            if backup.is_symlink():
                raise RuntimeError("credential migration backup is a symlink")
            backup_path = backup / "pre-migration.store.json"
            restored = json.loads(backup_path.read_text(encoding="utf-8"))
            if not isinstance(restored, dict) or restored.get("version") != STORE_VERSION:
                raise RuntimeError("credential migration backup is invalid")
            self._write_backup(self.root / f"rollback-{migration_id}.json", state)
            self._write(restored)
            return {"migration_id": migration_id, "rolled_back": True}

    def _fernet(self) -> Fernet:
        self._prepare_storage()
        if self.key_path.is_file():
            self._require_owner_file(self.key_path, "credential key")
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

    def _record_mac(self, record: Mapping[str, Any]) -> str:
        """Authenticate handle metadata so profile tampering fails closed."""

        unsigned = {key: value for key, value in record.items() if key != "record_mac"}
        raw = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._fernet()._signing_key, raw, hashlib.sha256).hexdigest()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "version": STORE_VERSION,
                "revision": 0,
                "credentials": {},
                "migration": None,
            }
        self._require_owner_file(self.path, "credential store")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            raise ValueError("credential store version is invalid")
        if not isinstance(payload.get("credentials"), dict):
            raise ValueError("credential store records are invalid")
        return payload

    def _write(self, state: Mapping[str, Any]) -> None:
        self._prepare_storage()
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
            "profile_id": record.get("profile_id"),
            "key_version": record.get("key_version", KEY_VERSION),
            "purpose": record.get("purpose", "provider.invoke"),
            "scopes": list(record.get("scopes") or []),
            "label": record.get("label"),
            "expires_at": record.get("expires_at"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "configured": True,
        }

    def _prepare_storage(self) -> None:
        """Create owner storage while rejecting every mutable symlink edge."""

        for path in (self.user_data_root, self.user_data_root / "packs", self.root):
            if path.is_symlink():
                raise PermissionError(f"credential storage path is a symlink: {path}")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise PermissionError("credential storage root is not a directory")
        if os.name == "nt":
            if not self._windows_acl_secured:
                _secure_windows_directory(self.root)
                self._windows_acl_secured = True
        else:
            user_metadata = self.user_data_root.stat()
            getuid = getattr(os, "geteuid", None)
            if user_metadata.st_mode & 0o022 or (
                callable(getuid) and user_metadata.st_uid != getuid()
            ):
                raise PermissionError("credential user-data root permissions are unsafe")
            os.chmod(self.root, 0o700)
        for path in (self.path, self.key_path, self.lock_root, self.backup_root):
            if path.is_symlink():
                raise PermissionError(f"credential storage entry is a symlink: {path}")

    @staticmethod
    def _require_owner_file(path: Path, label: str) -> None:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise PermissionError(f"{label} permissions are unsafe")
        if os.name == "nt":
            return
        if metadata.st_mode & 0o077:
            raise PermissionError(f"{label} permissions are unsafe")
        getuid = getattr(os, "geteuid", None)
        if callable(getuid) and metadata.st_uid != getuid():
            raise PermissionError(f"{label} owner is unsafe")


def _secure_windows_directory(path: Path) -> None:
    """Replace inherited ACLs with one verified current-user SID grant."""

    # Keep the target path out of the PowerShell command text. Windows PowerShell
    # treats tokens after ``-Command`` as part of the command invocation rather
    # than as a stable argv contract, so relying on ``$args[0]`` is not portable
    # across the hosted Windows runner and desktop hosts. stdin is an inert data
    # channel and therefore also avoids quoting or command-injection ambiguity.
    script = r"""
$ErrorActionPreference = 'Stop'
$target = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($target)) { throw 'credential ACL target missing' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User
$acl = [System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$propagation = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
  $sid, $fullControl, $inheritance, $propagation, $allow
)
[void]$acl.AddAccessRule($rule)
$directory = [System.IO.DirectoryInfo]::new($target)
$directory.SetAccessControl($acl)
$sections = [System.Security.AccessControl.AccessControlSections]::Access -bor
  [System.Security.AccessControl.AccessControlSections]::Owner
$verified = $directory.GetAccessControl($sections)
if (-not $verified.AreAccessRulesProtected) { throw 'credential ACL inherits' }
$rules = @($verified.GetAccessRules(
  $true, $false, [System.Security.Principal.SecurityIdentifier]
))
if ($rules.Count -ne 1) { throw 'credential ACL has extra principals' }
$ownerSid = $verified.GetOwner(
  [System.Security.Principal.SecurityIdentifier]
).Value
if ($ownerSid -ne $sid.Value) { throw 'credential ACL owner changed' }
$actual = $rules[0].IdentityReference.Value
if ($actual -ne $sid.Value) { throw 'credential ACL owner grant changed' }
if ($rules[0].AccessControlType -ne $allow) {
  throw 'credential ACL is not an allow grant'
}
if ($rules[0].FileSystemRights -ne $fullControl) {
  throw 'credential ACL does not grant full control'
}
if ($rules[0].InheritanceFlags -ne $inheritance -or
    $rules[0].PropagationFlags -ne $propagation) {
  throw 'credential ACL propagation changed'
}
"""
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            input=str(path),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionError("credential Windows ACL could not be secured") from exc


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(item in normalized for item in ("\x00", "\r", "\n"))
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _scopes(values: list[str]) -> list[str]:
    return sorted({_identifier(value, "scope") for value in values if str(value or "").strip()})


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
