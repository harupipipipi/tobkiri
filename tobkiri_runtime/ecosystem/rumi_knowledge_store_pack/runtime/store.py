"""Single-write profile-bound knowledge owner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

VERSION = "rumi.knowledge-store.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class KnowledgeConflict(RuntimeError):
    """Raised for stale owner state or invalid migration state."""


class KnowledgeStore:
    """Own source knowledge records; indexes remain disposable projections."""

    def __init__(self, profile_id: str, *, user_data_root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(user_data_root or USER_DATA_DIR)
            / "packs"
            / "rumi_knowledge_store_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "knowledge.json"
        self.backup_root = self.root / "migration_backups"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return the complete authoritative knowledge snapshot."""
        state = self._read()
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "items": [_copy(state["items"][key]) for key in sorted(state["items"])],
            "migration": _copy(state.get("migration")),
        }

    def get(self, knowledge_id: str) -> dict[str, Any] | None:
        """Return one source record without a derived embedding."""
        item = self._read()["items"].get(_identifier(knowledge_id))
        return _copy(item) if isinstance(item, Mapping) else None

    def search(self, query: str, *, limit: int = 8) -> dict[str, Any]:
        """Return deterministic local search results from source records."""
        state = self._read()
        ranked = []
        for item in state["items"].values():
            text = " ".join(
                [str(item.get("title") or ""), str(item.get("content") or "")]
            )
            score = _score(query, text)
            if score > 0:
                ranked.append({**_copy(item), "score": score})
        ranked.sort(key=lambda item: (-item["score"], item["id"]))
        return {
            "revision": state["revision"],
            "index_kind": "derived_local_text",
            "items": ranked[: max(0, limit)],
        }

    def put(
        self, record: Mapping[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        """Create or replace one source record at an exact revision."""
        normalized = _record(record)
        with NamedLock(self.lock_root, "knowledge"):
            state = self._read()
            _assert_revision(state, expected_revision)
            current = state["items"].get(normalized["id"])
            normalized["record_revision"] = (
                int(current.get("record_revision") or 0) + 1 if current else 1
            )
            if current:
                normalized["created_at"] = current["created_at"]
            normalized["updated_at"] = _now_ms()
            state["items"][normalized["id"]] = normalized
            state["revision"] += 1
            self._write(state)
        return {"item": _copy(normalized), "revision": state["revision"]}

    def delete(
        self, knowledge_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        """Delete one source record at an exact revision."""
        knowledge_id = _identifier(knowledge_id)
        with NamedLock(self.lock_root, "knowledge"):
            state = self._read()
            _assert_revision(state, expected_revision)
            if knowledge_id not in state["items"]:
                raise KeyError("knowledge record is unknown")
            del state["items"][knowledge_id]
            state["revision"] += 1
            self._write(state)
        return {"deleted_id": knowledge_id, "revision": state["revision"]}

    def migrate(
        self,
        records: list[Mapping[str, Any]],
        *,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Import a source-hash-bound snapshot exactly once."""
        source = {"items": [_record(item) for item in records]}
        actual_hash = _hash(source)
        if actual_hash != expected_source_hash:
            raise KnowledgeConflict("knowledge migration source hash does not match")
        with NamedLock(self.lock_root, "knowledge"):
            state = self._read()
            if state["items"] or state.get("migration"):
                raise KnowledgeConflict("knowledge owner is not migration-empty")
            migration_id = str(uuid.uuid4())
            backup = self.backup_root / f"{migration_id}.json"
            _atomic_json(backup, state)
            state["items"] = {item["id"]: item for item in source["items"]}
            state["revision"] += 1
            state["migration"] = {
                "id": migration_id,
                "source_hash": actual_hash,
                "backup": backup.name,
            }
            self._write(state)
        return {"migration_id": migration_id, "revision": state["revision"]}

    def rollback(self, migration_id: str) -> dict[str, Any]:
        """Restore the owner backup bound to the migration marker."""
        migration_id = _identifier(migration_id)
        with NamedLock(self.lock_root, "knowledge"):
            state = self._read()
            marker = state.get("migration")
            if not isinstance(marker, Mapping) or marker.get("id") != migration_id:
                raise KnowledgeConflict("knowledge migration marker does not match")
            backup = self.backup_root / str(marker.get("backup") or "")
            restored = json.loads(backup.read_text(encoding="utf-8"))
            self._write(self._normalize(restored))
        return {"rolled_back": migration_id}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        return self._normalize(json.loads(self.path.read_text(encoding="utf-8")))

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)

    def _empty(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": 0,
            "items": {},
            "migration": None,
        }

    def _normalize(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("knowledge owner state is invalid")
        if value.get("profile_id") != self.profile_id:
            raise ValueError("knowledge owner profile does not match")
        items = value.get("items")
        if not isinstance(items, Mapping):
            raise ValueError("knowledge owner records are invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": max(0, int(value.get("revision") or 0)),
            "items": {str(key): _copy(item) for key, item in items.items()},
            "migration": _copy(value.get("migration")),
        }


def create_knowledge_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create knowledge read operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = KnowledgeStore(_profile(payload))
        if name == "snapshot":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("knowledge_id") or ""))
        if name == "search":
            return store.search(
                str(payload.get("query") or ""),
                limit=max(0, min(100, int(payload.get("limit") or 8))),
            )
        raise ValueError(f"unknown knowledge resource operation: {name}")

    return operation


def create_knowledge_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create source knowledge mutation operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = KnowledgeStore(_profile(payload))
        expected = int(payload.get("expected_revision") or 0)
        if name == "put":
            return store.put(_mapping(payload.get("item")), expected_revision=expected)
        if name == "delete":
            return store.delete(
                str(payload.get("knowledge_id") or ""),
                expected_revision=expected,
            )
        raise ValueError(f"unknown knowledge action: {name}")

    return operation


def create_migration_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create explicit knowledge migration operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = KnowledgeStore(_profile(payload))
        if name == "migrate":
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("knowledge migration items are invalid")
            return store.migrate(
                [item for item in items if isinstance(item, Mapping)],
                expected_source_hash=str(payload.get("expected_source_hash") or ""),
            )
        if name == "rollback":
            return store.rollback(str(payload.get("migration_id") or ""))
        raise ValueError(f"unknown knowledge migration action: {name}")

    return operation


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    now = _now_ms()
    metadata = value.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("knowledge metadata must be an object")
    return {
        "id": _identifier(value.get("id") or uuid.uuid4()),
        "title": str(value.get("title") or ""),
        "content": str(value.get("content") or ""),
        "source_reference": _copy(value.get("source_reference")),
        "metadata": _copy(metadata),
        "created_at": int(value.get("created_at") or now),
        "updated_at": int(value.get("updated_at") or now),
        "record_revision": max(1, int(value.get("record_revision") or 1)),
    }


def _score(query: str, content: str) -> float:
    words = set(query.casefold().split())
    content = content.casefold()
    if not words:
        return 1.0
    if query.casefold() in content:
        return 1.0
    return round(sum(word in content for word in words) / len(words), 6)


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise KnowledgeConflict("knowledge store revision is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("knowledge identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".knowledge-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

