from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


CANONICAL_MODEL_CAPABILITY_KEYS = {
    "text_input",
    "image_input",
    "audio_input",
    "text_output",
    "tool_calling",
    "parallel_tool_calls",
    "json_schema",
    "structured_output",
    "thinking",
    "streaming",
}
CANONICAL_REQUEST_FEATURE_KEYS = {
    "json_mode",
    "response_format",
    "tool_choice",
}
MODEL_ENDPOINT_TYPES = {
    "chat",
    "embedding",
    "tts",
    "transcription",
    "moderation",
}
LEGACY_CONTEXT_KEYS = ("max_context", "max_context_tokens")
CONTEXT_KEYS = ("context_window", *LEGACY_CONTEXT_KEYS)
DEFAULTS_CAPABILITY_OVERLAP_KEYS = {"chat", "vision", "reasoning", "fast"}

_CAPABILITY_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "audio": ("audio_input",),
    "input_audio": ("audio_input",),
    "image": ("image_input",),
    "images": ("image_input",),
    "multimodal": ("image_input",),
    "native_tool_calling": ("tool_calling",),
    "reasoning": ("thinking",),
    "supports_audio": ("audio_input",),
    "supports_audio_input": ("audio_input",),
    "supports_image_input": ("image_input",),
    "supports_reasoning": ("thinking",),
    "supports_thinking": ("thinking",),
    "supports_tool_calling": ("tool_calling",),
    "supports_vision": ("image_input",),
    "tool_calls": ("tool_calling",),
    "tools": ("tool_calling",),
    "vision": ("image_input",),
}
_REQUEST_FEATURE_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "function_calling": ("tool_choice",),
    "json_mode": ("json_mode",),
    "response_format": ("response_format",),
    "structured_outputs": ("response_format",),
    "tool_choice": ("tool_choice",),
}


class ModelMetadataSchemaError(ValueError):
    pass


def normalize_capability_map(raw: Any) -> dict[str, Any]:
    items: Iterable[tuple[object, object]]
    if isinstance(raw, dict):
        items = ((key, value) for key, value in raw.items())
    elif isinstance(raw, (list, tuple, set)):
        items = ((str(item), True) for item in raw if str(item or "").strip())
    else:
        items = ()

    normalized: dict[str, Any] = {}
    for key, value in items:
        name = str(key or "").strip()
        if not name:
            continue
        canonical_names = _CAPABILITY_ALIAS_MAP.get(name, (name,))
        for canonical in canonical_names:
            if canonical in CANONICAL_MODEL_CAPABILITY_KEYS:
                normalized[canonical] = value
    if normalized.get("text_input") is None:
        normalized.setdefault("text_input", True)
    if normalized.get("text_output") is None:
        normalized.setdefault("text_output", True)
    return normalized


