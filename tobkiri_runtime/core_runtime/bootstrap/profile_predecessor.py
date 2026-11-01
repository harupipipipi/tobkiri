"""Bind an inactive imported bootstrap definition to an explicit successor review."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest

from ..development_host_handover_guard import read_handover_journal
from ..profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)


def _require_inactive(user_data: Path, profile_id: str) -> None:
    paths = (
        user_data / "profiles" / "active.json",
        user_data / "workspaces" / profile_id / "activation" / "active.json",
    )
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ProfileDefinitionStoreConflict(
            "inactive bootstrap predecessor has an active Profile"
        )


def inactive_bootstrap_predecessor(
    *,
    user_data: Path,
    source: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Project only an unchanged data-only import from its authenticated fence.

    Missing import evidence keeps ordinary template/conflict behavior. A
    completed handover whose destination registry changed cannot authorize a
    replacement. No staged history or former Authority state is read.
    """
    profile_id = str(source["profile_id"])
    active_paths = (
        user_data / "profiles" / "active.json",
        user_data / "workspaces" / profile_id / "activation" / "active.json",
    )
    if any(path.exists() or path.is_symlink() for path in active_paths):
        return None
    index = user_data / "profiles" / "index.json"
    if not index.exists() and not index.is_symlink():
        return None
    current = (
        ProfileDefinitionStore(user_data).snapshot() if snapshot is None else snapshot
    )
    entries = [row for row in current["profiles"] if row["profile_id"] == profile_id]
    if not entries:
        return None
    entry = entries[0]
    if entry["tombstone"]:
        raise ProfileDefinitionStoreConflict("imported bootstrap Profile is tombstoned")
    revision = entry["current_revision"]
    versions = [
        row for row in entry["revisions"] if row["profile_revision"] == revision
    ]
    if len(versions) != 1 or versions[0]["tombstone"]:
        raise ProfileDefinitionStoreConflict(
            "imported bootstrap revision is unavailable"
        )
    existing = versions[0]["profile"]
    if dict(existing) == dict(source):
        return None
    journal = read_handover_journal(user_data)
    if journal is None:
        return None
    if (
        journal.get("stage") != "completed"
        or journal.get("destination_profile_generation") != current["generation"]
        or journal.get("destination_profile_snapshot_digest")
        != canonical_digest(current)
        or current["bootstrap"]
        != {"state": "not_required", "template_profile_revision": None}
    ):
        raise ProfileDefinitionStoreConflict(
            "imported bootstrap registry changed after completed handover"
        )
    if (
        existing.get("state") != "needs_resolution"
        or existing.get("catalog_revision") is not None
        or existing.get("authority_references") != []
        or existing.get("profile_authority_snapshot_digest") is not None
        or "content_projections" in existing
        or any("authority_reference" in edge for edge in existing["requested_edges"])
        or existing.get("mode") != "interactive"
        or not isinstance(existing.get("shell"), Mapping)
    ):
        raise ProfileDefinitionStoreConflict(
            "bootstrap import is not inert definition data"
        )
    _require_inactive(user_data, profile_id)
    shell = existing["shell"]
    return {
        "profile_revision": revision,
        "definition_digest": canonical_digest(existing),
        "store_generation": current["generation"],
        "snapshot_digest": canonical_digest(current),
        "handover_digest": canonical_digest(journal),
        "display_name": str(existing.get("display_name") or profile_id),
        "pack_ids": [str(row["pack_id"]) for row in existing["packs"]],
        "base_pack_id": str(existing["base"]["pack_id"]),
        "shell_pack_id": str(shell["pack_id"]),
        "shell_provider_id": str(shell["provider_id"]),
        "shell_artifact_digest": shell["artifact_digest"],
        "shell_executable_artifact_digest": shell["executable_artifact_digest"],
        "shell_definition_revision": shell["definition_revision"],
    }


def require_inactive_bootstrap_predecessor(
    *,
    user_data: Path,
    source: Mapping[str, Any],
    expected: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> None:
    """Require the exact reviewed predecessor immediately before registry CAS."""
    _require_inactive(user_data, str(source["profile_id"]))
    current = inactive_bootstrap_predecessor(
        user_data=user_data, source=source, snapshot=snapshot
    )
    if current is None or dict(expected) != current:
        raise ProfileDefinitionStoreConflict("inactive bootstrap predecessor is stale")
