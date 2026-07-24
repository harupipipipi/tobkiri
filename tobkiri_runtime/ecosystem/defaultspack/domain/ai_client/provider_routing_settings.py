from __future__ import annotations

from pathlib import Path
from typing import Any


GATEWAY_PROVIDER_IDS = {"openrouter", "vercel-ai-gateway"}
_GATEWAY_TARGET_ALIASES = {
    "": "all",
    "all": "all",
    "auto": "all",
    "gateway": "all",
    "gateways": "all",
    "openrouter": "openrouter",
    "open-router": "openrouter",
    "or": "openrouter",
    "vercel": "vercel-ai-gateway",
    "vercel-ai": "vercel-ai-gateway",
    "vercel-ai-gateway": "vercel-ai-gateway",
    "ai-gateway": "vercel-ai-gateway",
    "aigateway": "vercel-ai-gateway",
    "ai_gateway": "vercel-ai-gateway",
}
_PROVIDER_SLUG_ALIASES = {"auto": "", "default": ""}
_SORT_ALIASES = {
    "": "auto", "auto": "auto", "default": "auto",
    "fast": "throughput", "speed": "throughput", "throughput": "throughput",
    "tps": "throughput", "tokens/s": "throughput",
    "latency": "latency", "ttft": "latency",
    "cost": "cost", "price": "cost", "cheap": "cost",
}
_MODE_ALIASES = {
    "": "auto", "auto": "auto", "prefer": "prefer", "preferred": "prefer",
    "order": "prefer", "only": "only", "strict": "only",
}

DEFAULT_GATEWAY_ROUTING_SETTINGS: dict[str, Any] = {
    "gateway_routing_target": "all",
    "openrouter_provider_mode": "auto",
    "openrouter_primary_provider": "",
    "openrouter_provider_order": [],
    "openrouter_provider_only": [],
    "openrouter_provider_ignore": [],
    "vercel_provider_mode": "auto",
    "vercel_primary_provider": "",
    "vercel_provider_order": [],
    "vercel_provider_only": [],
    "gateway_provider_sort": "auto",
    "gateway_allow_fallbacks": True,
    "gateway_require_parameters": False,
    "gateway_min_tokens_per_second": 0.0,
    "gateway_max_latency_seconds": 0.0,
    "fast_mode_enabled": False,
    "fast_min_samples": 3,
}


def _pack_root(pack_root: Path | None = None) -> Path:
    return pack_root or Path(__file__).resolve().parents[2]


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _nonnegative_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return default


