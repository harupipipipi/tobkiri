"""block: blocks.agent.scheduler.history

Retrieve execution history for a scheduled agent.

input_data:
    schedule_id : str  (required)
    limit       : int  (optional, default 50)
    offset      : int  (optional, default 0)
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

    limit = 50
    offset = 0
    if isinstance(input_data, dict):
        raw_limit = input_data.get("limit")
        if isinstance(raw_limit, int) and raw_limit > 0:
            limit = min(raw_limit, 200)
        raw_offset = input_data.get("offset")
        if isinstance(raw_offset, int) and raw_offset >= 0:
            offset = raw_offset

    try:
        scheduler = Scheduler(settings_owner=settings_owner)
        sched = scheduler.get_schedule(schedule_id)
    except Exception as exc:
        return error("failed to load schedule: " + str(exc), "INTERNAL_ERROR")

    if sched is None:
        return error("schedule not found: " + schedule_id, "NOT_FOUND")

    try:
        result = scheduler.get_history(schedule_id, limit=limit, offset=offset)
    except Exception as exc:
        return error("failed to load history: " + str(exc), "INTERNAL_ERROR")

    return ok(result)
