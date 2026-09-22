"""planning_agent flow handler."""



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
    planning_model = "stub/default"
    if hasattr(context, "get_config") and callable(context.get_config):
        agent_id = context.get_config("agent_id") or agent_id
        planning_model = context.get_config("planning_model") or planning_model

    chat_response = _call_chat_send(
        context,
        {
            "conversation_id": conversation_id,
            "message": message,
            "agent_id": agent_id,
            "model": planning_model,
        },
    )
    if not _is_success(chat_response):
        return _chat_error(chat_response)

    return ok(
        {
            "flow_id": "planning_agent",
            "result": chat_response,
            "plan": _extract_plan_steps(chat_response),
            "agent_id": agent_id,
            "planning_model": planning_model,
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


def _extract_plan_steps(chat_response):
    data = chat_response.get("data") if isinstance(chat_response, dict) else {}
    if isinstance(data, dict):
        message = data.get("message") if isinstance(data.get("message"), dict) else data
        raw_text = message.get("raw_text") or message.get("content") or message.get("text") or ""
    else:
        raw_text = str(data or "")
    if isinstance(raw_text, list):
        parts = []
        for item in raw_text:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        raw_text = "\n".join(part for part in parts if part)
    text = str(raw_text).strip()
    if not text:
        return []
    return [line.strip(" -\t") for line in text.splitlines() if line.strip()]
