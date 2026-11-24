
from blocks._common import ok, error
from domain.ai_client.client import AIClient


def run(input_data, context):
    model = input_data.get("model")
    input_text = input_data.get("input")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if input_text is None:
        return error("input is required", "MISSING_PARAM")

    try:
        client = AIClient()
        result = client.embed(model, input_text)
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
