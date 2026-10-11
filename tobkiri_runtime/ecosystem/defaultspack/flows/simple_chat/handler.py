"""simple_chat flow handler."""



from blocks._common import error, gen_id, ok


def run(input_data, context):
    conversation_id = ""
    message = {}

    if isinstance(input_data, dict):
        conversation_id = input_data.get("conversation_id", "")
        message = input_data.get("message", {})

    if not conversation_id:
        conversation_id = gen_id()

    if not message:
        return error("message is required")

    chat_response = _call_chat_send(
        context,
        {
            "conversation_id": conversation_id,
            "message": message,
        },
    )
    if not _is_success(chat_response):
        return _chat_error(chat_response)

    return ok(
        {
            "flow_id": "simple_chat",
            "result": chat_response,
        }
    )


def _call_chat_send(context, params):
    if hasattr(context, "call_handler") and callable(context.call_handler):
        try:
            return context.call_handler("defaults.chat.send", params)
        except Exception as exc:
            return {
                "status": "error",
                "error": {"code": "CHAT_SEND_FAILED", "message": str(exc)},
            }
    return {
        "status": "error",
        "error": {
            "code": "CHAT_SEND_UNAVAILABLE",
            "message": "defaults.chat.send is unavailable in this flow context",
        },
    }


def _is_success(response):
    return isinstance(response, dict) and response.get("status") != "error"


def _chat_error(response):
    if isinstance(response, dict):
        details = response.get("error")
        if isinstance(details, dict):
            return error(
                details.get("message", "defaults.chat.send failed"),
                details.get("code", "CHAT_SEND_FAILED"),
            )
        return error(str(details or "defaults.chat.send failed"), "CHAT_SEND_FAILED")
    return error("defaults.chat.send did not return a response", "CHAT_SEND_FAILED")
