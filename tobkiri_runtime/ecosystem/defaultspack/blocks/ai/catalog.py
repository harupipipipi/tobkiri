
from blocks._common import ok
from ecosystem.defaultspack.backend.ai_client.provider_catalog import (
    list_model_catalog,
    list_profile_catalog,
    list_provider_catalog,
)


def run(input_data, context):
    return ok(
        {
            "providers": list_provider_catalog(),
            "models": list_model_catalog(provider=input_data.get("provider", "")),
            "profiles": list_profile_catalog(),
        }
    )
