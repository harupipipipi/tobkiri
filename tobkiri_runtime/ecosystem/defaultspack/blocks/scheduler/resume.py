

from blocks.scheduler.update import run as update_run


def run(input_data, context=None):
    data = dict(input_data or {})
    data["updates"] = {"enabled": True}
    return update_run(data, context)
