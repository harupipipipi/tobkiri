

from blocks._common import error, ok
from domain.research.providers import ExternalWebProvider, compact_provider_result


def run(input_data, context=None):
    query = input_data.get("query")
    if not query:
        return error("'query' is required", code="INVALID_INPUT")
    provider = ExternalWebProvider()
    result = provider.search(
        query,
        limit=int(input_data.get("limit", 5)),
        allow_network=bool(input_data.get("allow_network", True)),
        timeout=float(input_data.get("timeout", 8.0)),
        domains=input_data.get("domains"),
        official_only=bool(input_data.get("official_only", False)),
        fetch_pages=bool(input_data.get("fetch_pages", False)),
    )
    result = compact_provider_result(
        result,
        max_chars=input_data.get("max_chars") or input_data.get("max_output_chars"),
        max_tokens=input_data.get("max_tokens") or input_data.get("max_output_tokens"),
    )
    return ok(result.as_dict())
