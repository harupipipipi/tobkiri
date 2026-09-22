from __future__ import annotations


from blocks._common import ok


from domain.coding.frontend_precision import frontend_command_payload


def run(input_data, context=None):
    payload = frontend_command_payload(input_data if isinstance(input_data, dict) else {}, context or {})
    return ok(payload)
