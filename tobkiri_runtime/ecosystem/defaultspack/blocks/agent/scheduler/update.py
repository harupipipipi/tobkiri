"""block: blocks.agent.scheduler.update

Update an existing scheduled agent execution.

input_data:
    schedule_id     : str   (required)
    name            : str   (optional)
    description     : str   (optional)
    schedule_type   : str   (optional) — "interval" | "cron" | "once"
    schedule_config : dict  (optional) — new schedule configuration
    task            : dict  (optional) — partial or full task update
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from blocks._common import ok, error
from domain.agent.scheduler import Scheduler


def run(input_data, context, *, settings_owner=None):
    if not isinstance(input_data, dict):
        return error("input_data must be a JSON object")

    schedule_id = input_data.get("schedule_id")
    if not schedule_id:
        return error("schedule_id is required")

    updates = {}
    if "name" in input_data:
        updates["name"] = input_data["name"]
    if "description" in input_data:
        updates["description"] = input_data["description"]
    if "schedule_type" in input_data:
        updates["type"] = input_data["schedule_type"]
    if "schedule_config" in input_data:
        updates["config"] = input_data["schedule_config"]
    if "task" in input_data:
        updates["task"] = input_data["task"]

    if not updates:
        return error("no update fields provided")

    try:
        scheduler = Scheduler(settings_owner=settings_owner)
        schedule = scheduler.update_schedule(schedule_id, updates)
    except ValueError as exc:
        return error(str(exc), "VALIDATION_ERROR")
    except Exception as exc:
        return error("failed to update schedule: " + str(exc), "INTERNAL_ERROR")

    if schedule is None:
        return error("schedule not found: " + schedule_id, "NOT_FOUND")

    return ok(schedule)
