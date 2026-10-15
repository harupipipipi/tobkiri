

from blocks._common import error, ok
from domain.scheduler.job_store import SchedulerJobStore


def run(input_data, context=None):
    job_id = input_data.get("job_id") or input_data.get("id")
    if not job_id:
        return error("job_id is required", "INVALID_INPUT")
    return ok({"deleted": SchedulerJobStore().delete(job_id)})
