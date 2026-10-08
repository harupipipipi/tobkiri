"""Compose admitted external Packs for one already verified active Profile."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..authority.v4 import AuthorityDenied


def catalog_for_verified_active_profile(
    catalog: Any,
    active: Any,
    *,
    user_data: Path,
) -> Any:
    """Retain exact active external pins without replacing bundled authority.

    The caller must first revalidate the persisted activation against Authority.
    Admission/CAS verification remains owned by the existing catalog loader;
    this function adds no approval, grant, Profile definition, or route.
    """
    from ..pack_control_v4 import catalog_with_admitted_pack_closure
    from .profile_capture import runtime_user_data_scope

    profile = active.resolved.profile
    rows = profile["packs"]
    selected_ids = {row["pack_id"] for row in rows}
    selected_ids.update((profile["base"]["pack_id"], profile["shell"]["pack_id"]))
    artifact_pins = {row["pack_id"]: row["artifact_digest"] for row in rows}
    changed_ids = {
        pack_id for pack_id, digest in artifact_pins.items()
        if pack_id not in catalog.packs
        or catalog.packs[pack_id]["pack"]["artifact_digest"] != digest
    }
    if selected_ids.issubset(catalog.packs) and not changed_ids:
        return catalog
    with runtime_user_data_scope(user_data):
        merged, _closure = catalog_with_admitted_pack_closure(
            catalog, sorted(selected_ids), artifact_pins=artifact_pins,
        )

    # Include transitively admitted dependencies, even when a Pack has no
    # executable bindings. Both active graphs must pin each new manifest once.
    for pack_id in (set(merged.packs) - set(catalog.packs)) | changed_ids:
        manifest = merged.packs[pack_id]
        digest = manifest["pack"]["artifact_digest"]
        if (
            not isinstance(digest, str)
            or manifest["pack"]["kind"] not in {"normal_sandbox", "application"}
            or manifest["requirements"]["execution_boundary"] not in {"sandbox", "declarative_only"}
        ):
            raise AuthorityDenied("active external Pack identity is invalid")
        selected_rows = [row for row in rows if row["pack_id"] == pack_id]
        if selected_rows and (
            len(selected_rows) != 1
            or selected_rows[0].get("role") not in {"provider", "application"}
            or selected_rows[0].get("artifact_digest") != digest
        ):
            raise AuthorityDenied("active external Pack Profile pin is stale")
        for graph in (active.resolved.lock, active.resolved.plan):
            pins = [
                row
                for row in graph["effective_set"]
                if isinstance(row, Mapping) and row.get("identity") == pack_id
            ]
            if (
                len(pins) != 1
                or pins[0].get("role") != "pack"
                or pins[0].get("artifact_digest") != digest
            ):
                raise AuthorityDenied("active external Pack closure pin is stale")
    return merged
