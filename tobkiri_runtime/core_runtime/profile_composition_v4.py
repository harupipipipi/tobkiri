"""Non-authorizing edits to Host-owned Profile Pack definitions."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from tobkiri_protocol.ids import validate_canonical_id
from tobkiri_protocol.validation import validate_document

from .profile_catalog_v4 import (
    bundle_lock_digest,
    profile_catalog_digest,
    require_profile_catalog_binding,
)
from .profile_runtime_port import require_profile_runtime


def project_composition_catalog(catalog: Any) -> dict[str, object]:
    """Publish verified Pack choices independently of the active Profile."""
    return {
        "composition_api_version": "io.tobkiri.profile-composition.v4",
        "profile_catalog_digest": profile_catalog_digest(catalog),
        "bundle_lock_digest": bundle_lock_digest(catalog),
        "packs": [
            {
                "pack_id": pack_id,
                "display_name": str(manifest["pack"].get("display_name") or pack_id),
                "version": str(manifest["pack"]["version"]),
                "kind": str(manifest["pack"]["kind"]),
                "artifact_digest": str(manifest["pack"]["artifact_digest"]),
                "dependencies": list(manifest["requirements"]["pack_dependencies"]),
            }
            for pack_id, manifest in sorted(catalog.packs.items())
        ],
    }


def build_profile_composition(
    catalog: Any,
    profile_id: str,
    expected_revision: str,
    composition: object,
) -> dict[str, Any]:
    """Build an unresolved successor from verified choices, without activation.

    The client supplies IDs and freshness fences only. The Host derives artifact
    bindings and operation requests; approval and execution remain separate.
    """
    if not isinstance(composition, Mapping) or set(composition) != {
        "pack_ids", "profile_catalog_digest", "bundle_lock_digest"
    }:
        raise ValueError("Profile composition shape is invalid")
    if not all(isinstance(composition[key], str) for key in (
        "profile_catalog_digest", "bundle_lock_digest"
    )):
        raise ValueError("Profile composition binding is invalid")
    source = require_profile_catalog_binding(
        catalog,
        profile_id=profile_id,
        expected_definition_digest=expected_revision,
        expected_catalog_digest=composition["profile_catalog_digest"],
        expected_bundle_lock_digest=composition["bundle_lock_digest"],
    )
    pack_ids = composition["pack_ids"]
    if not isinstance(pack_ids, list) or not pack_ids:
        raise ValueError("Select at least one Profile Pack")
    for pack_id in pack_ids:
        if not isinstance(pack_id, str):
            raise ValueError("Profile Pack ID is invalid")
        validate_canonical_id(pack_id)
    if len(pack_ids) != len(set(pack_ids)):
        raise ValueError("Profile Pack IDs must be unique")
    for pack_id in pack_ids:
        manifest = catalog.packs.get(pack_id)
        if manifest is None or manifest["pack"]["kind"] in {"base", "shell", "application"}:
            raise ValueError("Profile Pack choice is unavailable")

    successor = copy.deepcopy(dict(source))
    previous = {row["pack_id"]: row for row in source["packs"]}
    successor["packs"] = [
        {
            "pack_id": pack_id,
            "artifact_digest": catalog.packs[pack_id]["pack"]["artifact_digest"],
            "role": previous.get(pack_id, {}).get("role", "provider"),
        }
        for pack_id in sorted(pack_ids)
    ] + [copy.deepcopy(row) for row in source["packs"] if row["role"] == "application"]
    # Retain only requests whose caller and provider remain in the new closure.
    # Required dependencies are derived from signed manifests, not client input.
    closure = {source["base"]["pack_id"], source["shell"]["pack_id"]}
    pending = [row["pack_id"] for row in successor["packs"]] + list(closure)
    while pending:
        pack_id = pending.pop()
        manifest = catalog.packs.get(pack_id)
        if manifest is None:
            raise ValueError("Profile Pack dependency is unavailable")
        for dependency in manifest["requirements"]["pack_dependencies"]:
            if dependency not in closure:
                pending.append(dependency)
        closure.add(pack_id)
        # Mark dependencies before another branch can enqueue the same cycle.
        closure.update(manifest["requirements"]["pack_dependencies"])
    functions = {
        function["id"]
        for pack_id in closure
        for function in catalog.packs[pack_id]["functions"]
    }
    successor["requested_edges"] = [
        copy.deepcopy(edge) for edge in source["requested_edges"]
        if edge["caller_function_id"] in functions
        and edge["target_provider_id"] in functions
    ]
    runtime = require_profile_runtime()
    updated_catalog = runtime.catalog_with_profiles(
        catalog, {**catalog.profiles, profile_id: successor}
    )
    additions = tuple(sorted(set(pack_ids) - set(previous)))
    successor["requested_edges"].extend(
        runtime.dynamic_profile_edges(updated_catalog, profile_id, additions)
    )
    successor.update({
        "state": "needs_resolution",
        "catalog_revision": None,
        "authority_references": [],
        "profile_authority_snapshot_digest": None,
    })
    validate_document(successor, "profile")
    return successor
