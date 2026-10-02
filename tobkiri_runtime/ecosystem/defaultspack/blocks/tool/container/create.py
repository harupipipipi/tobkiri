"""blocks.tool.container.create — コンテナ作成"""

from blocks._common import ok, error


def run(input_data, context):
    """POST /api/container — 新しいコンテナを作成する"""
    from domain.tool.container_manager import create_container

    if not isinstance(input_data, dict):
        return error("request body must be a JSON object", "INVALID_INPUT")

    name = input_data.get("name", "")
    image = input_data.get("image", "ubuntu:22.04")
    config = input_data.get("config", {})

    try:
        result = create_container(name, image, config)
    except Exception as exc:
        return error("Failed to create container: {}".format(exc), "CONTAINER_ERROR")

    return ok(result)
