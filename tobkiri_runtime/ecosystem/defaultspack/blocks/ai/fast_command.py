from __future__ import annotations

from typing import Any

from blocks._common import ok
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.ai_client.provider_performance import select_fast_model
from domain.ai_client.provider_routing_settings import (
    GATEWAY_PROVIDER_IDS,
    gateway_routing_summary,
    update_gateway_routing_settings,
)


def _enabled(value: Any, *, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    token = str(value or "").strip().lower()
    if not token:
        return default
    if token in {"1", "true", "yes", "on", "enable", "enabled", "fast"}:
        return True
    if token in {"0", "false", "no", "off", "disable", "disabled", "normal"}:
        return False
    return default


def run(input_data: Any, context: dict[str, Any]) -> dict[str, Any]:
    del context
    data = input_data if isinstance(input_data, dict) else {}
    enabled = _enabled(data.get("enabled"), default=True)
    settings = update_gateway_routing_settings({"fast_mode_enabled": enabled})
    summary = gateway_routing_summary(settings)
    if not enabled:
        summary["message"] = "Disabled Fast mode and restored normal gateway routing."
        return ok(summary)

    model_settings = ModelRuntimeSettingsService()
    current_model = model_settings.get_preferred_model()
    current_provider = current_model.split("/", 1)[0].lower()
    if current_provider in GATEWAY_PROVIDER_IDS:
        summary["direct_selection"] = {
            "selected_model": current_model,
            "changed": False,
            "reason": "GATEWAY_NATIVE_SPEED_ROUTING",
        }
    else:
        from domain.ai_client.providers import (
            detect_available_providers,
            get_all_known_models,
        )

        selection = select_fast_model(
            get_all_known_models(),
            detect_available_providers(),
            current_model=current_model,
            min_samples=int(settings.get("fast_min_samples") or 3),
            required_context_tokens=int(data.get("required_context_tokens") or 0),
            requires_tools=bool(data.get("requires_tools")),
            requires_image=bool(data.get("requires_image")),
        )
        if selection.get("changed"):
            model_settings.set_preferred_model(str(selection["selected_model"]))
        summary["direct_selection"] = selection
    summary["message"] = (
        "Enabled Fast mode. OpenRouter uses throughput routing and Vercel AI "
        "Gateway uses TPS routing; direct providers use successful measured "
        "tokens/second samples."
    )
    return ok(summary)
