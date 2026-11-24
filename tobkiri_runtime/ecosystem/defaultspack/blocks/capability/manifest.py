

from blocks._common import error, ok
from domain.capability.catalog import CapabilityCatalog


def run(input_data, context=None):
    capability_id = input_data.get("capability_id") or input_data.get("id")
    catalog = CapabilityCatalog()
    if capability_id:
        capability = catalog.capability(str(capability_id))
        if capability is None:
            response = error("Capability not found", code="NOT_FOUND")
            response["_http_status"] = 404
            return response
        return ok(capability)
    return ok(catalog.manifest())
