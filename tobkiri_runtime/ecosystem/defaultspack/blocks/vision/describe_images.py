

from blocks._common import ok
from domain.vision.image_bridge import describe_images


def run(input_data, context):
    data = input_data if isinstance(input_data, dict) else {}
    return ok(
        describe_images(
            messages=data.get("messages") if isinstance(data.get("messages"), list) else [],
            attachments=data.get("attachments") if isinstance(data.get("attachments"), list) else [],
            conversation_context=str(data.get("conversation_context") or ""),
            model=str(data.get("model") or ""),
            call_handler=(context or {}).get("call_handler") if isinstance(context, dict) else None,
        )
    )
