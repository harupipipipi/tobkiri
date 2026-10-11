

from blocks._common import ok
from domain.gateway.server import get_gateway_server


def run(input_data, context=None):
    return ok(get_gateway_server().stop())
