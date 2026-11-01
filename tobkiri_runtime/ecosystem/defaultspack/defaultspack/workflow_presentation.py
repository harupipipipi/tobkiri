"""Exact Workflow v4 request normalization for the HTTP capability surface.

The dynamic capability projection exposes the Workflow Pack's operations as
``pack.tobkiri_workflow_pack.<operation_id>`` targets.  This module is the
single source of truth for which payload keys each projected operation may
carry (``allowed_payload_keys``) and for the shape ``normalize_payload``
enforces before a request may reach the dispatch session and the RequestBroker.

Only the exact verified target identities below may normalize.  Everything
else still fails closed in ``normalize_payload``; there is no generic
``pack.*`` bypass.
"""

from __future__ import annotations

from collections.abc import Mapping
import re

from core_runtime.global_contracts.http_contract_dispatch import HTTPContractTarget

WORKFLOW_CONTRACT_ID = "tobkiri.workflow.v4"
WORKFLOW_STOP_CONTRACT_ID = "tobkiri.workflow.stop.v4"
WORKFLOW_EXECUTE_PRINCIPAL = "tobkiri.workflow.provider"
WORKFLOW_STOP_PRINCIPAL = "tobkiri.workflow.stop.provider"
WORKFLOW_OWNER_PACK_ID = "tobkiri_workflow_pack"

# Operation -> (required keys, admitted keys).  Key sets mirror the exact
# payload surface the Workflow Pack provider consumes; nothing else may pass.
_WORKFLOW_V4_KEYS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "definition.archive": (
        frozenset({"definition_id", "if_match"}),
        frozenset({"definition_id", "if_match"}),
    ),
    "definition.compile-preview": (
        frozenset({"document"}),
        frozenset({"document"}),
    ),
    "definition.create": (
        frozenset({"definition_id", "document"}),
        frozenset({"definition_id", "document"}),
    ),
    "definition.delete": (
        frozenset({"definition_id", "if_match"}),
        frozenset({"definition_id", "if_match"}),
    ),
    "definition.get": (
        frozenset({"definition_id"}),
        frozenset({"definition_id"}),
    ),
    "definition.list": (frozenset(), frozenset()),
    "definition.publish": (
        frozenset({"definition_id", "if_match"}),
        frozenset({"definition_id", "if_match"}),
    ),
    "definition.update": (
        frozenset({"definition_id", "document", "if_match"}),
        frozenset({"definition_id", "document", "if_match"}),
    ),
    "definition.validate": (
        frozenset({"document"}),
        frozenset({"document"}),
    ),
    "operation.palette": (frozenset(), frozenset()),
    "run.advance": (frozenset({"run_id"}), frozenset({"run_id"})),
    "run.cancel": (frozenset({"run_id"}), frozenset({"run_id"})),
    "run.create": (
        frozenset({"definition_id"}),
        frozenset({"definition_id", "revision_digest", "inputs", "occurrence_id", "run_id"}),
    ),
    "run.get": (frozenset({"run_id"}), frozenset({"run_id"})),
    # Root's read-only bounded observe surface: same finite keys as run.get.
    "run.observe": (frozenset({"run_id"}), frozenset({"run_id"})),
    "run.pause": (frozenset({"run_id"}), frozenset({"run_id"})),
    "run.reconcile-recovery": (frozenset({"run_id"}), frozenset({"run_id"})),
    "run.resume": (frozenset({"run_id"}), frozenset({"run_id"})),
    "run.step.execute": (
        frozenset({"run_id", "step_id"}),
        frozenset({"run_id", "step_id"}),
    ),
    "run.step.resume": (
        frozenset({"run_id", "step_id"}),
        frozenset({"run_id", "step_id"}),
    ),
    "run.step.retry": (
        frozenset({"run_id", "step_id"}),
        frozenset({"run_id", "step_id"}),
    ),
}

_WORKFLOW_STOP_KEYS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "run.stop": (frozenset({"run_id"}), frozenset({"run_id"})),
}

