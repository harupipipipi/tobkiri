"""blocks.tool.container.delete — コンテナ削除"""

from blocks._common import ok, error


def run(input_data, context):
    """DELETE /api/container/{id} — コンテナを削除する"""
    from domain.tool.container_manager import delete_container

    container_id = input_data.get("id") if isinstance(input_data, dict) else None
    if not container_id:
        return error("container id is required", "MISSING_PARAM")

    try:
        result = delete_container(container_id)
    except KeyError as exc:
        return error(str(exc), "NOT_FOUND")
    except Exception as exc:
        return error("Failed to delete container: {}".format(exc), "CONTAINER_ERROR")

    return ok(result)
