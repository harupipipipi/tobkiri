import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.scheduler.scheduler import Scheduler


def run(input_data, context=None, *, settings_owner=None):
    job_id = input_data.get("job_id") or input_data.get("id")
    if not job_id:
        return error("job_id is required", "INVALID_INPUT")
    result = Scheduler(settings_owner=settings_owner).run_now(job_id)
    if result.get("status") == "error":
        if "disabled" in str(result.get("error", "")).lower():
            return error(result.get("error", "scheduler disabled"), "PERMISSION_DENIED")
        return error(result.get("error", "job not found"), "NOT_FOUND")
    return ok(result)
