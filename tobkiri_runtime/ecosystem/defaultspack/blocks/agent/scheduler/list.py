"""block: blocks.agent.scheduler.list

List all scheduled agent executions.

input_data:
    status : str  (optional) — filter by status: "active" | "paused" | "completed"
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from blocks._common import ok, error
from domain.agent.scheduler import Scheduler


def run(input_data, context, *, settings_owner=None):
    status_filter = None
    if isinstance(input_data, dict):
        status_filter = input_data.get("status")

    try:
        scheduler = Scheduler(settings_owner=settings_owner)
        schedules = scheduler.list_schedules(status_filter=status_filter)
    except Exception as exc:
        return error("failed to list schedules: " + str(exc), "INTERNAL_ERROR")

    return ok({"schedules": schedules, "total": len(schedules)})
