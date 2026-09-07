"""Build additive Profile review proposals without changing durable state."""

from copy import deepcopy
from typing import Any, Mapping

from ..profile_definition_store_v4 import ProfileDefinitionStoreConflict


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
