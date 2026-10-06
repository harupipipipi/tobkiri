"""Authenticated finite saved-tool ancestry and private nested session binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
from tobkiri_host.models import RequestContext

SAVED_TOOL_CAPTURE_FIELDS = (
    "profile_id",
    "profile_revision",
    "activation_id",
    "activation_digest",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
)


@dataclass(frozen=True)
class NestedToolBinding:
    """Private nested session retained by the production Host closure."""

    context: RequestContext
    ceiling: Mapping[str, Any]
    caller_publisher_lineage: str
    cancellation_proof: Any
    release: Callable[[], None]


def saved_tool_owner(invocation: Any) -> Any:
    """Return the authenticated live saved root for the finite executor chain."""
    invocation.assert_current()
    envelope = invocation.envelope
    if (envelope.contract_id, envelope.operation_id) != (
        "tobkiri.service.tool.execute.v1",
        "rumi_tool_local_executor_pack.tool-local-execute",
    ):
        raise PermissionError("saved tool executor ancestry is unavailable")
    parents: list[Any] = []
    scope = invocation.parent_invocation
    while scope is not None:
        if len(parents) >= 16 or any(scope is prior for prior in parents):
            raise PermissionError("saved tool ancestry is unavailable")
        scope.assert_current()
        if any(
            getattr(scope.envelope.context, key) != getattr(envelope.context, key)
            for key in SAVED_TOOL_CAPTURE_FIELDS
        ):
            raise PermissionError("saved tool ancestry capture changed")
        parents.append(scope)
        scope = scope.parent
    operations = [(item.envelope.contract_id, item.envelope.operation_id) for item in parents]
    if (
        not operations
        or operations[0] != ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
        or ("conversation.saved-turn.v1", "saved_complete") not in operations[1:]
    ):
        raise PermissionError("saved tool ancestry is unavailable")
    root = parents[-1].envelope.context
    if (
        root.caller_principal.value != invocation.presentation_owner_principal_id
        or root.caller_session_id != invocation.presentation_owner_session_id
    ):
        raise PermissionError("saved tool presentation owner changed")
    return root
