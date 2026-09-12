"""block: blocks.agent.scheduler.get

Get details of a specific scheduled agent execution.

input_data:
    schedule_id : str  (required)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from blocks._common import ok, error
from domain.agent.scheduler import Scheduler


def run(input_data, context, *, settings_owner=None):
    schedule_id = input_data.get("schedule_id") if isinstance(input_data, dict) else None
    if not schedule_id:
        return error("schedule_id is required")

    try:
        scheduler = Scheduler(settings_owner=settings_owner)
        schedule = scheduler.get_schedule(schedule_id)
    except Exception as exc:
        return error("failed to get schedule: " + str(exc), "INTERNAL_ERROR")

    if schedule is None:
        return error("schedule not found: " + schedule_id, "NOT_FOUND")

    return ok(schedule)
