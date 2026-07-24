"""Read-only legacy projection over gateway routing diagnostics."""

from blocks._common import error, ok
from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract


def run(input_data, context):
    del context
    registry = get_container().get_or_none("interface_registry")
    if registry is None:
        return error("interface registry is unavailable", "UNAVAILABLE")
    value = invoke_global_contract(
        registry,
        "rumi.resource.ai.routing.diagnostics.v1",
        "list",
        {"request_id": input_data.get("request_id")},
    )
    return ok(value)
