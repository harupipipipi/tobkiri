"""blocks.tool.container.exec — コンテナ内コマンド実行"""

from blocks._common import ok, error


def run(input_data, context):
    """POST /api/container/{id}/exec — コンテナ内でコマンドを実行する"""
    from domain.tool.container_manager import exec_in_container

    if not isinstance(input_data, dict):
        return error("request body must be a JSON object", "INVALID_INPUT")

    container_id = input_data.get("id")
    if not container_id:
        return error("container id is required", "MISSING_PARAM")

    command = input_data.get("command")
    if not command:
        return error("command is required", "MISSING_PARAM")

    try:
        result = exec_in_container(container_id, command)
    except KeyError as exc:
        return error(str(exc), "NOT_FOUND")
    except Exception as exc:
        return error("Command execution failed: {}".format(exc), "EXEC_ERROR")

    return ok(result)
