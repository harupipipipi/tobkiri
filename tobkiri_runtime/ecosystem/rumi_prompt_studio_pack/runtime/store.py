"""Atomic authoritative Prompt Studio store with versioned rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import (
    ProfileWorkspaceManager,
    validate_profile_id,
)
from core_runtime.runtime_locks import NamedLock

STORE_VERSION = "rumi.prompt-studio.store.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_ABSENT_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()


class PromptWriteConflict(RuntimeError):
    """Raised when an authoring mutation uses a stale body revision."""


@dataclass(frozen=True)
class MigrationInspection:
    """Read-only migration inventory produced before owner switching."""

    profile_id: str
    source_files: tuple[str, ...]
    prompt_ids: tuple[str, ...]
    source_hash: str
    target_exists: bool
    owner_marker_exists: bool


class PromptStudioStore:
    """Own prompt authoring state for one profile in one atomic document."""

    def __init__(
        self,
        profile_id: str,
        *,
        user_data_root: Path | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        root = Path(user_data_root or USER_DATA_DIR)
        self.user_data_root = root
        self.root = (
            root
            / "packs"
            / "rumi_prompt_studio_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "prompt_studio.store.json"
        self.owner_marker = self.root / "authoritative-owner.json"
        self.backup_root = self.root / "migration_backups"
        self.lock_root = self.root / "locks"
        self._lock_name = f"prompt-studio:{self.profile_id}"

    def legacy_profile_root(self) -> Path:
        """Return the only legacy profile root accepted for migration."""
        return ProfileWorkspaceManager(
            self.user_data_root
        ).paths_for_profile(self.profile_id).root

    def snapshot(self) -> dict[str, Any]:
        """Return the current store without exposing version bodies by default."""
        state = self._read()
        return {
            "version": state["version"],
            "profile_id": state["profile_id"],
            "revision": state["revision"],
            "edge_states": dict(state["edge_states"]),
            "prompts": [
                self._public_prompt(item)
                for item in sorted(
                    state["prompts"].values(),
                    key=lambda value: value["prompt_id"],
                )
            ],
        }

    def get(self, prompt_id: str) -> dict[str, Any] | None:
        """Return one current prompt record."""
        prompt_id = _prompt_id(prompt_id)
        record = self._read()["prompts"].get(prompt_id)
        return dict(record) if isinstance(record, dict) else None

    def set_edge_state(self, edge_id: str, enabled: bool) -> dict[str, Any]:
        """Atomically persist one composition edge state without lost updates."""
        edge_id = _edge_id(edge_id)
        with NamedLock(self.lock_root, self._lock_name):
            state = self._read()
            state["edge_states"][edge_id] = bool(enabled)
            state["revision"] += 1
            state["updated_at"] = _now()
            self._write(state)
            return {
                "action": "edge_state_saved",
                "profile_id": self.profile_id,
                "edge_id": edge_id,
                "enabled": bool(enabled),
                "store_revision": state["revision"],
            }

    def save(
        self,
        prompt_id: str,
        body: str,
        *,
        expected_body_hash: str,
        description: str = "",
        variables: list[str] | None = None,
        enabled: bool = True,
        reason: str = "manual_save",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically save prompt and version record with optimistic concurrency."""
        prompt_id = _prompt_id(prompt_id)
        body = str(body)
        with NamedLock(self.lock_root, self._lock_name):
            state = self._read()
            current = state["prompts"].get(prompt_id)
            previous_body = str((current or {}).get("body") or "")
            previous_exists = current is not None
            self._assert_revision(
                expected_body_hash,
                previous_body,
                previous_exists,
            )
            now = _now()
            version = _version_record(
                prompt_id,
                previous_body,
                body,
                previous_exists=previous_exists,
                reason=reason,
                metadata=metadata,
            )
            record = {
                "prompt_id": prompt_id,
                "body": body,
                "body_hash": _body_hash(body),
                "description": str(description),
                "variables": _string_list(variables),
                "enabled": bool(enabled),
                "created_at": str((current or {}).get("created_at") or now),
                "updated_at": now,
                "revision": int((current or {}).get("revision") or 0) + 1,
                "metadata": _safe_metadata(metadata),
                "versions": [
                    *list((current or {}).get("versions") or []),
                    version,
                ],
            }
            state["prompts"][prompt_id] = record
            state["revision"] += 1
            state["updated_at"] = now
            self._write(state)
            return {
                "action": "saved",
                "profile_id": self.profile_id,
                "prompt": self._public_prompt(record),
                "version": _public_version(version),
                "store_revision": state["revision"],
            }

    def delete(
        self,
        prompt_id: str,
        *,
        expected_body_hash: str,
    ) -> dict[str, Any]:
        """Delete an authored prompt only when its body revision matches."""
        prompt_id = _prompt_id(prompt_id)
        with NamedLock(self.lock_root, self._lock_name):
            state = self._read()
            current = state["prompts"].get(prompt_id)
            if current is None:
                raise KeyError(f"prompt not found: {prompt_id}")
            self._assert_revision(
                expected_body_hash,
                str(current.get("body") or ""),
                True,
            )
            del state["prompts"][prompt_id]
            state["revision"] += 1
            state["updated_at"] = _now()
            self._write(state)
            return {
                "action": "deleted",
                "profile_id": self.profile_id,
                "prompt_id": prompt_id,
                "store_revision": state["revision"],
            }

    def versions(self, prompt_id: str) -> dict[str, Any]:
        """List redacted version metadata for a prompt."""
        prompt_id = _prompt_id(prompt_id)
        prompt = self.get(prompt_id)
        versions = list((prompt or {}).get("versions") or [])
        return {
            "profile_id": self.profile_id,
            "prompt_id": prompt_id,
            "versions": [_public_version(item) for item in reversed(versions)],
            "count": len(versions),
        }

    def rollback(
        self,
        prompt_id: str,
        version_id: str,
        *,
        expected_body_hash: str,
        use_previous: bool = True,
    ) -> dict[str, Any]:
        """Atomically rollback, including first-write deletion compensation."""
        prompt_id = _prompt_id(prompt_id)
        version_id = _version_id(version_id)
        with NamedLock(self.lock_root, self._lock_name):
            state = self._read()
            current = state["prompts"].get(prompt_id)
            if current is None:
                raise KeyError(f"prompt not found: {prompt_id}")
            self._assert_revision(
                expected_body_hash,
                str(current.get("body") or ""),
                True,
            )
            source = next(
                (
                    item
                    for item in current.get("versions") or []
                    if item.get("version_id") == version_id
                ),
                None,
            )
            if source is None:
                raise KeyError(f"version not found: {version_id}")
            if use_previous and source.get("previous_exists") is False:
                del state["prompts"][prompt_id]
                state["revision"] += 1
                state["updated_at"] = _now()
                self._write(state)
                return {
                    "action": "rolled_back",
                    "profile_id": self.profile_id,
                    "prompt_id": prompt_id,
                    "removed_override": True,
                    "body_hash": _ABSENT_HASH,
                    "store_revision": state["revision"],
                }
            next_body = str(
                source.get("previous_body")
                if use_previous
                else source.get("next_body")
                or ""
            )
            audit = _version_record(
                prompt_id,
                str(current.get("body") or ""),
                next_body,
                previous_exists=True,
                reason=f"rollback:{version_id}",
                metadata={"rolled_back_from": version_id},
            )
            current = {
                **current,
                "body": next_body,
                "body_hash": _body_hash(next_body),
                "updated_at": _now(),
                "revision": int(current.get("revision") or 0) + 1,
                "versions": [*list(current.get("versions") or []), audit],
            }
            state["prompts"][prompt_id] = current
            state["revision"] += 1
            state["updated_at"] = _now()
            self._write(state)
            return {
                "action": "rolled_back",
                "profile_id": self.profile_id,
                "prompt": self._public_prompt(current),
                "version": _public_version(audit),
                "store_revision": state["revision"],
            }

    def inspect_migration(
        self,
        legacy_profile_root: Path | None = None,
    ) -> MigrationInspection:
        """Inspect legacy prompt overrides without mutating either owner."""
        root = Path(legacy_profile_root or self.legacy_profile_root()).resolve()
        expected = self.legacy_profile_root().resolve()
        if root != expected:
            raise PermissionError("legacy profile root is outside the bound profile")
        prompts_root = root / "prompts"
        sources = sorted(
            path for path in prompts_root.glob("*")
            if path.is_file() and path.suffix in {".md", ".txt"}
        ) if prompts_root.is_dir() else []
        inventory = [
            (path.name, _body_hash(path.read_text(encoding="utf-8")))
            for path in sources
        ]
        return MigrationInspection(
            profile_id=self.profile_id,
            source_files=tuple(str(path) for path in sources),
            prompt_ids=tuple(_prompt_id_from_path(path) for path in sources),
            source_hash=_body_hash(json.dumps(inventory, sort_keys=True)),
            target_exists=self.path.is_file(),
            owner_marker_exists=self.owner_marker.is_file(),
        )

    def migrate_from_legacy(
        self,
        legacy_profile_root: Path | None = None,
        *,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Backup, transform, verify, then atomically switch authoritative owner."""
        inspection = self.inspect_migration(legacy_profile_root)
        if inspection.source_hash != expected_source_hash:
            raise PromptWriteConflict("legacy source changed after migration inspect")
        if inspection.owner_marker_exists:
            raise RuntimeError("Prompt Studio owner is already switched")
        if inspection.target_exists:
            raise RuntimeError("Prompt Studio target store already exists")
        with NamedLock(self.lock_root, self._lock_name):
            inspection = self.inspect_migration(legacy_profile_root)
            if inspection.source_hash != expected_source_hash:
                raise PromptWriteConflict("legacy source changed during migration")
            migration_id = f"migration-{uuid.uuid4().hex}"
            backup = self.backup_root / migration_id
            backup.mkdir(parents=True, exist_ok=False)
            os.chmod(backup, 0o700)
            for source in inspection.source_files:
                path = Path(source)
                backup_path = backup / path.name
                shutil.copy2(path, backup_path)
                os.chmod(backup_path, 0o600)
            state = self._empty()
            for source in inspection.source_files:
                path = Path(source)
                prompt_id = _prompt_id_from_path(path)
                body = path.read_text(encoding="utf-8")
                state["prompts"][prompt_id] = {
                    "prompt_id": prompt_id,
                    "body": body,
                    "body_hash": _body_hash(body),
                    "description": "",
                    "variables": [],
                    "enabled": True,
                    "created_at": _now(),
                    "updated_at": _now(),
                    "revision": 1,
                    "metadata": {
                        "migration_id": migration_id,
                        "legacy_source_hash": _body_hash(body),
                    },
                    "versions": [],
                }
            state["revision"] = 1
            state["updated_at"] = _now()
            self._write(state)
            verified = self._read()
            if set(verified["prompts"]) != set(inspection.prompt_ids):
                self.path.unlink(missing_ok=True)
                raise RuntimeError("Prompt Studio migration verification failed")
            marker = {
                "owner": "rumi_prompt_studio_pack",
                "profile_id": self.profile_id,
                "store_version": STORE_VERSION,
                "migration_id": migration_id,
                "legacy_source_hash": inspection.source_hash,
                "backup": str(backup),
                "switched_at": _now(),
            }
            _atomic_json(self.owner_marker, marker)
            return marker

    def migrate_records(
        self,
        records: list[Mapping[str, Any]],
        *,
        edge_states: Mapping[str, Any] | None = None,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Import adapter-supplied legacy records without reading a sibling pack."""
        normalized = _migration_records(records)
        normalized_edge_states = {
            _edge_id(key): bool(value)
            for key, value in (edge_states or {}).items()
        }
        source_payload = {
            "records": normalized,
            "edge_states": {
                key: normalized_edge_states[key]
                for key in sorted(normalized_edge_states)
            },
        }
        source_hash = _body_hash(
            json.dumps(source_payload, ensure_ascii=False, sort_keys=True)
        )
        if source_hash != expected_source_hash:
            raise PromptWriteConflict("legacy records changed after migration inspect")
        with NamedLock(self.lock_root, self._lock_name):
            if self.owner_marker.is_file() or self.path.is_file():
                raise RuntimeError("Prompt Studio target is already initialized")
            migration_id = f"migration-{uuid.uuid4().hex}"
            backup = self.backup_root / migration_id
            backup.mkdir(parents=True, exist_ok=False)
            os.chmod(backup, 0o700)
            _atomic_json(backup / "legacy-records.json", source_payload)
            state = self._empty()
            state["edge_states"] = dict(source_payload["edge_states"])
            for item in normalized:
                prompt_id = item["prompt_id"]
                body = item["body"]
                now = _now()
                state["prompts"][prompt_id] = {
                    "prompt_id": prompt_id,
                    "body": body,
                    "body_hash": _body_hash(body),
                    "description": item["description"],
                    "variables": item["variables"],
                    "enabled": item["enabled"],
                    "created_at": now,
                    "updated_at": now,
                    "revision": 1,
                    "metadata": {
                        "migration_id": migration_id,
                        "legacy_source": item["source"],
                    },
                    "versions": [],
                }
            state["revision"] = 1
            state["updated_at"] = _now()
            self._write(state)
            if set(self._read()["prompts"]) != {
                item["prompt_id"] for item in normalized
            }:
                self.path.unlink(missing_ok=True)
                raise RuntimeError("Prompt Studio migration verification failed")
            marker = {
                "owner": "rumi_prompt_studio_pack",
                "profile_id": self.profile_id,
                "store_version": STORE_VERSION,
                "migration_id": migration_id,
                "legacy_source_hash": source_hash,
                "backup": str(backup),
                "switched_at": _now(),
            }
            _atomic_json(self.owner_marker, marker)
            return marker

    def rollback_migration(self, migration_id: str) -> dict[str, Any]:
        """Remove the new owner state; legacy files were never modified."""
        marker = _read_json(self.owner_marker)
        if marker.get("migration_id") != migration_id:
            raise ValueError("migration marker mismatch")
        with NamedLock(self.lock_root, self._lock_name):
            rollback_snapshot = self.root / f"rollback-{migration_id}.json"
            if self.path.is_file():
                shutil.copy2(self.path, rollback_snapshot)
                os.chmod(rollback_snapshot, 0o600)
                self.path.unlink()
            self.owner_marker.unlink(missing_ok=True)
            return {
                "action": "migration_rolled_back",
                "profile_id": self.profile_id,
                "migration_id": migration_id,
                "new_store_snapshot": str(rollback_snapshot),
            }

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        payload = _read_json(self.path)
        if payload.get("version") != STORE_VERSION:
            raise ValueError("unsupported Prompt Studio store version")
        if payload.get("profile_id") != self.profile_id:
            raise ValueError("Prompt Studio store profile mismatch")
        if not isinstance(payload.get("prompts"), dict):
            raise ValueError("Prompt Studio prompts must be an object")
        if "edge_states" not in payload:
            payload["edge_states"] = {}
        if not isinstance(payload.get("edge_states"), dict):
            raise ValueError("Prompt Studio edge states must be an object")
        return payload

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)

    def _empty(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "profile_id": self.profile_id,
            "revision": 0,
            "updated_at": None,
            "prompts": {},
            "edge_states": {},
        }

    @staticmethod
    def _assert_revision(
        expected: str,
        current_body: str,
        current_exists: bool,
    ) -> None:
        expected = str(expected or "").strip()
        current = _body_hash(current_body) if current_exists else _ABSENT_HASH
        if not expected:
            raise PromptWriteConflict("expected_body_hash is required")
        if expected != current:
            raise PromptWriteConflict(
                f"stale prompt revision: expected {expected}; current {current}"
            )

    @staticmethod
    def _public_prompt(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key != "versions"
        } | {"version_count": len(record.get("versions") or [])}


