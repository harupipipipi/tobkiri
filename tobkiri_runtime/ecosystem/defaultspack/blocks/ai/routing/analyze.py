
from blocks._common import ok, error
from domain.ai_client.client import AIClient
from domain.ai_client.model_router import ModelRouter


def run(input_data, context):
    """入力分析結果を返す。

    input_data:
        messages: list[dict] - StandardMessage 形式
        mode: str - "fast" (default) or "heavy"

    Returns:
        ok(analysis_result)
    """
    messages = input_data.get("messages")
    if not messages:
        return error("messages is required", "MISSING_PARAM")
    if not isinstance(messages, list):
        return error("messages must be a list", "INVALID_INPUT")

    mode = input_data.get("mode", "fast")
    if mode not in ("fast", "heavy"):
        return error("mode must be 'fast' or 'heavy'", "INVALID_INPUT")

    client = AIClient()
    router = ModelRouter(client)
    analysis = router.analyze(messages, mode=mode)

    return ok(analysis)
