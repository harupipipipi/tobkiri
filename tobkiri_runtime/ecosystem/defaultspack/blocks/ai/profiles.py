

from blocks._common import ok
from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog


def run(input_data, context):
    del input_data, context
    profiles = list_profile_catalog()
    return ok({"profiles": profiles, "count": len(profiles)})
