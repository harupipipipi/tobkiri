"""defaults.media.clipboard_write — クリップボード書込ブロック"""
from blocks._common import ok, error
from domain.media.contract_adapter import CLIPBOARD_WRITE, invoke_media_contract


def run(input_data, context):
    """Hostの正式契約でクリップボードに内容を書き込む。

    input_data:
        content (str): 書き込む内容

    Returns:
        dict: {"status": "ok", "data": {"written": true}}
    """
    content = input_data.get("content")
    if content is None:
        return error("content is required", code="INVALID_INPUT")

    try:
        result = invoke_media_contract(
            CLIPBOARD_WRITE, "write", {"text": content},
            source_function_id="defaults.media.clipboard_write",
        )
        if result.get("success") is not True:
            return result
        return ok({"written": True})
    except Exception as exc:
        return error(str(exc), code="CLIPBOARD_WRITE_ERROR")
