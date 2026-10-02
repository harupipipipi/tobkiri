"""Explicit Wave 10 sunset shim for the removed legacy Company inbox store."""

from ._helpers import company_runtime_route_sunset


def run(input_data, context):
    """Fail closed until a selected Company inbox contract is available."""

    del input_data, context
    return company_runtime_route_sunset("company inbox")
