"""
defaults.tool.consent_confirm — ユーザーが免責ポップアップに同意/拒否した記録を保存する handler。

handler 名: defaults.tool.consent_confirm

input_data:
  - consent_id: str（必須）
  - accepted: bool（必須）

戻り値 (ok):
  {
    "consent_id": str,
    "accepted": bool,
    "accepted_at": str | None
  }
"""


from blocks._common import ok, error
from domain.tool.consent import ConsentChecker


def run(input_data, context):
    """defaults.tool.consent_confirm — 同意を記録する"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    consent_id = input_data.get("consent_id")
    if not consent_id:
        return error("consent_id is required", "MISSING_PARAM")
    if not isinstance(consent_id, str):
        return error("consent_id must be a string", "INVALID_PARAM")

    accepted = input_data.get("accepted")
    if accepted is None:
        return error("accepted is required", "MISSING_PARAM")
    if not isinstance(accepted, bool):
        return error("accepted must be a boolean", "INVALID_PARAM")

    checker = ConsentChecker()
    result = checker.confirm(consent_id, accepted)

    if result is None:
        return error(
            "consent_id '{}' not found".format(consent_id),
            "NOT_FOUND",
        )

    return ok(result)
