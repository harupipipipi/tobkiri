"""blocks.tool.container.start — コンテナ起動"""

from blocks._common import ok, error


def run(input_data, context):
    """POST /api/container/{id}/start — コンテナを起動する"""
    from domain.tool.container_manager import start_container

    container_id = input_data.get("id") if isinstance(input_data, dict) else None
    if not container_id:
        return error("container id is required", "MISSING_PARAM")

    try:
        result = start_container(container_id)
    except KeyError as exc:
        return error(str(exc), "NOT_FOUND")
    except Exception as exc:
        return error("Failed to start container: {}".format(exc), "CONTAINER_ERROR")

    return ok(result)
