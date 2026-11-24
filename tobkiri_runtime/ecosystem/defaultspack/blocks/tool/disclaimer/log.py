"""
blocks/tool/disclaimer/log.py — 同意ログの取得。

input_data:
  - limit: int（任意、デフォルト 100）
  - offset: int（任意、デフォルト 0）

戻り値 (ok):
  {
    "entries": [
      {
        "consent_id": str,
        "action": "accepted" | "rejected",
        "categories": [str],
        "created_at": str,
        "resolved_at": str,
      },
      ...
    ],
    "total": int,
  }
"""


from blocks._common import ok, error
from domain.tool.disclaimer_manager import DisclaimerManager


def run(input_data, context):
    """同意ログを取得する。"""
    if not isinstance(input_data, dict):
        input_data = {}

    limit = input_data.get("limit", 100)
    offset = input_data.get("offset", 0)

    if not isinstance(limit, int) or limit < 1:
        limit = 100
    if not isinstance(offset, int) or offset < 0:
        offset = 0

    manager = DisclaimerManager()
    result = manager.list_log(limit=limit, offset=offset)
    return ok(result)
