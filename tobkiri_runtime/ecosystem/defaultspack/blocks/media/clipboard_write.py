"""defaults.media.clipboard_write — クリップボード書込ブロック"""
from blocks._common import error
from domain.media.contract_adapter import CLIPBOARD_WRITE, execute_ui_host_contract


def run(input_data, context):
    """クリップボードに内容を書き込む。

    直接のホスト実行は廃止済みのため、宣言済みの Pack v4 contract 経路へ
    委譲する。所有者や実行環境が無い場合は型付きエラーで失敗する。

    input_data:
        content (str): 書き込む内容

    Returns:
        dict: contract 経路の処理結果
    """
    content = input_data.get("content")
    if content is None:
        return error("content is required", code="INVALID_INPUT")

    try:
        return execute_ui_host_contract(
            CLIPBOARD_WRITE,
            "rumi_clipboard_host_service_pack.clipboard-write",
            {"text": str(content)},
            source_function_id="defaults.media.clipboard_write",
            context=context,
        )
    except Exception as exc:
        return error(str(exc), code="CLIPBOARD_WRITE_ERROR")
