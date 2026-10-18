

from blocks._common import ok
from domain.gateway.server import get_gateway_server


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    event = input_data.get("event", "gateway.message")
    payload = input_data.get("payload", input_data)
    return ok(get_gateway_server().delivery.publish(event, payload))
