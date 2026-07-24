"""defaults.media.clipboard_read — クリップボード読取ブロック"""
from blocks._common import error
from domain.media.contract_adapter import CLIPBOARD_READ, invoke_media_contract


def run(input_data, context):
    """クリップボードの内容を読み取る（スタブ）。

    input_data:
        (なし)

    Returns:
        dict: {"status": "ok", "data": {"content", "format"}}
    """
    try:
        return invoke_media_contract(
            CLIPBOARD_READ,
            "read",
            {},
            source_function_id="defaults.media.clipboard_read",
        )
    except Exception as exc:
        return error(str(exc), code="CLIPBOARD_READ_ERROR")