def _version_record(
    prompt_id: str,
    previous_body: str,
    next_body: str,
    *,
    previous_exists: bool,
    reason: str,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "version_id": f"v-{time.time_ns()}-{uuid.uuid4().hex[:8]}",
        "prompt_id": prompt_id,
        "created_at": _now(),
        "previous_exists": previous_exists,
        "previous_body": previous_body,
        "next_body": next_body,
        "previous_hash": _body_hash(previous_body),
        "next_hash": _body_hash(next_body),
        "reason": str(reason),
        "metadata": _safe_metadata(metadata),
    }


def _migration_records(
    records: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, Mapping):
            raise ValueError("migration record must be an object")
        prompt_id = _prompt_id(item.get("prompt_id"))
        normalized[prompt_id] = {
            "prompt_id": prompt_id,
            "body": str(item.get("body") or ""),
            "description": str(item.get("description") or ""),
            "variables": _string_list(item.get("variables")),
            "enabled": bool(item.get("enabled", True)),
            "source": str(item.get("source") or "legacy"),
        }
    return [normalized[key] for key in sorted(normalized)]


def _public_version(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"previous_body", "next_body"}
    }


def _prompt_id(value: Any) -> str:
    prompt_id = str(value or "").strip()
    if not prompt_id or _SAFE_ID.fullmatch(prompt_id) is None:
        raise ValueError("invalid prompt_id")
    return prompt_id


