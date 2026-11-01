

from blocks._common import ok


def _content_items(message):
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        return []
    if isinstance(message, str):
        return [{"type": "text", "text": message}]
    return []


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    message = data.get("message", data)
    items = _content_items(message)
    attachments = message.get("attachments") if isinstance(message, dict) and isinstance(message.get("attachments"), list) else []
    has_text = any(str(item.get("type", "")).lower() == "text" and item.get("text") for item in items if isinstance(item, dict))
    has_images = any(str(item.get("type", "")).lower() in {"image", "image_url", "input_image"} for item in items if isinstance(item, dict))
    has_audio = any(str(item.get("type", "")).lower() in {"audio", "input_audio"} for item in items if isinstance(item, dict)) or any(
        str(item.get("type") or item.get("mime_type") or "").lower().startswith("audio/")
        for item in attachments
        if isinstance(item, dict)
    )
    has_files = any(str(item.get("type", "")).lower() in {"file", "attachment"} for item in items if isinstance(item, dict))
    return ok(
        {
            "has_text": has_text,
            "has_images": has_images,
            "has_audio": has_audio,
            "has_files": has_files,
            "modalities": [
                name
                for name, enabled in (
                    ("text", has_text),
                    ("image", has_images),
                    ("audio", has_audio),
                    ("file", has_files),
                )
                if enabled
            ],
        }
    )
