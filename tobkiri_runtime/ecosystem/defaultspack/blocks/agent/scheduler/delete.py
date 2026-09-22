"""block: blocks.agent.scheduler.delete

Delete a scheduled agent execution.

input_data:
    schedule_id : str  (required)
"""



from blocks._common import ok, error
from domain.agent.scheduler import Scheduler


def run(input_data, context, *, settings_owner=None):
    schedule_id = input_data.get("schedule_id") if isinstance(input_data, dict) else None
    if not schedule_id:
        return error("schedule_id is required")

    try:
        scheduler = Scheduler(settings_owner=settings_owner)
        deleted = scheduler.delete_schedule(
            schedule_id,
            expected_revision=input_data.get("expected_revision"),
        )
    except ValueError as exc:
        return error(str(exc), "VALIDATION_ERROR")
    except Exception as exc:
        return error("failed to delete schedule: " + str(exc), "INTERNAL_ERROR")

    if not deleted and not input_data.get("mutation_id"):
        return error("schedule not found: " + schedule_id, "NOT_FOUND")

    return ok({"schedule_id": schedule_id, "deleted": True})