def _version_id(value: Any) -> str:
    version_id = str(value or "").strip()
    if not version_id or _SAFE_ID.fullmatch(version_id) is None:
        raise ValueError("invalid version_id")
    return version_id


def _edge_id(value: Any) -> str:
    edge_id = str(value or "").strip()
    if not edge_id or len(edge_id) > 256 or any(
        character in edge_id for character in ("\x00", "\r", "\n")
    ):
        raise ValueError("invalid edge_id")
    return edge_id


def _prompt_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".system.md", ".prompt.md", ".md", ".txt"):
        if name.endswith(suffix):
            return _prompt_id(name[: -len(suffix)])
    return _prompt_id(path.stem)


def _body_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(str(body).encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _string_list(value: Any) -> list[str]:
    return [
        str(item).strip()
        for item in (value or [])
        if str(item).strip()
    ] if isinstance(value, list) else []


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    blocked = {"secret", "token", "credential", "authorization", "api_key", "apikey"}
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key)
        if _sensitive_key(normalized_key, blocked):
            continue
        sanitized = _sanitize_metadata_value(item, blocked)
        if sanitized is not _DROP:
            result[normalized_key] = sanitized
    return result


_DROP = object()


def _sanitize_metadata_value(value: Any, blocked: set[str]) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, list):
        return [
            sanitized
            for item in value
            if (sanitized := _sanitize_metadata_value(item, blocked)) is not _DROP
        ]
    if isinstance(value, Mapping):
        return {
            str(key): sanitized
            for key, item in value.items()
            if not _sensitive_key(str(key), blocked)
            and (sanitized := _sanitize_metadata_value(item, blocked)) is not _DROP
        }
    return _DROP


def _sensitive_key(key: str, blocked: set[str]) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in blocked)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
