
from blocks._common import ok, error
from domain.ai_client.client import AIClient


def run(input_data, context):
    model = input_data.get("model")
    prompt = input_data.get("prompt")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if not prompt:
        return error("prompt is required", "MISSING_PARAM")
    params = input_data.get("params", {})

    try:
        client = AIClient()
        result = client.image_gen(model, prompt, params=params)
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
