

from blocks._common import ok
from domain.scheduler.job_store import SchedulerJobStore


def run(input_data, context=None):
    return ok({"jobs": SchedulerJobStore().list()})
