"""Canonical requested-scope handling for Profile operation edges."""

from __future__ import annotations

from typing import Any, Mapping

from .canonical import canonical_digest
from .errors import ProtocolError

_ALLOWED_KEYS = {
    "capability",
    "semantics_digest",
    "dimensions",
    "quotas",
    "exact_request_digest",
    "opaque",
}


def normalize_requested_scope_template(
    template: Mapping[str, Any],
    *,
    contract_id: str,
    operation_id: str,
    semantics_digest: str,
) -> dict[str, Any]:
    """Return the closed, exact scope committed for one Profile edge.

    An empty template means the minimum useful scope: invocation of only the
    edge's exact Contract operation.  It never means an unbounded scope.
    """

    if not isinstance(template, Mapping):
        raise ProtocolError("requested scope template must be an object")
    unknown = set(template) - _ALLOWED_KEYS
    if unknown:
        raise ProtocolError(
            "requested scope template contains unknown fields: "
            + ", ".join(sorted(unknown))
        )
    capability = template.get("capability", "operation.invoke")
    if capability != "operation.invoke":
        raise ProtocolError("requested scope capability must be operation.invoke")
    supplied_semantics = template.get("semantics_digest", semantics_digest)
    if supplied_semantics != semantics_digest:
        raise ProtocolError("requested scope semantics do not match the Contract revision")
    raw_dimensions = template.get("dimensions", {})
    raw_quotas = template.get("quotas", {})
    if not isinstance(raw_dimensions, Mapping) or not isinstance(raw_quotas, Mapping):
        raise ProtocolError("requested scope dimensions and quotas must be objects")
    dimensions: dict[str, list[str]] = {}
    for key, raw_values in raw_dimensions.items():
        if not isinstance(key, str) or not key:
            raise ProtocolError("requested scope dimension names must be non-empty")
        if not isinstance(raw_values, list) or not raw_values or any(
            not isinstance(value, str) or not value for value in raw_values
        ):
            raise ProtocolError("requested scope dimension values must be strings")
        values = sorted(set(raw_values))
        if "*" in values:
            raise ProtocolError("requested scope templates cannot use wildcards")
        dimensions[key] = values
    for key, exact in (("contract", contract_id), ("operation", operation_id)):
        supplied = dimensions.get(key)
        if supplied is not None and supplied != [exact]:
            raise ProtocolError(f"requested scope {key} does not match its Profile edge")
        dimensions[key] = [exact]
    quotas: dict[str, int] = {}
    for key, value in raw_quotas.items():
        if (
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ProtocolError("requested scope quotas must be non-negative integers")
        quotas[key] = value
    exact_request_digest = template.get("exact_request_digest")
    opaque = template.get("opaque", False)
    if exact_request_digest is not None or opaque is not False:
        raise ProtocolError("Profile requested scopes must use declarative semantics")
    return {
        "capability": "operation.invoke",
        "semantics_digest": semantics_digest,
        "dimensions": {key: dimensions[key] for key in sorted(dimensions)},
        "quotas": {key: quotas[key] for key in sorted(quotas)},
        "exact_request_digest": None,
        "opaque": False,
    }


def requested_scope_digest(template: Mapping[str, Any]) -> str:
    """Digest a previously normalized requested scope."""

    return canonical_digest(dict(template))


__all__ = ["normalize_requested_scope_template", "requested_scope_digest"]