def normalize_request_features(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        for canonical in _REQUEST_FEATURE_ALIAS_MAP.get(name, (name,)):
            if canonical in CANONICAL_REQUEST_FEATURE_KEYS:
                normalized[canonical] = value
    return normalized


def normalize_routing_defaults(model: dict[str, Any]) -> dict[str, bool]:
    routing_value = model.get("routing")
    routing: dict[str, object] = dict(routing_value) if isinstance(routing_value, dict) else {}
    defaults_value = model.get("defaults")
    defaults: dict[str, object] = dict(defaults_value) if isinstance(defaults_value, dict) else {}
    default_for = routing.get("default_for")
    if isinstance(default_for, str):
        values = [default_for]
    elif isinstance(default_for, list):
        values = [str(item) for item in default_for if str(item or "").strip()]
    else:
        values = [str(key) for key, value in defaults.items() if bool(value)]
    result = {value: True for value in values if value}
    speed_tier = str(routing.get("speed_tier") or "").strip().lower()
    if speed_tier == "fast":
        result.setdefault("fast", True)
    return result


def context_window_value(model: dict[str, Any], *, default: int = 0) -> int:
    raw_values = {
        key: model.get(key)
        for key in CONTEXT_KEYS
        if key in model and model.get(key) not in (None, "")
    }
    if not raw_values:
        return default
    values: dict[str, int] = {}
    for key, raw in raw_values.items():
        try:
            if isinstance(raw, bool):
                values[key] = int(raw)
            elif isinstance(raw, (int, float, str)):
                values[key] = int(raw)
            else:
                raise TypeError
        except (TypeError, ValueError) as exc:
            raise ModelMetadataSchemaError(f"{key} must be an integer") from exc
    unique = set(values.values())
    if len(unique) != 1:
        raise ModelMetadataSchemaError(f"context aliases disagree: {values}")
    return next(iter(unique))


def validate_model_catalog_source(payload: Any, *, path: Path | str | None = None) -> None:
    label = str(path or "<models.json>")
    if isinstance(payload, dict):
        models = payload.get("models")
    else:
        models = payload
    if not isinstance(models, list):
        raise ModelMetadataSchemaError(f"{label}: models must be an array")

    seen_ids: set[str] = set()
    seen_provider_models: set[tuple[str, str]] = set()
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            raise ModelMetadataSchemaError(f"{label}: models[{index}] must be an object")
        full_id = str(model.get("id") or "").strip()
        provider_id = str(model.get("provider_id") or "").strip()
        model_id = str(model.get("model_id") or "").strip()
        if not full_id:
            raise ModelMetadataSchemaError(f"{label}: models[{index}].id is required")
        if full_id in seen_ids:
            raise ModelMetadataSchemaError(f"{label}: duplicate model id: {full_id}")
        seen_ids.add(full_id)
        if provider_id and model_id:
            key = (provider_id, model_id)
            if key in seen_provider_models:
                raise ModelMetadataSchemaError(f"{label}: duplicate provider/model id: {provider_id}/{model_id}")
            seen_provider_models.add(key)

        model_type = str(model.get("type") or "chat").strip()
        if model_type not in MODEL_ENDPOINT_TYPES:
            raise ModelMetadataSchemaError(f"{label}: {full_id} has unsupported type: {model_type}")

        context_window_value(model, default=0)
        for legacy_key in LEGACY_CONTEXT_KEYS:
            if legacy_key in model:
                raise ModelMetadataSchemaError(f"{label}: {full_id} must use context_window, not {legacy_key}")

        capabilities = model.get("capabilities")
        if not isinstance(capabilities, dict):
            raise ModelMetadataSchemaError(f"{label}: {full_id}.capabilities must be an object")
        unknown = set(capabilities) - CANONICAL_MODEL_CAPABILITY_KEYS
        if unknown:
            raise ModelMetadataSchemaError(
                f"{label}: {full_id}.capabilities has unknown keys: {sorted(unknown)}"
            )

        request_features = model.get("request_features")
        if request_features is not None:
            if not isinstance(request_features, dict):
                raise ModelMetadataSchemaError(f"{label}: {full_id}.request_features must be an object")
            unknown_features = set(request_features) - CANONICAL_REQUEST_FEATURE_KEYS
            if unknown_features:
                raise ModelMetadataSchemaError(
                    f"{label}: {full_id}.request_features has unknown keys: {sorted(unknown_features)}"
                )

        defaults = model.get("defaults")
        if isinstance(defaults, dict) and DEFAULTS_CAPABILITY_OVERLAP_KEYS.intersection(defaults):
            raise ModelMetadataSchemaError(
                f"{label}: {full_id}.defaults overlaps capabilities/routing: "
                f"{sorted(DEFAULTS_CAPABILITY_OVERLAP_KEYS.intersection(defaults))}"
            )

        thinking = model.get("thinking")
        if thinking is not None:
            if not isinstance(thinking, dict):
                raise ModelMetadataSchemaError(f"{label}: {full_id}.thinking must be an object")
            levels = [str(item) for item in thinking.get("levels", []) if str(item or "").strip()]
            provider_mapping = thinking.get("provider_mapping")
            if levels and not isinstance(provider_mapping, dict):
                raise ModelMetadataSchemaError(f"{label}: {full_id}.thinking.provider_mapping is required")
            if isinstance(provider_mapping, dict):
                missing = [level for level in levels if level not in provider_mapping]
                if missing:
                    raise ModelMetadataSchemaError(
                        f"{label}: {full_id}.thinking.provider_mapping missing levels: {missing}"
                    )
