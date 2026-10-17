"""blocks.tool.container.task_create — AI操作タスク作成"""

from blocks._common import ok, error


def run(input_data, context):
    """POST /api/container/task — AI操作タスクを作成して実行開始する"""
    from domain.tool.ai_operator import create_task

    if not isinstance(input_data, dict):
        return error("request body must be a JSON object", "INVALID_INPUT")

    container_id = input_data.get("container_id")
    if not container_id:
        return error("container_id is required", "MISSING_PARAM")

    instruction = input_data.get("instruction")
    if not instruction:
        return error("instruction is required", "MISSING_PARAM")

    config = input_data.get("config", {})

    try:
        result = create_task(container_id, instruction, config)
    except KeyError as exc:
        return error(str(exc), "NOT_FOUND")
    except Exception as exc:
        return error("Failed to create task: {}".format(exc), "TASK_ERROR")

    return ok(result)
