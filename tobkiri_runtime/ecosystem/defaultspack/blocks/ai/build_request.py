

from blocks._common import ok


def _content(value):
    if isinstance(value, dict):
        return str(value.get("content") or value.get("prompt") or "")
    return str(value or "")


def _selected_model(route_model):
    if not isinstance(route_model, dict):
        return "default"
    return str(
        route_model.get("selected_model")
        or route_model.get("model")
        or route_model.get("model_id")
        or route_model.get("profile_id")
        or "default"
    )


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    message = data.get("message")
    system_prompt = data.get("system_prompt")
    permitted_tools = data.get("tools")
    tools = permitted_tools.get("tools") if isinstance(permitted_tools, dict) else permitted_tools
    if not isinstance(tools, list):
        tools = []
    messages = []
    prompt_text = _content(system_prompt)
    if prompt_text:
        messages.append({"role": "system", "content": prompt_text})
    if isinstance(message, dict):
        messages.append(message)
    else:
        messages.append({"role": "user", "content": str(message or "")})
    request = {
        "conversation_id": data.get("conversation_id"),
        "model": _selected_model(data.get("route_model")),
        "messages": messages,
        "tools": tools,
        "params": {},
    }
    if data.get("vision_bridge_result"):
        request["vision_bridge_result"] = data["vision_bridge_result"]
    return ok(request)
