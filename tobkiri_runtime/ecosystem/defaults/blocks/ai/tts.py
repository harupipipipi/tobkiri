import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.ai_client.modality_contract_client import invoke_modality


def run(input_data, context):
    model = input_data.get("model")
    text = input_data.get("text")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if not text:
        return error("text is required", "MISSING_PARAM")
    voice = input_data.get("voice")

    try:
        result = invoke_modality(
            "rumi.service.ai.audio.speech.v1",
            "synthesize",
            {"model_id": model, "text": text, "voice": voice},
        )
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
