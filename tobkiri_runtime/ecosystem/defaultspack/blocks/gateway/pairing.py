

from blocks._common import ok
from domain.gateway.pairing import create_pairing_code


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    return ok(create_pairing_code(input_data.get("client_id", "")))
