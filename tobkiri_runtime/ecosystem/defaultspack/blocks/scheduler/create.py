

from blocks._common import error, ok
from domain.scheduler.job_store import SchedulerJobStore
from domain.scheduler.security import SchedulerPolicyError, validate_scheduler_enabled


def run(input_data, context=None):
    try:
        validate_scheduler_enabled()
        return ok(SchedulerJobStore().create(input_data if isinstance(input_data, dict) else {}))
    except SchedulerPolicyError as exc:
        return error(str(exc), "PERMISSION_DENIED")
    except ValueError as exc:
        return error(str(exc), "INVALID_INPUT")
