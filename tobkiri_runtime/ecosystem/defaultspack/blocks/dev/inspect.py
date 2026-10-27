"""defaults.dev.inspect — 直前のリクエスト情報を返す handler

入力:
    {
        "conversation_id": str (任意),
        "request_id": str (任意)
    }

    request_id 指定時はそのリクエストのログを返す。
    conversation_id 指定時はその会話の最新ログを返す。
    両方未指定の場合は直前のリクエストログを返す。

出力:
    {"status": "ok", "data": {...}}
"""

from blocks._common import ok, error

from domain.dev.inspector import Inspector


def run(input_data: dict, context: dict) -> dict:
    inspector = Inspector()

    request_id = input_data.get("request_id")
    conversation_id = input_data.get("conversation_id")

    if request_id:
        log = inspector.get_log(request_id)
        if log is None:
            return error(
                f"Request log not found: {request_id}",
                "NOT_FOUND",
            )
        return ok(log)

    if conversation_id:
        logs = inspector.find_by_conversation(conversation_id, limit=1)
        if not logs:
            return error(
                f"No logs found for conversation: {conversation_id}",
                "NOT_FOUND",
            )
        return ok(logs[0])

    # 両方未指定 → 直前のリクエスト
    latest = inspector.get_latest()
    if latest is None:
        return ok({
            "request_id": None,
            "conversation_id": None,
            "model": "",
            "prompt_used": "",
            "tools_called": [],
            "context_info": {},
            "timestamp": "",
            "_message": "No requests logged yet",
        })
    return ok(latest)
