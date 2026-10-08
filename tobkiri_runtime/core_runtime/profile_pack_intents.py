"""Definition-only Pack pins, independent of enablement and authority."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest

from .profile_definition_store_v4 import ProfileDefinitionStore


def profile_artifact_pins(profile: Mapping[str, Any]) -> dict[str, str]:
    """Return exact pins without adding optional intent to resolution roots."""
    return {
        str(row["pack_id"]): str(row["artifact_digest"])
        for row in [*profile["packs"], *profile.get("optional_pack_revisions", [])]
        if row.get("artifact_digest") is not None
    }


def verified_definition_source(
    profile_id: str,
    definition_digest: str,
    *,
    catalog: Any,
    user_data: Path,
) -> Mapping[str, Any]:
    """Load the exact source bound by an already verified activation plan."""
    store = ProfileDefinitionStore(user_data)
    source = store.get_profile_revision(profile_id, definition_digest)
    if source is None and store.get_profile(profile_id, include_tombstone=True) is None:
        # Legacy packaged activation may precede registration. Only an exact
        # verified bundled source is compatible; the current head is no fallback.
        bundled = catalog.profiles.get(profile_id)
        if bundled is not None and canonical_digest(bundled) == definition_digest:
            source = deepcopy(dict(bundled))
    if source is None or canonical_digest(source) != definition_digest:
        raise ValueError("active Profile definition history is unavailable")
    return source
