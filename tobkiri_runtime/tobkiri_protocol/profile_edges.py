"""Exact caller selectors and stable identities for signed Profile edges."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .canonical import canonical_digest
from .errors import ProtocolError
from .ids import (
    validate_artifact_digest,
    validate_canonical_id,
    validate_contract_id,
)

CALLER_SELECTOR_FIELDS = (
    "caller_contract_id",
    "caller_operation_id",
    "caller_contract_revision_digest",
)


def caller_operation_selector(edge: Mapping[str, Any]) -> dict[str, str]:
    """Validate a finite optional selector without creating caller authority.

    The Contract and operation must be supplied together. An optional revision
    only narrows that pair; omission preserves the legacy unambiguous rule.
    """
    present = [field for field in CALLER_SELECTOR_FIELDS if field in edge]
    if not present:
        return {}
    if not all(field in edge for field in CALLER_SELECTOR_FIELDS[:2]):
        raise ProtocolError("caller Contract and operation must be supplied together")
    selector = {
        "caller_contract_id": validate_contract_id(
            edge["caller_contract_id"], field="caller_contract_id"
        ),
        "caller_operation_id": validate_canonical_id(
            edge["caller_operation_id"], field="caller_operation_id"
        ),
    }
    if "caller_contract_revision_digest" in edge:
        selector["caller_contract_revision_digest"] = validate_artifact_digest(
            edge["caller_contract_revision_digest"],
            field="caller_contract_revision_digest",
        )
    return selector


def captured_edge_identity(edge: Mapping[str, Any]) -> tuple[str, ...]:
    """Key one selected caller-to-operation edge, retaining legacy key bytes."""
    key = tuple(
        str(edge.get(field) or "")
        for field in ("caller_function_id", "contract_id", "operation_id")
    )
    selector = caller_operation_selector(edge)
    if not selector:
        return key
    return key + tuple(selector.get(field, "") for field in CALLER_SELECTOR_FIELDS)


def profile_edge_identity(edge: Mapping[str, Any]) -> tuple[str, ...]:
    """Key a Profile edge or plan binding without merging sibling callers."""
    target = edge.get("target_provider_id")
    principal = edge.get("function_principal")
    if target is None and isinstance(principal, Mapping):
        target = principal.get("function_id")
    key = captured_edge_identity(edge)
    return (key[0], str(target or ""), *key[1:])


def profile_edge_key(edge: Mapping[str, Any]) -> str:
    """Return the authority-reference key; old four-part keys stay unchanged."""
    return "|".join(profile_edge_identity(edge))


def require_profile_edge_bindings(
    edges: Sequence[Mapping[str, Any]], bindings: Sequence[Mapping[str, Any]]
) -> None:
    """Require exact signed Profile-to-plan lineage, including caller selectors."""
    requested = {profile_edge_identity(edge): edge for edge in edges}
    selected = {profile_edge_identity(binding): binding for binding in bindings}
    if (
        len(requested) != len(edges)
        or len(selected) != len(bindings)
        or requested.keys() != selected.keys()
    ):
        raise ProtocolError("ResolvedPlan does not exactly match requested caller edges")
    for identity, edge in requested.items():
        binding = selected[identity]
        if (
            edge.get("authority_reference") != binding.get("authority_reference")
            or canonical_digest(edge["requested_scope_template"])
            != binding.get("requested_scope_digest")
            or edge.get("authority_mode", "profile_grant")
            != binding.get("authority_mode", "profile_grant")
        ):
            raise ProtocolError("ResolvedPlan caller edge binding is stale")
