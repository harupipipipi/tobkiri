"""blocks.context.conversation — 会話別コンテキスト情報を返す handler。

入力:
    {
        "conversation_id": str  (必須、path_inject で注入される)
    }

出力:
    {"status": "ok", "data": { ... 会話コンテキスト情報 ... }}
"""


from blocks._common import ok, error

from domain.context.analyzer import analyze_conversation


def run(input_data, context):
    """会話別のコンテキスト情報を返す。

    conversation_id が未指定または会話が存在しない場合はエラーを返す。

    Args:
        input_data: リクエストデータ dict。"conversation_id" キーを含む。
        context:    カーネルから渡されるコンテキスト dict。

    Returns:
        ok(data) または error(message, code) 形式の dict。
    """
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")

    result = analyze_conversation(conversation_id)
    if result is None:
        return error(
            "Conversation not found: " + str(conversation_id),
            "NOT_FOUND",
        )

    return ok(result)
