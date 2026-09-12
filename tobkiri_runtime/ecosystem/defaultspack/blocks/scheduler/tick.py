import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.scheduler.scheduler import Scheduler


def run(input_data, context=None, *, settings_owner=None):
    result = Scheduler(settings_owner=settings_owner).tick()
    if result.get("status") == "error":
        return error(result.get("error", "scheduler disabled"), "PERMISSION_DENIED")
    return ok(result)
