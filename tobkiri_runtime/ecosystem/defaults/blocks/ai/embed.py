import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.ai_client.modality_contract_client import invoke_modality


def run(input_data, context):
    model = input_data.get("model")
    input_text = input_data.get("input")
    if not model:
        return error("model is required", "MISSING_PARAM")
    if input_text is None:
        return error("input is required", "MISSING_PARAM")

    try:
        result = invoke_modality(
            "rumi.service.ai.embedding.v1",
            "embed",
            {"model_id": model, "input": input_text},
        )
        return ok(result)
    except RuntimeError as e:
        return error(str(e), "PROVIDER_ERROR")
