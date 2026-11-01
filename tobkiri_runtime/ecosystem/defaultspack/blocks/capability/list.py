

from blocks._common import ok
from domain.capability.catalog import CapabilityCatalog


def run(input_data, context=None):
    catalog = CapabilityCatalog()
    return ok(
        {
            "capabilities": catalog.capabilities(
                local_only=input_data.get("local_only"),
                risk_level=input_data.get("risk_level"),
            )
        }
    )
