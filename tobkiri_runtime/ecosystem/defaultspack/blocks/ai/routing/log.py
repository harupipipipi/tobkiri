
from blocks._common import ok, error
from domain.ai_client.client import AIClient
from domain.ai_client.model_router import ModelRouter


def run(input_data, context):
    """ルーティングログ取得。

    input_data:
        limit: int (default 100)
        offset: int (default 0)

    Returns:
        ok({
            "entries": list[dict],
            "total": int,
            "limit": int,
            "offset": int,
        })
    """
    limit = input_data.get("limit", 100)
    offset = input_data.get("offset", 0)

    if not isinstance(limit, int) or limit < 1:
        limit = 100
    if not isinstance(offset, int) or offset < 0:
        offset = 0

    client = AIClient()
    router = ModelRouter(client)
    entries = router.routing_log.get_all(limit=limit, offset=offset)
    total = router.routing_log.count()

    return ok({
        "entries": entries,
        "total": total,
        "limit": limit,
        "offset": offset,
    })
