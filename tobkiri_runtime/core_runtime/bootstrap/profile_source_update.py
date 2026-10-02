"""Build additive Profile review proposals without changing durable state."""

from copy import deepcopy
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.profile_edges import profile_edge_identity

from ..profile_definition_store_v4 import ProfileDefinitionStoreConflict


def profile_binding_renewal_required(
    plan: Mapping[str, Any], catalog: Any
) -> bool:
    """Return whether a verified plan binds a superseded sealed Pack artifact.

    This identifies only digest renewal for operation bindings already present
    in the active plan.  It does not add Packs, operations, or authority and
    the resulting candidate still requires the normal exact confirmation.
    """

    bindings = plan.get("bindings")
    packs = getattr(catalog, "packs", None)
    if not isinstance(bindings, list) or not isinstance(packs, Mapping):
        raise ProfileDefinitionStoreConflict(
            "artifact binding renewal requires a verified active plan"
        )
    renewal_required = False
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise ProfileDefinitionStoreConflict(
                "artifact binding renewal contains an invalid binding"
            )
        pack_id = binding.get("pack_id")
        artifact_digest = binding.get("artifact_digest")
        manifest = packs.get(pack_id) if isinstance(pack_id, str) else None
        pack = manifest.get("pack") if isinstance(manifest, Mapping) else None
        current_digest = pack.get("artifact_digest") if isinstance(pack, Mapping) else None
        if not isinstance(artifact_digest, str) or not isinstance(current_digest, str):
            raise ProfileDefinitionStoreConflict(
                "artifact binding renewal cannot verify the selected Pack"
            )
        renewal_required = renewal_required or artifact_digest != current_digest
    return renewal_required


def interrupted_source_update_predecessor(
    registry: Mapping[str, Any],
    registered: Mapping[str, Any],
    verified_definition_digest: str,
    source: Mapping[str, Any],
    *,
    successor_required: bool,
) -> dict[str, Any]:
    """Recover a review base, never authority, from one exact pending successor.

    A definition can commit before activation does. Recognize only its direct
    immutable predecessor and the same packaged proposal; unrelated edits are
    conflicts. The caller must still require normal confirmation and active
    predecessor CAS before granting the proposed operations.
    """
    current_digest = canonical_digest(registered)
    for entry in registry["profiles"]:
        if (
            entry["profile_id"] != registered["profile_id"]
            or entry["tombstone"]
            or entry["current_revision"] != current_digest
        ):
            continue
        revisions = {row["profile_revision"]: row for row in entry["revisions"]}
        current = revisions.get(current_digest)
        previous = revisions.get(verified_definition_digest)
        if (
            current is None or previous is None
            or current["parent_revision"] != verified_definition_digest
            or canonical_digest(previous["profile"]) != verified_definition_digest
        ):
            break
        for additions in (False, True):
            candidate = deepcopy(previous["profile"])
            if successor_required:
                candidate["shell"] = deepcopy(source["shell"])
            try:
                if successor_required or additions:
                    candidate = profile_scope_successor(candidate, source)
                if additions:
                    candidate = profile_source_additions(candidate, source)
            except ProfileDefinitionStoreConflict:
                continue
            if canonical_digest(candidate) == current_digest:
                return deepcopy(previous["profile"])
        break
    raise ProfileDefinitionStoreConflict(
        "bootstrap review does not match a verified interrupted source update"
    )


def profile_scope_successor(
    current: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Propose only packaged semantics-pin changes for otherwise identical edges.

    Call only after verifying the active predecessor and packaged successor.
    This does not authorize execution: the resulting definition needs a fresh
    exact-confirmation activation. Custom scopes and authority modes are never
    replaced, and absent source edges are retained for normal resolution checks.
    """
    source_edges = {
        profile_edge_identity(edge): edge
        for edge in source["requested_edges"]
    }
    if len(source_edges) != len(source["requested_edges"]):
        raise ProfileDefinitionStoreConflict("scope successor contains duplicate edges")
    result = deepcopy(dict(current))
    for edge in result["requested_edges"]:
        replacement = source_edges.get(profile_edge_identity(edge))
        if replacement is None:
            continue
        proposed = deepcopy(dict(replacement))
        old_scope = deepcopy(edge["requested_scope_template"])
        new_scope = proposed["requested_scope_template"]
        old_digest = old_scope.pop("semantics_digest", None)
        new_digest = new_scope.pop("semantics_digest", None)
        old_edge = {**edge, "requested_scope_template": old_scope}
        if old_edge == proposed and old_digest != new_digest and new_digest is not None:
            edge["requested_scope_template"]["semantics_digest"] = new_digest
    return result


def profile_source_additions(
    current: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge validated source additions while rejecting conflicting definitions.

    Inputs must be a registered Profile and its verified packaged counterpart.
    The result is only a proposal: callers must resolve and confirm it through
    the normal Authority ceremony before persisting or activating it. Shell
    successor selection remains the responsibility of the verified caller.
    """
    for field in ("profile_id", "base", "shell"):
        if current[field] != source[field]:
            raise ProfileDefinitionStoreConflict(f"additive Profile update cannot replace {field}")

    result = deepcopy(dict(current))
    collections = (
        ("packs", ("pack_id",)),
        (
            "requested_edges",
            (
                "caller_function_id",
                "target_provider_id",
                "contract_id",
                "operation_id",
            ),
        ),
    )
    for field, identity_fields in collections:
        existing: dict[tuple[str, ...], Mapping[str, Any]] = {}
        for rows, append in ((current[field], False), (source[field], True)):
            seen: set[tuple[str, ...]] = set()
            for row in rows:
                identity = (
                    profile_edge_identity(row) if field == "requested_edges"
                    else tuple(row[key] for key in identity_fields)
                )
                if identity in seen:
                    raise ProfileDefinitionStoreConflict(
                        f"additive Profile update contains duplicate {field}"
                    )
                seen.add(identity)
                prior = existing.get(identity)
                if prior is not None and prior != row:
                    raise ProfileDefinitionStoreConflict(
                        f"additive Profile update conflicts with existing {field}"
                    )
                if prior is None:
                    existing[identity] = row
                    if append:
                        result[field].append(deepcopy(row))
    return result
