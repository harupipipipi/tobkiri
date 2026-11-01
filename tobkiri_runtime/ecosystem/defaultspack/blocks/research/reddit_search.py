

from blocks._common import error, ok
from domain.research.providers import RedditProvider


def run(input_data, context=None):
    query = input_data.get("query")
    if not query:
        return error("'query' is required", code="INVALID_INPUT")
    provider = RedditProvider()
    result = provider.search(
        query,
        subreddit=input_data.get("subreddit"),
        sort=str(input_data.get("sort", "relevance")),
        limit=int(input_data.get("limit", 10)),
        allow_network=bool(input_data.get("allow_network", True)),
        timeout=float(input_data.get("timeout", 8.0)),
    )
    return ok(result.as_dict())
