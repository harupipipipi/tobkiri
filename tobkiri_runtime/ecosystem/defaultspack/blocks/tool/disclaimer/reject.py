"""
blocks/tool/disclaimer/reject.py — 同意拒否 → 回答テキストを破棄する。

input_data:
  - consent_id: str（パスパラメータまたは input_data から取得）

戻り値 (ok):
  {
    "consent_id": str,
    "accepted": false,
    "rejected_at": str,
    "categories": [str],
  }
"""


from blocks._common import ok, error
from domain.tool.disclaimer_manager import DisclaimerManager


def run(input_data, context):
    """同意を拒否し、ブロックされていた回答テキストを破棄する。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    consent_id = input_data.get("consent_id") or input_data.get("id")
    if not consent_id:
        return error("consent_id is required", "MISSING_PARAM")
    if not isinstance(consent_id, str):
        return error("consent_id must be a string", "INVALID_PARAM")

    manager = DisclaimerManager()
    result = manager.reject(consent_id)

    if result is None:
        return error(
            "consent_id '{}' not found or already resolved".format(consent_id),
            "NOT_FOUND",
        )

    return ok(result)
