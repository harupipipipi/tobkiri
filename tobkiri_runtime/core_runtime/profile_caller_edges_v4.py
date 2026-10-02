"""Resolve only exact Host-selected operation principals as Profile callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from core_runtime.authority.v4 import AuthorityDenied, FunctionPrincipal
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.profile_edges import caller_operation_selector


@dataclass(frozen=True)
class SelectedCallerOperationV4:
    """A verified selected operation, never a caller-supplied source claim."""

    contract_id: str
    principal: FunctionPrincipal


def resolve_selected_caller(
    edge: Mapping[str, Any], candidates: Iterable[SelectedCallerOperationV4]
) -> FunctionPrincipal:
    """Select one existing principal, with no fallback to sibling operations.

    Supply only selected plan target operations and operations of the selected
    Shell. Merely being present in a verified artifact is insufficient.
    Repeated incoming edges for the same exact operation are deduplicated.
    """
    try:
        selector = caller_operation_selector(edge)
    except ProtocolError as exc:
        raise AuthorityDenied(str(exc)) from exc
    matches: dict[str, FunctionPrincipal] = {}
    for candidate in candidates:
        principal = candidate.principal
        if principal.function_id != edge.get("caller_function_id"):
            continue
        if selector and (
            candidate.contract_id != selector["caller_contract_id"]
            or principal.operation_id != selector["caller_operation_id"]
            or (
                "caller_contract_revision_digest" in selector
                and principal.contract_revision_digest
                != selector["caller_contract_revision_digest"]
            )
        ):
            continue
        matches[principal.principal_id] = principal
    if len(matches) != 1:
        raise AuthorityDenied("Profile edge caller does not identify one selected principal")
    return next(iter(matches.values()))
