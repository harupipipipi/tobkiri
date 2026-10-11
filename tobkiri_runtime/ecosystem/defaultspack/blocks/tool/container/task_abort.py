"""blocks.tool.container.task_abort — AI操作タスク中断"""

from blocks._common import ok, error


def run(input_data, context):
    """POST /api/container/task/{id}/abort — タスクを中断する"""
    from domain.tool.ai_operator import abort_task

    task_id = input_data.get("id") if isinstance(input_data, dict) else None
    if not task_id:
        return error("task id is required", "MISSING_PARAM")

    try:
        result = abort_task(task_id)
    except KeyError as exc:
        return error(str(exc), "NOT_FOUND")
    except Exception as exc:
        return error("Failed to abort task: {}".format(exc), "ABORT_ERROR")

    return ok(result)
