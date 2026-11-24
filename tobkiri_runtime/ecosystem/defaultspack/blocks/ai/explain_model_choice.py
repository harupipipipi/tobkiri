

from blocks._common import ok
from domain.ai_client.model_router import explain_model_choice


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    return ok(
        {
            "explanation": explain_model_choice(
                str(data.get("selected_model") or data.get("model") or ""),
                data.get("reason_codes") if isinstance(data.get("reason_codes"), list) else [],
                data.get("warnings") if isinstance(data.get("warnings"), list) else [],
            )
        }
    )
