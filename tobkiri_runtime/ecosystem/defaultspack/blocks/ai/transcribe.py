
from blocks._common import ok, error
from domain.ai_client.client import AIClient


def run(input_data, context):
    model = input_data.get("model")
    audio = input_data.get("audio")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if not audio:
        return error("audio is required", "MISSING_PARAM")
    params = input_data.get("params", {})

    try:
        client = AIClient()
        result = client.transcribe(model, audio, params=params)
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
