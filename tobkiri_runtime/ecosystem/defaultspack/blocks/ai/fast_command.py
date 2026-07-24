from __future__ import annotations

from typing import Any

from blocks._common import ok
from domain.ai_client.provider_routing_settings import (
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
    if enabled:
        summary["message"] = (
            "Enabled Fast mode. OpenRouter uses throughput routing and "
            "Vercel AI Gateway uses TPS routing."
        )
    else:
        summary["message"] = "Disabled Fast mode and restored normal gateway routing."
    return ok(summary)
