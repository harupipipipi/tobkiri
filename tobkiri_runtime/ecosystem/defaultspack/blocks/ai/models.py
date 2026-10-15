
from blocks._common import ok
from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog


def run(input_data, context):
    del context
    provider = input_data.get("provider") or input_data.get("provider_id") or ""
    models = list_model_catalog(provider=provider)
    return ok({"models": models, "count": len(models)})
