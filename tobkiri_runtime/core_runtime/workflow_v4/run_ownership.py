"""Immutable run ownership derived from authenticated Host presentation context."""
from __future__ import annotations

import re
from typing import Any, Mapping

from .models import WorkflowDenied, digest

class WorkflowRunOwnershipDenied(WorkflowDenied):
    """Finite public denial for a missing or mismatched immutable run owner."""

    code = "WORKFLOW_RUN_OWNER_UNAVAILABLE"


_OWNER_FIELD = "owner_scope_digest"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def validate_owner_scope_digest(value: str | None) -> None:
    """Permit unowned standalone engines or an exact Host-generated digest."""
    if value is not None and (
        not isinstance(value, str) or _DIGEST.fullmatch(value) is None
    ):
        raise WorkflowDenied("Workflow run owner binding is invalid")


def captured_run_owner_scope_digest(invocation: Any) -> str:
    """Bind stable presentation identity, never a request lease or wire field."""
    invocation.assert_current()
    context = invocation.envelope.context
    fields = {
        "profile_id": context.profile_id,
        "activation_id": context.activation_id,
        "activation_digest": context.activation_digest,
        "plan_digest": context.plan_digest,
        "security_epoch": context.security_epoch,
        "principal_id": invocation.presentation_owner_principal_id,
        "session_id": invocation.presentation_owner_session_id,
    }
    if any(
        not isinstance(value, str) or not value
        for key, value in fields.items() if key != "security_epoch"
    ) or type(fields["security_epoch"]) is not int:
        raise WorkflowDenied("Workflow run owner context is unavailable")
    return digest({"kind": "io.tobkiri.workflow-run-owner.v1", **fields})


def assert_run_owner(
    run: Mapping[str, Any], owner_scope_digest: str | None,
) -> None:
    """Deny legacy adoption or foreign-owner mutation without changing state."""
    validate_owner_scope_digest(owner_scope_digest)
    stored = run.get(_OWNER_FIELD)
    if owner_scope_digest is None and stored is None:
        return
    if stored is None:
        raise WorkflowRunOwnershipDenied(
            "This Workflow run predates verified ownership and cannot be changed. "
            "Inspect its recorded outcomes before creating a new run; "
            "do not repeat effects that still need reconciliation."
        )
    if owner_scope_digest is None or stored != owner_scope_digest:
        raise WorkflowRunOwnershipDenied(
            "This Workflow run belongs to another authenticated session or capture. "
            "Return to its original signed-in window if it is still available. "
            "Otherwise inspect its recorded outcomes before creating a new run."
        )
