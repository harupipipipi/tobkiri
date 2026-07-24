from __future__ import annotations

from domain.memory.store import MemoryStore


def record_dream(content: str) -> str:
    entry = MemoryStore().store(content, {"scope": "dream", "source": "dreaming"})
    return str(entry["id"])
