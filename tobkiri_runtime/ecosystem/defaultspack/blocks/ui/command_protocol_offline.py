import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.frontend.command_protocol import CommandProtocolRegistry
from domain.frontend.offline_queue import OfflineQueueError


def run(input_data, context):
    payload = input_data if isinstance(input_data, dict) else {}
    action = str(payload.get("action") or "enqueue").strip()
    registry = CommandProtocolRegistry()
    try:
        owner_key = registry.owner_key(payload, context or {})
        if action == "enqueue":
            return ok(registry.enqueue_offline(payload, context or {}))
        if action == "pending":
            limit = int(payload.get("limit") or 100)
            return ok(
                {
                    "api_version": "tobkiri.commands/v1",
                    "status": "succeeded",
                    "queue": registry.offline.pending(
                        limit=limit,
                        owner_key=owner_key,
                    ),
                }
            )
        if action == "replay":
            limit = int(payload.get("limit") or 100)
            return ok(
                registry.replay_offline(
                    limit=limit,
                    owner_key=owner_key,
                )
            )
        if action == "cancel":
            cancellation = registry.offline.cancel(
                str(payload.get("queue_id") or ""),
                owner_key=owner_key,
            )
            return ok(
                {
                    "api_version": "tobkiri.commands/v1",
                    "status": cancellation["status"],
                    "too_late": cancellation["too_late"],
                    "queue": cancellation.get("queue"),
                }
            )
    except (TypeError, ValueError, OfflineQueueError) as exc:
        return error(str(exc), "OFFLINE_QUEUE_REJECTED")
    return error("unsupported offline queue action", "INVALID_INPUT")
