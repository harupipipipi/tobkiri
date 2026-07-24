import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.frontend.command_protocol import CommandProtocolRegistry
from domain.frontend.invocation_events import InvocationEventError


def run(input_data, context):
    payload = input_data if isinstance(input_data, dict) else {}
    invocation_id = str(payload.get("invocation_id") or "").strip()
    pending_action = str(payload.get("action") or "") == "pending_approvals"
    if not invocation_id and not pending_action:
        return error("invocation_id is required", "INVALID_INPUT")
    try:
        after_sequence = int(payload.get("after_sequence") or 0)
        limit = int(payload.get("limit") or 500)
        registry = CommandProtocolRegistry()
        owner_key = registry.owner_key(payload, context or {})
        if pending_action:
            for pending in registry.events.pending_approvals(owner_key=owner_key):
                registry.reconcile_approval(
                    {"invocation_id": pending["invocation_id"]},
                    context or {},
                )
            return ok(
                {
                    "api_version": "tobkiri.commands/v1",
                    "pending_approvals": registry.events.pending_approvals(
                        owner_key=owner_key
                    ),
                }
            )
        registry.reconcile_approval(payload, context or {})
        events = registry.events.resume(
            invocation_id,
            after_sequence=after_sequence,
            limit=limit,
            owner_key=owner_key,
        )
    except (TypeError, ValueError, InvocationEventError) as exc:
        return error(str(exc), "INVALID_INPUT")
    return ok(
        {
            "api_version": "tobkiri.commands/v1",
            "events": events,
            "snapshot": registry.events.snapshot(
                invocation_id,
                owner_key=owner_key,
            ),
        }
    )
