"""Deprecated SQLite-shaped facade over the global memory owner."""

from __future__ import annotations

import uuid
import warnings
from pathlib import Path
from typing import Any, Mapping

from domain.memory.store import MemoryStore


def default_memory_dir() -> Path:
    """Return a nonauthoritative compatibility label path."""
    import os

    configured = os.environ.get("RUMI_DEFAULTSPACK_MEMORY2_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("compatibility") / "memory-owner-contract"


class MemorySQLiteStore:
    """Finite API facade that opens and owns no SQLite database."""

    def __init__(self, *_: Any, **__: Any) -> None:
        warnings.warn(
            "MemorySQLiteStore no longer owns SQLite state",
            DeprecationWarning,
            stacklevel=2,
        )
        self.root = default_memory_dir()
        self.db_path = self.root / "removed-state.db"
        self.memory = MemoryStore()

    def add(
        self,
        content: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        scope: str = "user",
        agent_id: str | None = None,
        project_id: str | None = None,
        source: str = "manual",
        confidence: float = 1.0,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        """Write once through the global memory owner."""
        return self.memory.put_record(
            {
                "id": memory_id or str(uuid.uuid4()),
                "content": content,
                "scope": scope,
                "source": source,
                "metadata": {
                    **dict(metadata or {}),
                    "agent_id": agent_id,
                    "project_id": project_id,
                    "confidence": float(confidence),
                },
            }
        )

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """Read one projected record by ID."""
        return next(
            (
                item
                for item in self.memory.list_records()
                if item.get("id") == memory_id
            ),
            None,
        )

    def update(
        self, memory_id: str, updates: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Update one record through the global memory owner."""
        return self.memory.update(memory_id, updates)

    def delete(self, memory_id: str) -> bool:
        """Delete one record through the global memory owner."""
        if self.get(memory_id) is None:
            return False
        return self.memory.delete(memory_id)

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        scope: str | None = None,
        agent_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search and filter the single owner projection."""
        result = self.memory.search(query, limit=max(limit, 100))
        filtered = []
        for item in result:
            metadata = item.get("metadata") or {}
            if scope is not None and item.get("scope") != scope:
                continue
            if agent_id is not None and metadata.get("agent_id") != agent_id:
                continue
            if project_id is not None and metadata.get("project_id") != project_id:
                continue
            filtered.append(item)
        return filtered[:limit]

    @staticmethod
    def json_dumps(value: Any) -> str:
        """Retain a formatting helper for finite callers."""
        import json

        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def json_loads(value: Any) -> dict[str, Any]:
        """Retain a parsing helper for finite callers."""
        import json

        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(str(value or "{}"))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
