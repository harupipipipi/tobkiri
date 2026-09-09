"""Build additive Profile review proposals without changing durable state."""

from copy import deepcopy
from typing import Any, Mapping

from ..profile_definition_store_v4 import ProfileDefinitionStoreConflict


def profile_scope_successor(
    current: Mapping[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    """Propose only packaged semantics-pin changes for otherwise identical edges.

    Call only after verifying the active predecessor and packaged successor.
    This does not authorize execution: the resulting definition needs a fresh
    exact-confirmation activation. Custom scopes and authority modes are never
    replaced, and absent source edges are retained for normal resolution checks.
    """
    identity_fields = (
        "caller_function_id", "target_provider_id", "contract_id", "operation_id"
    )
    source_edges = {
        tuple(edge[key] for key in identity_fields): edge
        for edge in source["requested_edges"]
    }
    if len(source_edges) != len(source["requested_edges"]):
        raise ProfileDefinitionStoreConflict("scope successor contains duplicate edges")
    result = deepcopy(dict(current))
    for edge in result["requested_edges"]:
        replacement = source_edges.get(tuple(edge[key] for key in identity_fields))
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
                identity = tuple(row[key] for key in identity_fields)
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
