
from blocks._common import ok
from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_provider_catalog


def run(input_data, context):
    del input_data, context
    providers = list_provider_catalog()
    return ok({"providers": providers, "count": len(providers)})
