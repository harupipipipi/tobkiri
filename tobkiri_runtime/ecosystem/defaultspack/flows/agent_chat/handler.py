"""agent_chat flow handler."""



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

    agent_id = "default_agent"
    max_iterations = 10
    if hasattr(context, "get_config") and callable(context.get_config):
        agent_id = context.get_config("agent_id") or agent_id
        try:
            max_iterations = int(context.get_config("max_iterations") or max_iterations)
        except (TypeError, ValueError):
            max_iterations = 10

    chat_response = _call_chat_send(
        context,
        {
            "conversation_id": conversation_id,
            "message": message,
            "agent_id": agent_id,
        },
    )
    if not _is_success(chat_response):
        return _chat_error(chat_response)

    return ok(
        {
            "flow_id": "agent_chat",
            "result": chat_response,
            "agent_id": agent_id,
            "iterations_used": 1,
            "max_iterations": max_iterations,
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
