"""Public, CAS-bound Pack revision choices for immutable Named Profiles."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.ids import validate_artifact_digest, validate_canonical_id

from .bootstrap.profile_capture import host_profile_catalog, runtime_user_data_root
from .external_pack_catalog_v4 import (
    control_catalog_revision,
    load_external_pack_catalog,
    pack_revision_catalog_transaction,
)
from .profile_definition_store_v4 import (
    ProfileDefinitionNotFound,
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)


def profile_pack_versions(profile_id: str) -> dict[str, Any]:
    """Read verified available revisions without changing the active selection."""

    validate_canonical_id(profile_id, field="profile_id")
    catalog = host_profile_catalog()
    store = ProfileDefinitionStore(runtime_user_data_root())
    profile = store.get_profile(profile_id)
    if profile is None:
        raise ProfileDefinitionNotFound(profile_id)
    external = load_external_pack_catalog()
    rows: list[dict[str, Any]] = []
    for selection in profile.profile["packs"]:
        pack_id = str(selection["pack_id"])
        baseline = catalog.packs.get(pack_id)
        versions: dict[str, dict[str, Any]] = {}
        if baseline is not None and baseline["pack"]["kind"] in {"normal_sandbox", "application"}:
            pack = baseline["pack"]
            versions[str(pack["artifact_digest"])] = {
                "artifact_digest": str(pack["artifact_digest"]),
                "version": str(pack["version"]),
                "origin": "bundled",
                "capabilities": list(baseline["requirements"]["capabilities"]),
                "contracts": list(baseline["contracts"]),
            }
        for digest, entry in external.versions.get(pack_id, {}).items():
            record = entry["catalog_record"]
            versions[digest] = {
                "artifact_digest": digest,
                "version": str(entry["version_string"]),
                "origin": "signed-admission",
                "publisher_id": str(entry["publisher_id"]),
                "key_id": str(entry["key_id"]),
                "capabilities": list(record["capabilities"]),
                "contracts": list(record["provided_contracts"]),
            }
        if not versions:
            continue
        selected = selection.get("artifact_digest")
        if selected is None:
            if baseline is not None:
                selected = baseline["pack"]["artifact_digest"]
            else:
                baseline_entry = external.entries.get(pack_id)
                if baseline_entry is None:
                    raise ValueError("Profile Pack baseline is unavailable")
                selected = baseline_entry["artifact_digest"]
        if selected not in versions:
            raise ValueError("Profile Pack selected revision is unavailable")
        rows.append(
            {
                "pack_id": pack_id,
                "role": str(selection["role"]),
                "selected_digest": selected,
                "versions": [versions[key] for key in sorted(versions)],
            }
        )
    return {
        "profile_id": profile_id,
        "profile_definition_revision": profile.profile_revision,
        "store_generation": int(store.snapshot()["generation"]),
        "catalog_revision": control_catalog_revision(),
        "packs": rows,
    }


def select_profile_pack_version(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Append a definition successor; activation and authority stay separate."""

    if set(payload) != {
        "profile_id",
        "pack_id",
        "artifact_digest",
        "expected_selected_digest",
        "expected_profile_revision",
        "expected_store_generation",
        "expected_catalog_revision",
    }:
        raise ValueError("Profile Pack version selection fields are invalid")
    for field in ("profile_id", "pack_id"):
        validate_canonical_id(payload[field], field=field)
    for field in (
        "artifact_digest",
        "expected_selected_digest",
        "expected_profile_revision",
        "expected_catalog_revision",
    ):
        validate_artifact_digest(payload[field], field=field)
    if type(payload["expected_store_generation"]) is not int:
        raise ValueError("Profile store generation must be an integer")
    with pack_revision_catalog_transaction():
        return _select_profile_pack_version_locked(payload)


