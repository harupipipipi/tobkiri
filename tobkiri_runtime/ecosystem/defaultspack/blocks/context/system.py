"""blocks.context.system — システム全体のコンテキスト情報を返す handler。

入力:
    {} (パラメータ不要)

出力:
    {"status": "ok", "data": { ... システムコンテキスト情報 ... }}
"""


from blocks._common import ok

from domain.context.analyzer import analyze_system


def run(input_data, context):
    """システム全体のコンテキスト情報を返す。

    アクティブ会話数、総メモリ使用量概算、登録済みプロンプト数、
    ツール数などを包括的に返す。

    Args:
        input_data: リクエストデータ dict (未使用)。
        context:    カーネルから渡されるコンテキスト dict。

    Returns:
        ok(data) 形式の dict。
    """
    result = analyze_system()
    return ok(result)
