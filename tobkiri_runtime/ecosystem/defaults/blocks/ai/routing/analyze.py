"""Provider-free legacy request analysis projection."""

from blocks._common import error, ok


def run(input_data, context):
    del context
    messages = input_data.get("messages")
    if not isinstance(messages, list) or not messages:
        return error("messages is required", "MISSING_PARAM")
    text = "\n".join(
        str(item.get("content") or "")
        for item in messages
        if isinstance(item, dict)
    )
    has_image = any(
        isinstance(item, dict)
        and isinstance(item.get("content"), list)
        for item in messages
    )
    requirements = {
        "modalities": ["text", "image"] if has_image else ["text"],
        "capabilities": ["code"]
        if any(marker in text for marker in ("```", "def ", "class ")) else [],
        "request_surface": "legacy.routing.analyze",
    }
    return ok({"requirements": requirements, "executes": False})
