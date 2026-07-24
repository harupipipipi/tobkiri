"""defaults.media.screenshot — スクリーンショットブロック"""
from blocks._common import error
from domain.media.contract_adapter import MEDIA_CAPTURE, invoke_media_contract


def run(input_data, context):
    """スクリーンショットを撮影する（スタブ）。

    input_data:
        region (dict|None): キャプチャ領域（将来拡張）
            例: {"x": 0, "y": 0, "width": 800, "height": 600}

    Returns:
        dict: {"status": "ok", "data": {"path", "width", "height"}}
    """
    try:
        payload = {}
        if input_data.get("region") is not None:
            payload["region"] = input_data["region"]
        return invoke_media_contract(
            MEDIA_CAPTURE,
            "host.screen.capture",
            payload,
            source_function_id="defaults.media.screenshot",
        )
    except Exception as exc:
        return error(str(exc), code="SCREENSHOT_ERROR")
