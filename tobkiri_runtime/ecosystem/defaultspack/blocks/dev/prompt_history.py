"""defaults.dev.prompt_history — プロンプト使用履歴を返す handler

入力:
    {"limit": int (デフォルト20)}

出力:
    {"status": "ok", "data": {"history": [...]}}

どの会話でどのプロンプトが使われたかを時系列で返す。
"""

from blocks._common import ok, error

from domain.dev.inspector import Inspector


def run(input_data: dict, context: dict) -> dict:
    inspector = Inspector()
    limit = input_data.get("limit", 20)

    if not isinstance(limit, int) or limit < 1:
        limit = 20
    if limit > 1000:
        limit = 1000

    logs = inspector.list_logs(limit=limit)

    history = []
    for log in logs:
        history.append({
            "request_id": log["request_id"],
            "conversation_id": log["conversation_id"],
            "model": log["model"],
            "prompt_used": log["prompt_used"],
            "tools_called": log["tools_called"],
            "timestamp": log["timestamp"],
        })

    return ok({"history": history, "total": len(history)})
