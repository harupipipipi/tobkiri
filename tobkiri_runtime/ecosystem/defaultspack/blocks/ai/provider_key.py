import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.ai_client.api_key_store import (
    delete_custom_provider,
    delete_provider_api_key,
    list_custom_providers,
    provider_key_status,
    register_custom_provider,
    rename_provider_api_key,
    set_provider_api_key,
)
from domain.ai_client.model_availability import ModelAvailabilityService


def run(input_data, context):
    del context
    method = (input_data or {}).get("_method", "GET").upper()
    if method == "GET":
        return ok({
            "providers": provider_key_status(),
            "custom_providers": list_custom_providers(),
        })
    if method == "POST":
        action = str((input_data or {}).get("action", "upsert")).strip().lower()
        provider_id = str((input_data or {}).get("provider_id", "")).strip()
        api_id = str((input_data or {}).get("api_id", "")).strip()
        name = str((input_data or {}).get("name", "")).strip()
        kind_value = (input_data or {}).get("kind")
        if action == "register_provider":
            label = str((input_data or {}).get("label") or provider_id).strip()
            result = register_custom_provider(
                provider_id,
                label=label or None,
                kind=str(kind_value or "").strip() or None,
            )
            if not result.get("success"):
                return error(result.get("error") or "failed to register provider", "PROVIDER_REGISTER_FAILED")
            return ok({key: value for key, value in result.items() if key != "error"})
        if action == "delete_provider":
            result = delete_custom_provider(provider_id)
            if not result.get("success"):
                return error(result.get("error") or "failed to delete provider", "PROVIDER_DELETE_FAILED")
            return ok({key: value for key, value in result.items() if key != "error"})
        if action == "delete":
            result = delete_provider_api_key(provider_id, api_id)
        elif action == "rename":
            new_api_id = str((input_data or {}).get("new_api_id", "")).strip()
            result = rename_provider_api_key(
                provider_id,
                api_id,
                name,
                new_api_id=new_api_id or None,
                base_url=(input_data or {}).get("base_url"),
                allowed_models=(input_data or {}).get("allowed_models"),
                default_model=(input_data or {}).get("default_model"),
                notes=(input_data or {}).get("notes"),
                quota_label=(input_data or {}).get("quota_label"),
                kind=str(kind_value or "").strip() or None,
            )
        else:
            value = str((input_data or {}).get("value", ""))
            result = set_provider_api_key(
                provider_id,
                value,
                api_id=api_id or None,
                name=name or None,
                base_url=(input_data or {}).get("base_url"),
                allowed_models=(input_data or {}).get("allowed_models"),
                default_model=(input_data or {}).get("default_model"),
                notes=(input_data or {}).get("notes"),
                quota_label=(input_data or {}).get("quota_label"),
                kind=str(kind_value or "").strip() or None,
                credential_mode=(input_data or {}).get("credential_mode"),
            )
        if not result.get("success"):
            return error(result.get("error") or "failed to save api key", "API_KEY_SAVE_FAILED")
        # A key can activate a remote model inventory.  Do not serve the previous
        # 30-second Settings/composer profile cache after changing credentials.
        try:
            from domain.frontend.registry import FrontendRegistry

            FrontendRegistry.invalidate_selectable_model_profiles()
        except Exception:
            pass
        payload = {key: value for key, value in result.items() if key != "error"}
        if (
            action in {"upsert", "rename"}
            and payload.get("configured")
            and str(payload.get("kind") or "llm").strip().lower() != "custom"
        ):
            payload["model_availability"] = ModelAvailabilityService().after_provider_key_saved(
                str(payload.get("provider_id") or provider_id),
                str(payload.get("api_id") or api_id or "default"),
                default_model=str((input_data or {}).get("default_model") or payload.get("default_model") or ""),
                allowed_models=(input_data or {}).get("allowed_models", payload.get("allowed_models")),
            )
        return ok(payload)
    return error("unsupported method", "METHOD_NOT_ALLOWED")
