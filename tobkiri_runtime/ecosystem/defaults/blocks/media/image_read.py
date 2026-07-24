"""defaults.media.image_read — 画像メタデータ取得ブロック"""
from blocks._common import ok, error
from domain.media.contract_adapter import MEDIA_INSPECT, invoke_media_contract


def run(input_data, context):
    """画像ファイルのメタデータを返す。

    input_data:
        path (str): 画像ファイルパス

    Returns:
        dict: {"status": "ok", "data": {"path", "width", "height", "format", "size_bytes"}}
    """
    path = input_data.get("path")
    workspace_id = input_data.get("workspace_id")
    if not path or not workspace_id:
        return error("workspace_id and path are required", code="INVALID_INPUT")

    try:
        metadata = invoke_media_contract(
            MEDIA_INSPECT,
            "image.inspect",
            {"workspace_id": workspace_id, "path": path},
            source_function_id="defaults.media.image_read",
        )
        return ok(metadata)
    except FileNotFoundError as exc:
        return error(str(exc), code="FILE_NOT_FOUND")
    except Exception as exc:
        return error(str(exc), code="READ_ERROR")
