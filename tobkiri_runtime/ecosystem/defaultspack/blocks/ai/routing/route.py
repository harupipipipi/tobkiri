
from blocks._common import ok, error
from domain.ai_client.client import AIClient
from domain.ai_client.model_router import ModelRouter


def run(input_data, context):
    """最適モデルを選択して返す。

    input_data:
        messages: list[dict] - StandardMessage 形式
        mode: str - "fast" (default) or "heavy"
        speed_preference: str - "fast", "balanced" (default), "heavy"

    Returns:
        ok({
            selected_model: str,
            analysis: dict,
            scores: dict,
            matched_rule: str | None,
            reason: str,
        })
    """
    messages = input_data.get("messages")
    if not messages:
        return error("messages is required", "MISSING_PARAM")
    if not isinstance(messages, list):
        return error("messages must be a list", "INVALID_INPUT")

    mode = input_data.get("mode", "fast")
    if mode not in ("fast", "heavy"):
        return error("mode must be 'fast' or 'heavy'", "INVALID_INPUT")

    speed_preference = input_data.get("speed_preference", "balanced")
    if speed_preference not in ("fast", "balanced", "heavy"):
        return error("speed_preference must be 'fast', 'balanced', or 'heavy'", "INVALID_INPUT")

    client = AIClient()
    router = ModelRouter(client)
    result = router.route(messages, mode=mode, speed_preference=speed_preference)

    return ok(result)
