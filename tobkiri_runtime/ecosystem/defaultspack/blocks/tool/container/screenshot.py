"""blocks.tool.container.screenshot — スクリーンショット取得"""

from blocks._common import ok, error


def run(input_data, context):
    """GET /api/container/{id}/screenshot — コンテナのスクリーンショットを取得する"""
    from domain.tool.screen_controller import take_screenshot

    container_id = input_data.get("id") if isinstance(input_data, dict) else None
    if not container_id:
        return error("container id is required", "MISSING_PARAM")

    try:
        result = take_screenshot(container_id)
    except KeyError as exc:
        return error(str(exc), "NOT_FOUND")
    except Exception as exc:
        return error("Screenshot failed: {}".format(exc), "SCREENSHOT_ERROR")

    return ok(result)
