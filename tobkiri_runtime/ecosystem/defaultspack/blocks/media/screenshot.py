"""defaults.media.screenshot — スクリーンショットブロック"""
from blocks._common import error
from domain.media.contract_adapter import MEDIA_CAPTURE, execute_ui_host_contract


def run(input_data, context):
    """スクリーンショットを撮影する。

    直接のホスト実行は廃止済みのため、宣言済みの Pack v4 media capture
    contract 経路へ委譲する。所有者や実行環境が無い場合は型付きエラーで
    失敗する。

    input_data:
        region (dict|None): キャプチャ領域（将来拡張）
            例: {"x": 0, "y": 0, "width": 800, "height": 600}

    Returns:
        dict: contract 経路の処理結果
    """
    request = {"operation": "host.screen.capture"}
    region = input_data.get("region")
    if isinstance(region, dict):
        request["region"] = region

    try:
        return execute_ui_host_contract(
            MEDIA_CAPTURE,
            "rumi_media_capture_host_service_pack.media-capture",
            request,
            source_function_id="defaults.media.screenshot",
            context=context,
        )
    except Exception as exc:
        return error(str(exc), code="SCREENSHOT_ERROR")
