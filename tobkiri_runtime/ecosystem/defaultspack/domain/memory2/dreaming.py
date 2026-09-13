from __future__ import annotations

import os
from pathlib import Path

from core_runtime.paths import USER_DATA_DIR
from core_runtime.resolved_profile_scope import persisted_resolved_profile
from domain.memory.store import MemoryStore


def record_dream(content: str) -> str:
    """Record a dream and return the selected owner's readable state path."""

    entry = MemoryStore().store(content, {"scope": "dream", "source": "dreaming"})
    del entry
    configured = os.environ.get("RUMI_DEFAULTSPACK_MEMORY2_DIR", "").strip()
    if configured:
        return str(Path(configured).expanduser() / "memories.json")
    plan = persisted_resolved_profile()
    profile_id = str(getattr(plan, "profile_id", "") or "").strip()
    if not profile_id:
        return ""
    return str(
        Path(USER_DATA_DIR)
        / "packs"
        / "rumi_memory_store_pack"
        / "profiles"
        / profile_id
        / "memories.json"
    )
