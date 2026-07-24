import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.ai_client.gateway_contract_client import generate


def run(input_data, context):
    model = input_data.get("model")
    image = input_data.get("image")
    prompt = input_data.get("prompt")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if not image:
        return error("image is required", "MISSING_PARAM")
    if not prompt:
        return error("prompt is required", "MISSING_PARAM")

    try:
        result = generate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image", "image": image},
                        ],
                    }
                ],
                "requirements": {
                    "preferred_model_id": model,
                    "modalities": ["text", "image"],
                    "request_surface": "legacy.image_analyze",
                },
            }
        )
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