def _select_profile_pack_version_locked(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Commit only while admission cannot change the reviewed catalog."""
    view = profile_pack_versions(str(payload["profile_id"]))
    if (
        view["profile_definition_revision"] != payload["expected_profile_revision"]
        or view["store_generation"] != payload["expected_store_generation"]
        or view["catalog_revision"] != payload["expected_catalog_revision"]
    ):
        raise ProfileDefinitionStoreConflict("Profile Pack version selection is stale")
    matches = [row for row in view["packs"] if row["pack_id"] == payload["pack_id"]]
    if len(matches) != 1 or matches[0]["selected_digest"] != payload["expected_selected_digest"]:
        raise ProfileDefinitionStoreConflict("Profile selected Pack revision is stale")
    if payload["artifact_digest"] not in {row["artifact_digest"] for row in matches[0]["versions"]}:
        raise ValueError("Profile Pack revision is not admitted")
    store = ProfileDefinitionStore(runtime_user_data_root())
    current = store.get_profile(str(payload["profile_id"]))
    if current is None:
        raise ProfileDefinitionNotFound(str(payload["profile_id"]))
    if current.profile_revision != payload["expected_profile_revision"]:
        raise ProfileDefinitionStoreConflict("Profile Pack version selection is stale")
    candidate = deepcopy(dict(current.profile))
    for row in candidate["packs"]:
        if row["pack_id"] == payload["pack_id"]:
            row["artifact_digest"] = payload["artifact_digest"]
    # No source paths, Contract/Operation changes, grants, or copied authority
    # enter this patch. Normal resolution must accept the exact retained graph;
    # changed operation semantics require the existing Profile wiring review.
    _validate_candidate(candidate)
    if canonical_digest(candidate) == current.profile_revision:
        current = store.require_current_profile(
            current.profile_id,
            expected_profile_revision=str(payload["expected_profile_revision"]),
            expected_store_generation=payload["expected_store_generation"],
        )
        return {
            "profile_id": current.profile_id,
            "profile_definition_revision": current.profile_revision,
            "activation_required": False,
        }
    updated = store.update_profile(
        str(payload["profile_id"]),
        candidate,
        expected_profile_revision=str(payload["expected_profile_revision"]),
        expected_store_generation=payload["expected_store_generation"],
    )
    return {
        "profile_id": updated.profile_id,
        "profile_definition_revision": updated.profile_revision,
        "activation_required": canonical_digest(candidate) != current.profile_revision,
    }


def _validate_candidate(candidate: Mapping[str, Any]) -> None:
    """Run normal resolution without grants, activation, or store writes."""
    from .authority.v4 import AuthorityStore
    from .bootstrap.profile_capture import (
        _authority_reference,
        _authority_snapshot_digest,
        _edge_key,
    )
    from .pack_control_v4 import catalog_with_admitted_pack_closure
    from .profile_catalog_v4 import bundle_lock_digest
    from .profile_runtime_port import require_profile_runtime

    runtime = require_profile_runtime()
    catalog = host_profile_catalog()
    pins = {
        str(row["pack_id"]): str(row["artifact_digest"])
        for row in candidate["packs"]
        if row.get("artifact_digest") is not None
    }
    catalog, _ = catalog_with_admitted_pack_closure(
        catalog,
        [str(row["pack_id"]) for row in candidate["packs"]],
        artifact_pins=pins,
    )
    catalog = runtime.catalog_with_profiles(
        catalog,
        {**catalog.profiles, str(candidate["profile_id"]): dict(candidate)},
    )
    with AuthorityStore(runtime_user_data_root() / "authority" / "v4.sqlite3") as authority:
        snapshot = _authority_snapshot_digest(authority, bundle_lock_digest(catalog))
        runtime.resolve_profile(
            catalog,
            str(candidate["profile_id"]),
            approved_artifact_digests={
                str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()
            },
            authority_snapshot_digest=snapshot,
            authority_bindings={
                _edge_key(edge): _authority_reference(edge, snapshot)
                for edge in candidate["requested_edges"]
            },
            security_epoch=authority.security_epoch,
        )
