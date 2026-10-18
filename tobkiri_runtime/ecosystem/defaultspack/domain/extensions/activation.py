"""Extension selection projected only from the verified v4 activation."""

from __future__ import annotations

from pathlib import Path

from core_runtime.resolved_profile_scope import effective_pack_ids, persisted_resolved_profile


def selected_extension_pack_ids(pack_root: Path | str) -> set[str]:
    del pack_root
    return set(effective_pack_ids())


def selected_extension_pack_artifacts(pack_root: Path | str) -> dict[str, str]:
    """Return effective Pack IDs bound to their verified v4 artifact digests."""

    del pack_root
    plan = persisted_resolved_profile()
    if plan is None:
        return {}
    effective = {str(pack_id) for pack_id in getattr(plan, "effective_pack_set", ())}
    artifacts: dict[str, str] = {}
    for pack in getattr(plan, "packs", ()):
        pack_id = str(getattr(pack, "pack_id", ""))
        digest = str(getattr(pack, "manifest_hash", "") or "").strip()
        if pack_id in effective and digest:
            artifacts[pack_id] = digest
    return artifacts
