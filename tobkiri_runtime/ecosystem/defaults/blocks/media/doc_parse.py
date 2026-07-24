"""defaults.media.doc_parse — ドキュメントパースブロック"""
from blocks._common import ok, error
from domain.media.contract_adapter import MEDIA_INSPECT, invoke_media_contract


def run(input_data, context):
    """ドキュメントをパースしてテキストコンテンツを返す（スタブ）。

    input_data:
        path (str): ドキュメントファイルパス
        format (str|None): ドキュメントフォーマットのヒント

    Returns:
        dict: {"status": "ok", "data": {"content", "metadata": {"path", "format"}}}
    """
    path = input_data.get("path")
    workspace_id = input_data.get("workspace_id")
    if not path or not workspace_id:
        return error("workspace_id and path are required", code="INVALID_INPUT")

    doc_format = input_data.get("format")

    try:
        result = invoke_media_contract(
            MEDIA_INSPECT,
            "document.parse",
            {
                "workspace_id": workspace_id,
                "path": path,
                "format": doc_format,
            },
            source_function_id="defaults.media.doc_parse",
        )
        return ok(result)
    except Exception as exc:
        return error(str(exc), code="PARSE_ERROR")
