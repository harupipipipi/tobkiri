import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_provider_catalog


def run(input_data, context):
    del input_data, context
    providers = []
    for provider in list_provider_catalog():
        merged = dict(provider)
        merged["registered"] = bool(provider.get("configured"))
        if merged["registered"]:
            merged["runtime"] = {
                "source": "rumi.resource.ai.provider.registry.v1",
                "provider_instance_id": f"provider.{provider['provider_id']}",
            }
            merged["status"] = "registered"
        providers.append(merged)
    return ok({"providers": providers, "count": len(providers)})
