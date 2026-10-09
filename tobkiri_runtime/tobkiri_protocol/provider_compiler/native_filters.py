"""Verified OpenRouter filters; discovery and inference are separate authorities."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

CAPABILITY_REVISION = "openrouter.official-filters.2026-10-07.v1"
DISCOVERY_FIELDS = {"output_modalities", "supported_parameters", "category"}
ROUTING_FIELDS = {
    "only", "ignore", "order", "quantizations", "require_parameters",
    "data_collection", "zdr", "max_price", "sort",
}
_MODALITIES = {
    "text", "image", "embeddings", "audio", "video", "rerank", "decisions",
    "speech", "transcription",
}
_CATEGORIES = {
    "programming", "roleplay", "marketing", "marketing/seo", "technology",
    "science", "translation", "legal", "finance", "health", "trivia", "academia",
}
_QUANTIZATIONS = {"int4", "int8", "fp4", "mxfp4", "nvfp4", "fp6", "fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown"}
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def is_official_openrouter(connection: Mapping[str, Any]) -> bool:
    """Require the actual TLS origin, protocol, and catalog identity to agree."""
    metadata = connection.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    endpoint = urlsplit(str(connection.get("endpoint") or ""))
    return (
        connection.get("adapter_id") in {"openai", "openai-compatible"}
        and metadata.get("catalog_provider_id") == "openrouter"
        and endpoint.scheme == "https"
        and endpoint.hostname == "openrouter.ai"
        and endpoint.port in {None, 443}
        and endpoint.username is None
        and endpoint.password is None
        and endpoint.path.rstrip("/") == "/api/v1"
        and not endpoint.query and not endpoint.fragment
    )


def _tokens(value: Any, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValueError("native filter list is invalid")
    if any(
        not isinstance(item, str) or not _SAFE_TOKEN.fullmatch(item)
        or (allowed is not None and item not in allowed)
        for item in value
    ):
        raise ValueError("native filter value is invalid")
    return list(dict.fromkeys(value))


def normalize_native_filters(
    value: Any, *, verified_openrouter: bool,
) -> dict[str, Any]:
    """Reject unknown revisions/providers and normalize documented fields only."""
    if value is None:
        return {}
    if not verified_openrouter:
        raise ValueError("native filters are unverified for this connection")
    if not isinstance(value, Mapping) or set(value) - {
        "revision", "discovery", "routing",
    } or value.get("revision") != CAPABILITY_REVISION:
        raise ValueError("native filter capability revision is unknown")
    discovery = value.get("discovery", {})
    routing = value.get("routing", {})
    if not isinstance(discovery, Mapping) or set(discovery) - DISCOVERY_FIELDS:
        raise ValueError("native discovery filter is unknown")
    if not isinstance(routing, Mapping) or set(routing) - ROUTING_FIELDS:
        raise ValueError("native routing filter is unknown")
    result_discovery: dict[str, Any] = {}
    for key, item in discovery.items():
        if key == "category":
            if not isinstance(item, str) or item not in _CATEGORIES:
                raise ValueError("native category is invalid")
            result_discovery[key] = item
        else:
            allowed = _MODALITIES | {"all"} if key == "output_modalities" else None
            tokens = _tokens(item, allowed)
            if "all" in tokens and tokens != ["all"]:
                raise ValueError("all modalities cannot be combined")
            result_discovery[key] = tokens
    result_routing: dict[str, Any] = {}
    for key, item in routing.items():
        if key in {"only", "ignore", "order", "quantizations"}:
            result_routing[key] = _tokens(
                item, _QUANTIZATIONS if key == "quantizations" else None,
            )
        elif key in {"require_parameters", "zdr"}:
            if type(item) is not bool:
                raise ValueError("native boolean is invalid")
            result_routing[key] = item
        elif key in {"data_collection", "sort"}:
            choices = {"allow", "deny"} if key == "data_collection" else {
                "price", "latency", "throughput",
            }
            if not isinstance(item, str) or item not in choices:
                raise ValueError("native enum is invalid")
            result_routing[key] = item
        else:
            if not isinstance(item, Mapping) or not item or set(item) - {
                "prompt", "completion", "request", "image",
            }:
                raise ValueError("native price fields are invalid")
            prices: dict[str, float] = {}
            for unit, price in item.items():
                if type(price) not in {int, float} or not math.isfinite(price) or price < 0:
                    raise ValueError("native price must be finite nonnegative USD")
                prices[unit] = float(price)
            result_routing[key] = prices
    if set(result_routing.get("only", [])) & set(result_routing.get("ignore", [])):
        raise ValueError("a provider cannot be allowed and excluded")
    return {
        "revision": CAPABILITY_REVISION,
        "discovery": result_discovery,
        "routing": result_routing,
    }


def discovery_url(native_filters: Mapping[str, Any]) -> str:
    """Build a public Models GET without credentials or a routing payload."""
    normalized = normalize_native_filters(native_filters, verified_openrouter=True)
    query = {"output_modalities": "all"}
    for key, value in normalized["discovery"].items():
        query[key] = ",".join(value) if isinstance(value, list) else value
    return "https://openrouter.ai/api/v1/models?" + urlencode(query)


def apply_native_routing(
    native_filters: Mapping[str, Any], parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile only documented Chat Completions provider routing fields."""
    normalized = normalize_native_filters(native_filters, verified_openrouter=True)
    if "provider" in parameters:
        raise ValueError("saved native routing cannot be overridden")
    result = dict(parameters)
    if normalized["routing"]:
        result["provider"] = {**normalized["routing"], "allow_fallbacks": False}
    return result
