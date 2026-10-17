

from blocks._common import ok
from domain.api_map.builder import build_api_map


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    return ok(
        build_api_map(
            profile_id=str(data.get("profile_id") or "").strip() or None,
            focus=str(data.get("focus") or "").strip() or None,
        )
    )
