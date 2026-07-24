import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.ai_client.modality_contract_client import invoke_modality


def run(input_data, context):
    model = input_data.get("model")
    prompt = input_data.get("prompt")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if not prompt:
        return error("prompt is required", "MISSING_PARAM")
    params = input_data.get("params", {})

    try:
        result = invoke_modality(
            "rumi.service.ai.image.v1",
            "generate",
            {"model_id": model, "prompt": prompt, "parameters": params},
        )
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
