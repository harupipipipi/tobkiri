"""Deprecated compatibility facade for the global knowledge owner."""

from __future__ import annotations

import uuid
import warnings
from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract
from core_runtime.resolved_profile_scope import active_resolved_profile

KNOWLEDGE = "rumi.resource.knowledge.v1"
KNOWLEDGE_MANAGE = "rumi.action.knowledge.manage.v1"


class KnowledgeStore:
    """Project legacy knowledge calls onto the selected authoritative owner."""

    _instance: "KnowledgeStore | None" = None

    def __new__(cls) -> "KnowledgeStore":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        warnings.warn(
            "domain.knowledge.store.KnowledgeStore is a Wave 7 compatibility facade",
            DeprecationWarning,
            stacklevel=2,
        )

    def create(
        self, content: str, metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a knowledge entry through the selected resource owner."""
        result = _invoke(
            KNOWLEDGE_MANAGE,
            "put",
            {
                "item": {
                    "id": str(uuid.uuid4()),
                    "content": str(content),
                    "metadata": dict(metadata or {}),
                },
                "expected_revision": self._revision(),
            },
        )
        return _item(result.get("item"))

    def get(self, entry_id: str) -> dict[str, Any] | None:
        """Return one entry or None when the owner does not contain it."""
        result = _invoke(KNOWLEDGE, "get", {"knowledge_id": str(entry_id)})
        return _item(result) if isinstance(result, Mapping) else None

    def list_entries(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List owner-backed entries using the legacy response shape."""
        snapshot = self._snapshot()
        items = [
            _item(value)
            for value in snapshot.get("items", [])
            if isinstance(value, Mapping)
        ]
        items.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
        return {"items": items[offset : offset + limit], "total": len(items)}

    def update(
        self,
        entry_id: str,
        content: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Replace mutable content or metadata through the selected owner."""
        current = self.get(entry_id)
        if current is None:
            return None
        item = {
            **current,
            "content": str(content) if content is not None else current["content"],
            "metadata": dict(metadata) if metadata is not None else current["metadata"],
        }
        result = _invoke(
            KNOWLEDGE_MANAGE,
            "put",
            {"item": item, "expected_revision": self._revision()},
        )
        return _item(result.get("item"))

    def delete(self, entry_id: str) -> bool:
        """Delete an entry through the selected owner."""
        try:
            _invoke(
                KNOWLEDGE_MANAGE,
                "delete",
                {
                    "knowledge_id": str(entry_id),
                    "expected_revision": self._revision(),
                },
            )
        except KeyError:
            return False
        return True

    def search(
        self, query: str, limit: int = 5, threshold: float = 0.0
    ) -> list[dict[str, Any]]:
        """Search owner-backed local text projections."""
        result = _invoke(
            KNOWLEDGE, "search", {"query": str(query), "limit": int(limit)}
        )
        items = result.get("items", []) if isinstance(result, Mapping) else []
        return [
            item
            for item in (
                _item(value) for value in items if isinstance(value, Mapping)
            )
            if float(item.get("score") or 0.0) > float(threshold)
        ]

    def _snapshot(self) -> dict[str, Any]:
        result = _invoke(KNOWLEDGE, "snapshot", {})
        return dict(result) if isinstance(result, Mapping) else {"items": []}

    def _revision(self) -> int:
        return int(self._snapshot().get("revision") or 0)


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    plan = active_resolved_profile()
    if registry is None or plan is None:
        raise RuntimeError("global knowledge owner is unavailable")
    return invoke_global_contract(
        registry,
        contract_id,
        operation,
        {"profile_id": plan.profile_id, **dict(payload)},
    )


def _item(value: Any) -> dict[str, Any]:
    item = dict(value) if isinstance(value, Mapping) else {}
    item.pop("embedding", None)
    item.setdefault("metadata", {})
    return item
