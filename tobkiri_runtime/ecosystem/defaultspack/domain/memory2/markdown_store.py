"""Deprecated Markdown facade that writes only to the memory owner contract."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Mapping

from domain.memory.store import MemoryStore

from .sqlite_store import default_memory_dir


class MarkdownMemoryStore:
    """Finite compatibility facade; Markdown files are no longer owner state."""

    def __init__(self, *_: Any, **__: Any) -> None:
        warnings.warn(
            "MarkdownMemoryStore no longer writes Markdown owner state",
            DeprecationWarning,
            stacklevel=2,
        )
        self.root = default_memory_dir()
        self.memory = MemoryStore()

    def append_memory(
        self, content: str, metadata: Mapping[str, Any] | None = None
    ) -> Path:
        """Write source content once through the memory owner."""
        self.memory.store(content, {"legacy_projection": "memory", **dict(metadata or {})})
        return self.root / "MEMORY.md"

    def append_daily(
        self, content: str, metadata: Mapping[str, Any] | None = None
    ) -> Path:
        """Write a daily-scoped source record without a Markdown copy."""
        self.memory.store(content, {"scope": "daily", **dict(metadata or {})})
        return self.root / "daily"

    def append_user(
        self, content: str, metadata: Mapping[str, Any] | None = None
    ) -> Path:
        """Write a user-scoped source record without a Markdown copy."""
        self.memory.store(content, {"scope": "user", **dict(metadata or {})})
        return self.root / "USER.md"

    def snapshot(self) -> dict[str, str]:
        """Return no file projection; owner records are queried through contracts."""
        return {}
