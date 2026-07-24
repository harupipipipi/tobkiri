import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.ai_client.modality_contract_client import invoke_modality


def run(input_data, context):
    model = input_data.get("model")
    audio = input_data.get("audio")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if not audio:
        return error("audio is required", "MISSING_PARAM")
    params = input_data.get("params", {})

    try:
        result = invoke_modality(
            "rumi.service.ai.audio.transcribe.v1",
            "transcribe",
            {"model_id": model, "audio": audio, "parameters": params},
        )
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
