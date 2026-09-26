

from blocks._common import error, ok
from domain.gateway.server import get_gateway_server


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    result = get_gateway_server().start(input_data.get("host", "127.0.0.1"), int(input_data.get("port", 18789)))
    if result.get("status") == "error":
        return error(result.get("error", "gateway start failed"), "PERMISSION_DENIED")
    return ok(result)
