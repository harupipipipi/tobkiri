"""blocks.tool.container.task_status — AI操作タスクステータス確認"""

from blocks._common import ok, error


def run(input_data, context):
    """GET /api/container/task/{id} — タスクのステータスを確認する"""
    from domain.tool.ai_operator import get_task_status

    task_id = input_data.get("id") if isinstance(input_data, dict) else None
    if not task_id:
        return error("task id is required", "MISSING_PARAM")

    result = get_task_status(task_id)
    if result is None:
        return error("task not found: {}".format(task_id), "NOT_FOUND")

    return ok(result)