WORKFLOW_V4_ALLOWED_KEYS: Mapping[str, frozenset[str]] = {
    operation: keys[1] for operation, keys in _WORKFLOW_V4_KEYS.items()
}
WORKFLOW_STOP_ALLOWED_KEYS: Mapping[str, frozenset[str]] = {
    operation: keys[1] for operation, keys in _WORKFLOW_STOP_KEYS.items()
}


def _identity(
    contract_id: str,
    operation_id: str,
    principal: str,
) -> tuple[str, str, str, str, str]:
    """Build the exact projected identity tuple for one Workflow operation."""

    return (
        f"pack.{WORKFLOW_OWNER_PACK_ID}.{operation_id}",
        contract_id,
        operation_id,
        principal,
        principal,
    )


_WORKFLOW_IDENTITIES: Mapping[
    tuple[str, str, str, str, str],
    tuple[frozenset[str], frozenset[str]],
] = {
    **{
        _identity(WORKFLOW_CONTRACT_ID, operation, WORKFLOW_EXECUTE_PRINCIPAL): keys
        for operation, keys in _WORKFLOW_V4_KEYS.items()
    },
    **{
        _identity(WORKFLOW_STOP_CONTRACT_ID, operation, WORKFLOW_STOP_PRINCIPAL): keys
        for operation, keys in _WORKFLOW_STOP_KEYS.items()
    },
}

WORKFLOW_TARGET_IDENTITIES: frozenset[tuple[str, str, str, str, str]] = frozenset(
    _WORKFLOW_IDENTITIES
)

_STRING_FIELDS = frozenset(
    {"definition_id", "revision_digest", "if_match", "run_id", "step_id", "occurrence_id"}
)
_MAPPING_FIELDS = frozenset({"document", "inputs"})


def is_workflow_target(target: HTTPContractTarget) -> bool:
    """Report whether one verified target is a Workflow Pack operation."""

    return target.owner_pack_id == WORKFLOW_OWNER_PACK_ID and (
        target.contribution_id,
        target.contract_id,
        target.operation_id,
        target.provider_id,
        target.function_id,
    ) in _WORKFLOW_IDENTITIES


def normalize_workflow_request(
    target: HTTPContractTarget,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    """Validate one Workflow operation request against its exact key surface.

    The payload passes through verbatim once admitted; Workflow identity and
    authority come from the captured dispatch session and the Broker, never
    from client-supplied fields.
    """

    identity = (
        target.contribution_id,
        target.contract_id,
        target.operation_id,
        target.provider_id,
        target.function_id,
    )
    if target.owner_pack_id != WORKFLOW_OWNER_PACK_ID:
        raise ValueError("Workflow operation requires the Workflow Pack owner")
    bounds = _WORKFLOW_IDENTITIES.get(identity)
    if bounds is None:
        raise ValueError("Workflow operation is not an approved target")
    required, allowed = bounds
    keys = set(payload)
    if keys - allowed or not required <= keys:
        raise ValueError("Workflow operation request is invalid")
    for field in _STRING_FIELDS & keys:
        value = payload[field]
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or value.strip() != value
        ):
            raise ValueError(f"{field} is required")
    if "revision_digest" in keys and (
        not isinstance(payload["revision_digest"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", payload["revision_digest"]) is None
    ):
        raise ValueError("revision_digest must be an exact SHA-256 digest")
    for field in _MAPPING_FIELDS & keys:
        if not isinstance(payload[field], Mapping):
            # ValueError is the presentation contract: the server maps it to
            # a 400 denial; TypeError would escape as an internal error.
            raise ValueError(f"{field} must be an object")  # noqa: TRY004
    return dict(payload)


__all__ = [
    "WORKFLOW_CONTRACT_ID",
    "WORKFLOW_EXECUTE_PRINCIPAL",
    "WORKFLOW_OWNER_PACK_ID",
    "WORKFLOW_STOP_ALLOWED_KEYS",
    "WORKFLOW_STOP_CONTRACT_ID",
    "WORKFLOW_STOP_PRINCIPAL",
    "WORKFLOW_TARGET_IDENTITIES",
    "WORKFLOW_V4_ALLOWED_KEYS",
    "is_workflow_target",
    "normalize_workflow_request",
]
