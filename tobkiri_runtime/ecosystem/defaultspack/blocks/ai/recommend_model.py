import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.ai_client.model_search import recommend_model


def run(input_data, context, *, settings_owner=None):
    del context
    settings = ModelRuntimeSettingsService(
        settings_owner=settings_owner
    ).get_settings()
    return ok(
        recommend_model(
            input_data if isinstance(input_data, dict) else {},
            profiles=None,
            settings=settings,
        )
    )
