"""blocks.context.token_estimate — テキストのトークン数を推定する handler。

入力:
    {
        "text": str          (必須 — 推定対象テキスト)
        "model": str         (任意 — モデル文字列。指定時はコンテキスト上限も返す)
    }

出力:
    {"status": "ok", "data": {
        "text_length": int,
        "estimated_tokens": int,
        "model": str | null,
        "model_context_limit": int | null,
        "usage_ratio": float | null
    }}
"""


from blocks._common import ok, error

from domain.context.analyzer import estimate_tokens, get_model_context_limit


def run(input_data, context):
    """テキストの推定トークン数を返す。

    オプションでモデルを指定すると、そのモデルのコンテキスト上限に対する
    使用率も合わせて返す。

    Args:
        input_data: リクエストデータ dict。"text" キーを含む。
                    "model" キーは任意。
        context:    カーネルから渡されるコンテキスト dict。

    Returns:
        ok(data) または error(message, code) 形式の dict。
    """
    text = input_data.get("text")
    if text is None:
        return error("text is required", "INVALID_INPUT")

    if not isinstance(text, str):
        return error("text must be a string", "INVALID_INPUT")

    estimated = estimate_tokens(text)
    text_length = len(text)

    model = input_data.get("model")
    model_context_limit = None
    usage_ratio = None

    if model and isinstance(model, str):
        model_context_limit = get_model_context_limit(model)
        if model_context_limit > 0:
            usage_ratio = round(estimated / model_context_limit, 6)

    return ok({
        "text_length": text_length,
        "estimated_tokens": estimated,
        "model": model if model else None,
        "model_context_limit": model_context_limit,
        "usage_ratio": usage_ratio,
    })
