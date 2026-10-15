"""
defaults.tool.consent_check — 回答テキストがセンシティブかどうか判定する handler。

handler 名: defaults.tool.consent_check

input_data:
  - text: str（必須）— 判定対象テキスト
  - use_ai: bool（任意、デフォルト false）— AI 判定を使うか
  - model: str（任意）— AI 判定時のモデル指定

戻り値 (ok):
  {
    "requires_consent": bool,
    "categories": [str],
    "consent_id": str | None,
    "disclaimers": {category: disclaimer_text}
  }

chat.send への統合方法（案）:
  send.py の assistant_msg 生成後に以下を追加:
    consent_result = call_handler("defaults.tool.consent_check", {
        "text": assistant_response_text,
        "use_ai": False,
    })
    if consent_result["data"]["requires_consent"]:
        context["emit_widget"]({
            "type": "consent_popup",
            "consent_id": consent_result["data"]["consent_id"],
            "disclaimers": consent_result["data"]["disclaimers"],
        })
        user_response = context["wait_event"]("ui.consent_response",
            timeout=300, filter={"consent_id": consent_result["data"]["consent_id"]})
        call_handler("defaults.tool.consent_confirm", {
            "consent_id": consent_result["data"]["consent_id"],
            "accepted": user_response.get("accepted", False),
        })
"""


from blocks._common import ok, error
from domain.tool.consent import ConsentChecker


def run(input_data, context):
    """defaults.tool.consent_check — テキストの同意必要性を判定する"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    text = input_data.get("text")
    if text is None:
        return error("text is required", "MISSING_PARAM")
    if not isinstance(text, str):
        return error("text must be a string", "INVALID_PARAM")

    use_ai = input_data.get("use_ai", False)
    model = input_data.get("model", "stub/default")

    checker = ConsentChecker()

    ai_client = None
    if use_ai:
        try:
            from domain.ai_client.client import AIClient
            ai_client = AIClient()
        except Exception:
            pass

    result = checker.check(
        text=text,
        use_ai=use_ai,
        ai_client=ai_client,
        model=model,
    )

    return ok(result)
