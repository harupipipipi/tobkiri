

from blocks._common import error, ok
from domain.scheduler.job_store import SchedulerJobStore
from domain.scheduler.security import SchedulerPolicyError, validate_scheduler_enabled


def run(input_data, context=None):
    job_id = input_data.get("job_id") or input_data.get("id")
    if not job_id:
        return error("job_id is required", "INVALID_INPUT")
    updates = input_data.get("updates", {})
    if not isinstance(updates, dict):
        updates = {key: value for key, value in input_data.items() if key not in {"job_id", "id"}}
    try:
        validate_scheduler_enabled()
        job = SchedulerJobStore().update(job_id, updates)
    except SchedulerPolicyError as exc:
        return error(str(exc), "PERMISSION_DENIED")
    except ValueError as exc:
        return error(str(exc), "INVALID_INPUT")
    if not job:
        return error("job not found", "NOT_FOUND")
    return ok(job)
