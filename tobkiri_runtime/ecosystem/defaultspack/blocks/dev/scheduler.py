

from blocks.scheduler.status import run as scheduler_status


def run(input_data, context=None):
    return scheduler_status(input_data or {}, context or {})
