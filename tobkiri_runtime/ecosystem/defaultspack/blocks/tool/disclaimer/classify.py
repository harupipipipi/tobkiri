"""
blocks/tool/disclaimer/classify.py — テキストから免責カテゴリを検出する。

input_data:
  - text: str（必須）— 分析対象テキスト
  - use_ai: bool（任意、デフォルト false）— AI 判定を使うか
  - model: str（任意）— AI 判定時のモデル指定

戻り値 (ok):
  {
    "detected_categories": [str],
    "disclaimers": {category: disclaimer_text}
  }
"""


from blocks._common import ok, error
from domain.tool.disclaimer_manager import DisclaimerManager


def run(input_data, context):
    """テキストを分析し、該当する免責カテゴリを検出する。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    text = input_data.get("text")
    if text is None:
        return error("text is required", "MISSING_PARAM")
    if not isinstance(text, str):
        return error("text must be a string", "INVALID_PARAM")

    use_ai = input_data.get("use_ai", False)
    model = input_data.get("model", "stub/default")

    ai_client = None
    if use_ai:
        try:
            from domain.ai_client.client import AIClient
            ai_client = AIClient()
        except Exception:
            ai_client = None

    manager = DisclaimerManager()
    result = manager.classify(text=text, use_ai=use_ai, ai_client=ai_client, model=model)
    return ok(result)
