
from blocks._common import ok, error
from domain.ai_client.client import AIClient


def run(input_data, context):
    model = input_data.get("model")
    text = input_data.get("text")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if not text:
        return error("text is required", "MISSING_PARAM")
    voice = input_data.get("voice")

    try:
        client = AIClient()
        result = client.tts(model, text, voice=voice)
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
