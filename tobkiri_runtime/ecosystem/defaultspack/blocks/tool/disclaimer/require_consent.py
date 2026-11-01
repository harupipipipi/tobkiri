"""
blocks/tool/disclaimer/require_consent.py — 同意要求を生成する。

テキストと検出結果を受け取り、回答をブロックして同意要求データを返す。

input_data:
  - text: str（必須）— ブロック対象の回答テキスト
  - detected_categories: [str]（必須）— 検出されたカテゴリ名リスト
  - disclaimers: {str: str}（必須）— カテゴリ別免責テキスト

戻り値 (ok):
  {
    "consent_id": str,
    "categories": [str],
    "disclaimers": {category: disclaimer_text},
    "created_at": str,
  }
"""


from blocks._common import ok, error
from domain.tool.disclaimer_manager import DisclaimerManager


def run(input_data, context):
    """同意要求を生成し、回答テキストをブロックする。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    text = input_data.get("text")
    if text is None:
        return error("text is required", "MISSING_PARAM")
    if not isinstance(text, str):
        return error("text must be a string", "INVALID_PARAM")

    detected_categories = input_data.get("detected_categories")
    if detected_categories is None:
        return error("detected_categories is required", "MISSING_PARAM")
    if not isinstance(detected_categories, list):
        return error("detected_categories must be a list", "INVALID_PARAM")
    if len(detected_categories) == 0:
        return error("detected_categories must not be empty", "INVALID_PARAM")

    disclaimers = input_data.get("disclaimers")
    if disclaimers is None:
        return error("disclaimers is required", "MISSING_PARAM")
    if not isinstance(disclaimers, dict):
        return error("disclaimers must be a dict", "INVALID_PARAM")

    manager = DisclaimerManager()
    result = manager.require_consent(
        text=text,
        detected_categories=detected_categories,
        disclaimers=disclaimers,
    )
    return ok(result)