def _positive_int(value: Any, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def normalize_gateway_target(value: Any) -> str:
    token = str(value or "").strip().lower()
    return _GATEWAY_TARGET_ALIASES.get(token, token if token in GATEWAY_PROVIDER_IDS else "all")


def is_known_gateway_target(value: Any) -> bool:
    """Return whether a gateway target can be normalized without guessing."""
    return str(value or "").strip().lower() in _GATEWAY_TARGET_ALIASES


def normalize_provider_slug(value: Any) -> str:
    token = str(value or "").strip().lower()
    return _PROVIDER_SLUG_ALIASES.get(token, token)


def normalize_provider_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    output: list[str] = []
    for item in raw_items:
        slug = normalize_provider_slug(item)
        if slug and slug not in output:
            output.append(slug)
    return output


def normalize_gateway_routing_settings(values: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> dict[str, Any]:
    if values is None:
        try:
            from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
            values = ModelRuntimeSettingsService(_pack_root(pack_root)).get_settings()
        except Exception:
            values = {}
    raw_values = dict(values or {})
    target = normalize_gateway_target(raw_values.get("gateway_routing_target"))
    for provider_id, prefix in (
        ("openrouter", "openrouter"),
        ("vercel-ai-gateway", "vercel"),
    ):
        if target not in {provider_id, "all"}:
            continue
        for legacy_key, suffix in (
            ("gateway_provider_mode", "provider_mode"),
            ("gateway_primary_provider", "primary_provider"),
            ("gateway_provider_order", "provider_order"),
            ("gateway_provider_only", "provider_only"),
        ):
            destination = f"{prefix}_{suffix}"
            if destination not in raw_values and legacy_key in raw_values:
                raw_values[destination] = raw_values[legacy_key]
    if (
        target in {"openrouter", "all"}
        and "openrouter_provider_ignore" not in raw_values
        and "gateway_provider_ignore" in raw_values
    ):
        raw_values["openrouter_provider_ignore"] = raw_values[
            "gateway_provider_ignore"
        ]
    result = dict(DEFAULT_GATEWAY_ROUTING_SETTINGS)
    result.update(raw_values)
    result["gateway_routing_target"] = normalize_gateway_target(result.get("gateway_routing_target"))
    for key in ("openrouter_provider_mode", "vercel_provider_mode"):
        result[key] = _MODE_ALIASES.get(str(result.get(key) or "").strip().lower(), "auto")
    for key in ("openrouter_primary_provider", "vercel_primary_provider"):
        result[key] = normalize_provider_slug(result.get(key))
    for key in (
        "openrouter_provider_order", "openrouter_provider_only",
        "openrouter_provider_ignore", "vercel_provider_order", "vercel_provider_only",
    ):
        result[key] = normalize_provider_list(result.get(key))
    result["gateway_provider_sort"] = _SORT_ALIASES.get(str(result.get("gateway_provider_sort") or "").strip().lower(), "auto")
    result["gateway_allow_fallbacks"] = _coerce_bool(result.get("gateway_allow_fallbacks"), default=True)
    result["gateway_require_parameters"] = _coerce_bool(result.get("gateway_require_parameters"), default=False)
    result["fast_mode_enabled"] = _coerce_bool(result.get("fast_mode_enabled"), default=False)
    result["gateway_min_tokens_per_second"] = _nonnegative_float(result.get("gateway_min_tokens_per_second"))
    result["gateway_max_latency_seconds"] = _nonnegative_float(result.get("gateway_max_latency_seconds"))
    result["fast_min_samples"] = _positive_int(result.get("fast_min_samples"), default=3)
    return result


def update_gateway_routing_settings(patch: dict[str, Any], *, pack_root: Path | None = None) -> dict[str, Any]:
    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
    service = ModelRuntimeSettingsService(_pack_root(pack_root))
    normalized = normalize_gateway_routing_settings({**service.get_settings(), **dict(patch or {})}, pack_root=pack_root)
    return service.update_settings({key: normalized[key] for key in DEFAULT_GATEWAY_ROUTING_SETTINGS})


def _target_matches(provider_id: str, settings: dict[str, Any]) -> bool:
    target = str(settings.get("gateway_routing_target") or "all")
    return target == "all" or target == provider_id


def _ordered_slugs(settings: dict[str, Any], prefix: str) -> list[str]:
    primary = normalize_provider_slug(settings.get(f"{prefix}_primary_provider"))
    order = normalize_provider_list(settings.get(f"{prefix}_provider_order"))
    if primary and primary not in order:
        order.insert(0, primary)
    return order


def openrouter_provider_options(values: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> dict[str, Any]:
    settings = normalize_gateway_routing_settings(values, pack_root=pack_root)
    if not _target_matches("openrouter", settings):
        return {}
    mode = str(settings.get("openrouter_provider_mode") or "auto")
    primary = normalize_provider_slug(settings.get("openrouter_primary_provider"))
    order = _ordered_slugs(settings, "openrouter")
    only = normalize_provider_list(settings.get("openrouter_provider_only"))
    if mode == "only" and primary and primary not in only:
        only.insert(0, primary)
    options: dict[str, Any] = {}
    if mode == "prefer" and order:
        options["order"] = order
    elif mode == "only" and only:
        options["only"] = only
    elif settings.get("openrouter_provider_order"):
        options["order"] = order
    elif settings.get("openrouter_provider_only"):
        options["only"] = only
    ignored = normalize_provider_list(settings.get("openrouter_provider_ignore"))
    if ignored:
        options["ignore"] = ignored
    sort_mode = str(settings.get("gateway_provider_sort") or "auto")
    if settings.get("fast_mode_enabled") and sort_mode == "auto":
        sort_mode = "throughput"
    if sort_mode != "auto":
        options["sort"] = {"cost": "price", "throughput": "throughput", "latency": "latency"}[sort_mode]
    if not settings.get("gateway_allow_fallbacks", True):
        options["allow_fallbacks"] = False
    if settings.get("gateway_require_parameters"):
        options["require_parameters"] = True
    minimum_tps = _nonnegative_float(settings.get("gateway_min_tokens_per_second"))
    if minimum_tps > 0:
        options["preferred_min_throughput"] = {"p90": minimum_tps}
    maximum_latency = _nonnegative_float(settings.get("gateway_max_latency_seconds"))
    if maximum_latency > 0:
        options["preferred_max_latency"] = {"p90": maximum_latency}
    return options


def vercel_gateway_options(values: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> dict[str, Any]:
    settings = normalize_gateway_routing_settings(values, pack_root=pack_root)
    if not _target_matches("vercel-ai-gateway", settings):
        return {}
    mode = str(settings.get("vercel_provider_mode") or "auto")
    primary = normalize_provider_slug(settings.get("vercel_primary_provider"))
    order = _ordered_slugs(settings, "vercel")
    only = normalize_provider_list(settings.get("vercel_provider_only"))
    if mode == "only" and primary and primary not in only:
        only.insert(0, primary)
    options: dict[str, Any] = {}
    if mode == "prefer" and order:
        options["order"] = order
    elif mode == "only" and only:
        options["only"] = only
    elif settings.get("vercel_provider_order"):
        options["order"] = order
    elif settings.get("vercel_provider_only"):
        options["only"] = only
    sort_mode = str(settings.get("gateway_provider_sort") or "auto")
    if settings.get("fast_mode_enabled") and sort_mode == "auto":
        sort_mode = "throughput"
    if sort_mode != "auto":
        options["sort"] = {"cost": "cost", "throughput": "tps", "latency": "ttft"}[sort_mode]
    return options


def gateway_routing_summary(values: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> dict[str, Any]:
    settings = normalize_gateway_routing_settings(values, pack_root=pack_root)
    return {
        "target": settings["gateway_routing_target"],
        "openrouter": {
            "mode": settings["openrouter_provider_mode"],
            "primary_provider": settings["openrouter_primary_provider"],
            "order": settings["openrouter_provider_order"],
            "only": settings["openrouter_provider_only"],
            "ignore": settings["openrouter_provider_ignore"],
        },
        "vercel": {
            "mode": settings["vercel_provider_mode"],
            "primary_provider": settings["vercel_primary_provider"],
            "order": settings["vercel_provider_order"],
            "only": settings["vercel_provider_only"],
        },
        "sort": settings["gateway_provider_sort"],
        "allow_fallbacks": settings["gateway_allow_fallbacks"],
        "require_parameters": settings["gateway_require_parameters"],
        "minimum_tokens_per_second": settings["gateway_min_tokens_per_second"],
        "maximum_latency_seconds": settings["gateway_max_latency_seconds"],
        "fast_mode_enabled": settings["fast_mode_enabled"],
        "fast_min_samples": settings["fast_min_samples"],
        "openrouter_request": openrouter_provider_options(settings),
        "vercel_gateway_request": vercel_gateway_options(settings),
    }
