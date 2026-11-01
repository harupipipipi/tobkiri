"""Deprecated MemoryStore facade over the selected global memory owner."""

from __future__ import annotations

import uuid
import warnings
from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
)

RESOURCE = "rumi.resource.memory.v1"
MANAGE = "rumi.action.memory.manage.v1"


class MemoryStore:
    """Finite legacy facade; never owns or writes a secondary memory store."""

    def __init__(self) -> None:
        warnings.warn(
            "domain.memory.store.MemoryStore is a Wave 7 compatibility facade",
            DeprecationWarning,
            stacklevel=2,
        )

    @property
    def long_term(self) -> list[dict[str, Any]]:
        """Project legacy long-term records read-only from the global owner."""
        return self._items()

    @property
    def vector_store(self) -> list[dict[str, Any]]:
        """Project the same source records; vectors are not authoritative."""
        return self._items()

    @property
    def short_term(self) -> dict[str, Any]:
        """Return no canonical short-term map; turn runtime owns ephemeral state."""
        return {}

    @property
    def project_context(self) -> dict[str, Any]:
        """Project records explicitly scoped as project context."""
        result: dict[str, Any] = {}
        for item in self._items():
            if item.get("scope") != "project_context":
                continue
            metadata = item.get("metadata")
            key = metadata.get("key") if isinstance(metadata, Mapping) else None
            if key:
                result[str(key)] = item.get("content")
        return result

    def store(
        self,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write exactly once through the selected global owner."""
        metadata = dict(metadata or {})
        item = {
            "id": str(uuid.uuid4()),
            "content": str(content),
            "scope": str(metadata.get("scope") or "user"),
            "source": "legacy_memory_facade",
            "metadata": metadata,
        }
        result = self._put_with_retry(item)
        return dict(result.get("item") or {})

    def recall(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Read search results only from the selected global owner."""
        result = _invoke(RESOURCE, "search", {"query": query, "limit": limit})
        return [dict(item) for item in result.get("items") or []]

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Alias recall for the finite legacy surface."""
        return self.recall(query, limit)

    def vector_add(
        self,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store source content once; no authoritative vector copy is created."""
        values = dict(metadata or {})
        values["legacy_projection"] = "vector"
        return self.store(content, values)

    def vector_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search the source owner instead of a second vector store."""
        return self.recall(query, limit)

    def delete(self, memory_id: str) -> bool:
        """Delete one record through the selected owner."""
        _invoke(
            MANAGE,
            "delete",
            {"memory_id": memory_id, "expected_revision": self._revision()},
        )
        return True

    def update(
        self, memory_id: str, updates: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Replace one record atomically through the selected owner."""
        current = next(
            (item for item in self._items() if item.get("id") == memory_id),
            None,
        )
        if current is None:
            return None
        item = dict(current)
        for key in ("content", "scope", "source", "expires_at"):
            if key in updates:
                item[key] = updates[key]
        if isinstance(updates.get("metadata"), Mapping):
            item["metadata"] = dict(updates["metadata"])
        result = self._put_with_retry(item)
        return dict(result.get("item") or {})

    def clear(self) -> None:
        """Delete projected records one by one with fresh owner revisions."""
        for item in self._items():
            self.delete(str(item["id"]))

    def put_project_context(self, key: str, value: Any) -> dict[str, Any]:
        """Write one project-context record through the owner contract."""
        existing = next(
            (
                item
                for item in self._items()
                if item.get("scope") == "project_context"
                and isinstance(item.get("metadata"), Mapping)
                and item["metadata"].get("key") == key
            ),
            None,
        )
        item = {
            "id": existing.get("id") if existing else str(uuid.uuid4()),
            "content": value,
            "scope": "project_context",
            "source": "legacy_memory_facade",
            "metadata": {"key": key},
        }
        result = self._put_with_retry(item)
        return dict(result.get("item") or {})

    def put_record(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Upsert a finite compatibility record while retaining its ID."""
        result = self._put_with_retry(dict(item))
        return dict(result.get("item") or {})

    def list_records(self) -> list[dict[str, Any]]:
        """Return the complete read-only compatibility projection."""
        return self._items()

    def _items(self) -> list[dict[str, Any]]:
        snapshot = _invoke(RESOURCE, "snapshot", {})
        return [dict(item) for item in snapshot.get("items") or []]

    def _revision(self) -> int:
        snapshot = _invoke(RESOURCE, "snapshot", {})
        return int(snapshot.get("revision") or 0)

    def _put_with_retry(self, item: Mapping[str, Any]) -> Any:
        """Retry only stale optimistic revisions from concurrent callers.

        The owner remains responsible for atomicity and conflict detection.
        This facade may have to re-read its revision between two threads; it
        retries that one expected conflict without masking owner unavailability
        or any other policy/error response.
        """
        for attempt in range(8):
            try:
                return _invoke(
                    MANAGE,
                    "put",
                    {"item": dict(item), "expected_revision": self._revision()},
                )
            except RuntimeError as exc:
                if str(exc) != "memory store revision is stale" or attempt == 7:
                    raise
        raise RuntimeError("memory owner write retry limit reached")


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise RuntimeError("global memory owner is unavailable")
    return invoke_global_contract(
        registry,
        contract_id,
        operation,
        {"profile_id": captured_profile_id(registry), **dict(payload)},
    )
