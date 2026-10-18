from __future__ import annotations

from pathlib import Path

from domain.memory.store import MemoryStore

from .sqlite_store import default_memory_dir


def write_wiki_page(slug: str, content: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in slug) or "page"
    MemoryStore().store(
        content,
        {"scope": "wiki", "source": "legacy_wiki_facade", "slug": safe},
    )
    return default_memory_dir() / "wiki" / f"{safe}.md"
