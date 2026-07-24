"""Deprecated KnowledgeStore facade over the global knowledge owner."""

from __future__ import annotations

import uuid
import warnings
from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract
from core_runtime.resolved_profile_scope import active_resolved_profile

RESOURCE = "rumi.resource.knowledge.v1"
MANAGE = "rumi.action.knowledge.manage.v1"


class KnowledgeStore:
    """Finite legacy facade without local storage or provider fallback."""

    def __init__(self) -> None:
        warnings.warn(
            "domain.knowledge.store.KnowledgeStore is a Wave 7 facade",
            DeprecationWarning,
            stacklevel=2,
        )

    def create(
        self, content: str, metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create one source record through the selected owner."""
        result = _invoke(
            MANAGE,
            "put",
            {
                "expected_revision": self._revision(),
                "item": {
                    "id": str(uuid.uuid4()),
                    "content": str(content),
                    "metadata": dict(metadata or {}),
                    "source_reference": {"kind": "legacy_facade"},
                },
            },
        )
        return dict(result.get("item") or {})

    def get(self, entry_id: str) -> dict[str, Any] | None:
        """Get one source record from the selected owner."""
        value = _invoke(RESOURCE, "get", {"knowledge_id": entry_id})
        return dict(value) if isinstance(value, Mapping) else None

    def list_entries(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Project a stable legacy list from the owner snapshot."""
        snapshot = _invoke(RESOURCE, "snapshot", {})
        items = [dict(item) for item in snapshot.get("items") or []]
        items.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return {"items": items[offset : offset + limit], "total": len(items)}

    def update(
        self,
        entry_id: str,
        content: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Replace one source record atomically through the owner."""
        current = self.get(entry_id)
        if current is None:
            return None
        if content is not None:
            current["content"] = str(content)
        if metadata is not None:
            current["metadata"] = dict(metadata)
        result = _invoke(
            MANAGE,
            "put",
            {"expected_revision": self._revision(), "item": current},
        )
        return dict(result.get("item") or {})

    def delete(self, entry_id: str) -> bool:
        """Delete one source record through the selected owner."""
        _invoke(
            MANAGE,
            "delete",
            {
                "knowledge_id": entry_id,
                "expected_revision": self._revision(),
            },
        )
        return True

    def search(
        self, query: str, limit: int = 5, threshold: float = 0.0
    ) -> list[dict[str, Any]]:
        """Use the selected owner's derived search projection only."""
        result = _invoke(
            RESOURCE,
            "search",
            {"query": query, "limit": limit},
        )
        return [
            dict(item)
            for item in result.get("items") or []
            if float(item.get("score") or 0.0) > threshold
        ]

    @staticmethod
    def _revision() -> int:
        snapshot = _invoke(RESOURCE, "snapshot", {})
        return int(snapshot.get("revision") or 0)


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("interface_registry")
    plan = active_resolved_profile()
    if registry is None or plan is None:
        raise RuntimeError("global knowledge owner is unavailable")
    return invoke_global_contract(
        registry,
        contract_id,
        operation,
        {"profile_id": plan.profile_id, **dict(payload)},
    )
