"""block: blocks.agent.scheduler.create

Create a new scheduled agent execution.

input_data:
    name            : str   (optional) — human-readable name
    description     : str   (optional)
    schedule_type   : str   (required) — "interval" | "cron" | "once"
    schedule_config : dict  (required)
        interval: {value: int, unit: "seconds"|"minutes"|"hours"}
        cron:     {expression: "0 9 * * *"}
        once:     {run_at: "2025-03-01T09:00:00Z"}
    task            : dict  (required)
        message         : str   (required) — instruction text for the AI
        model           : str   (optional, default "default")
        conversation_id : str   (optional) — existing conversation or null for new
        timeout         : int   (optional, default 300)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from blocks._common import ok, error
from domain.agent.scheduler import Scheduler


def run(input_data, context, *, settings_owner=None):
    if not isinstance(input_data, dict):
        return error("input_data must be a JSON object")

    schedule_type = input_data.get("schedule_type")
    if not schedule_type:
        return error("schedule_type is required (interval | cron | once)")

    schedule_config = input_data.get("schedule_config")
    if not isinstance(schedule_config, dict):
        return error("schedule_config is required and must be an object")

    task = input_data.get("task")
    if not isinstance(task, dict):
        return error("task is required and must be an object")

    if not task.get("message"):
        return error("task.message is required")

    name = input_data.get("name", "")
    description = input_data.get("description", "")

    try:
        scheduler = Scheduler(settings_owner=settings_owner)
        schedule = scheduler.create_schedule(
            schedule_type=schedule_type,
            task_config=task,
            schedule_config=schedule_config,
            name=name,
            description=description,
        )
    except ValueError as exc:
        return error(str(exc), "VALIDATION_ERROR")
    except Exception as exc:
        return error("failed to create schedule: " + str(exc), "INTERNAL_ERROR")

    return ok(schedule)
