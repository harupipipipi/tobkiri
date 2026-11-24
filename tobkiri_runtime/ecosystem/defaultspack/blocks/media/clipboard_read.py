"""defaults.media.clipboard_read — クリップボード読取ブロック"""
from blocks._common import error
from domain.media.contract_adapter import CLIPBOARD_READ, execute_ui_host_contract


def run(input_data, context):
    """クリップボードの内容を読み取る。

    直接のホスト実行は廃止済みのため、宣言済みの Pack v4 contract 経路へ
    委譲する。所有者や実行環境が無い場合は型付きエラーで失敗する。

    input_data:
        (なし)

    Returns:
        dict: contract 経路の処理結果
    """
    del input_data
    try:
        return execute_ui_host_contract(
            CLIPBOARD_READ,
            "rumi_clipboard_host_service_pack.clipboard-read",
            {},
            source_function_id="defaults.media.clipboard_read",
            context=context,
        )
    except Exception as exc:
        return error(str(exc), code="CLIPBOARD_READ_ERROR")
