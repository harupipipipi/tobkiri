"""defaults.media.clipboard_read — クリップボード読取ブロック"""
from blocks._common import ok, error
from domain.media.contract_adapter import CLIPBOARD_READ, invoke_media_contract


def run(input_data, context):
    """Hostの正式契約でクリップボードの内容を読み取る。

    input_data:
        (なし)

    Returns:
        dict: {"status": "ok", "data": {"content", "format"}}
    """
    try:
        result = invoke_media_contract(
            CLIPBOARD_READ, "read", {},
            source_function_id="defaults.media.clipboard_read",
        )
        if result.get("success") is not True:
            return result
        return ok({
            "content": result["text"],
            "format": "text/plain",
        })
    except Exception as exc:
        return error(str(exc), code="CLIPBOARD_READ_ERROR")
