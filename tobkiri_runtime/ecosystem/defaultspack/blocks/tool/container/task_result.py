"""blocks.tool.container.task_result — AI操作タスク結果取得"""

from blocks._common import ok, error


def run(input_data, context):
    """GET /api/container/task/{id}/result — タスクの結果を取得する"""
    from domain.tool.ai_operator import get_task_result

    task_id = input_data.get("id") if isinstance(input_data, dict) else None
    if not task_id:
        return error("task id is required", "MISSING_PARAM")

    result = get_task_result(task_id)
    if result is None:
        return error("task not found: {}".format(task_id), "NOT_FOUND")

    return ok(result)
