"""Extension selection projected only from the verified v4 activation."""

from __future__ import annotations

from pathlib import Path

from core_runtime.resolved_profile_scope import effective_pack_ids


def selected_extension_pack_ids(pack_root: Path | str) -> set[str]:
    del pack_root
    return set(effective_pack_ids())
