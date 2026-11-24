
from blocks._common import ok, error
from domain.ai_client.client import AIClient


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
        client = AIClient()
        result = client.image_analyze(model, image, prompt)
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
