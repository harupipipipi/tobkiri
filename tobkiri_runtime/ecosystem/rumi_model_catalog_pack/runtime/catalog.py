"""Load verified static model catalogs and the bounded OpenRouter inventory."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping

CATALOG_REVISION = "sha256:23cd323554cef32f891827a9a6ddd9c75b7fd3c898d0b501c7e62b091a5001cd"
_ROOT = Path(__file__).resolve().parents[1] / "catalog" / "providers"
_EXTENSION_ROOT = Path(__file__).resolve().parents[1] / "extensions" / "llm" / "providers"
_OPENROUTER_PROVIDER_ID = "openrouter"
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_INVENTORY_TTL_SECONDS = 3600
_OPENROUTER_FETCH_TIMEOUT_SECONDS = 3
_OPENROUTER_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_OPENROUTER_MAX_MODELS = 5000
_OPENROUTER_MEMORY_INVENTORY: dict[str, Any] | None = None
_OPENROUTER_INVENTORY_LOCK = threading.Lock()


def create_model_catalog_operation(client: Any):
    """Create a read-only catalog operation independent of adapter runtime."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "list",
            "get",
            "providers",
            "rumi_model_catalog_pack.bundled-model-catalog.generate",
            "rumi_model_catalog_pack.bundled-model-catalog.stream",
        }:
            raise ValueError(f"unknown model catalog operation: {name}")
        providers, models = _load_catalog()
        provider_id = str(payload.get("provider_id") or "").strip()
        model_id = str(payload.get("model_id") or "").strip()
        inventory: dict[str, Any] = {}
        if provider_id == _OPENROUTER_PROVIDER_ID:
            models, inventory = _merge_openrouter_inventory(models)
        if provider_id:
            providers = [item for item in providers if item["provider_id"] == provider_id]
            models = [item for item in models if item["provider_id"] == provider_id]
        if model_id:
            models = [item for item in models if item["model_id"] == model_id]
        return {
            "catalog_revision": CATALOG_REVISION,
            "providers": providers,
            "models": models,
            "inventory": inventory,
        }

    return operation


def tobkiri_packvm_invoke(
    operation_id: object,
    payload: object,
) -> dict[str, Any]:
    """Execute only the sealed Catalog PackVM ABI operations.

    This module intentionally depends only on the standard library. The
    PackVM sandbox supplies no network and this entrypoint neither imports a
    Host provider nor selects any non-catalog capability.
    """

    allowed_operations = {
        "rumi_model_catalog_pack.bundled-model-catalog.generate",
        "rumi_model_catalog_pack.bundled-model-catalog.stream",
    }
    if not isinstance(operation_id, str) or operation_id not in allowed_operations:
        raise ValueError("PackVM model catalog operation is not permitted")
    if not isinstance(payload, Mapping):
        raise ValueError("PackVM model catalog payload must be an object")
    result = create_model_catalog_operation(None)(operation_id, payload)
    if not isinstance(result, dict):
        raise ValueError("PackVM model catalog result must be an object")
    return dict(result)


def _load_catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if _catalog_revision() != CATALOG_REVISION:
        raise RuntimeError("model catalog integrity mismatch")
    provider_items: dict[str, dict[str, Any]] = {}
    model_items: dict[tuple[str, str], dict[str, Any]] = {}
    for manifest_path in sorted(_ROOT.glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        provider_id = str(manifest.get("provider_id") or manifest.get("id") or "").strip()
        if not provider_id:
            raise ValueError("model catalog provider ID is missing")
        provider_metadata = manifest.get("provider_metadata")
        provider_metadata = provider_metadata if isinstance(provider_metadata, Mapping) else {}
        provider_manifest = manifest.get("provider_manifest")
        provider_manifest = provider_manifest if isinstance(provider_manifest, Mapping) else {}
        provider_items[provider_id] = {
            "provider_id": provider_id,
            "display_name": str(
                provider_metadata.get("display_name") or manifest.get("display_name") or provider_id
            ),
            "kind": str(provider_metadata.get("kind") or "unknown"),
            "capabilities": _strings(
                provider_metadata.get("catalog_features") or manifest.get("catalog_features")
            ),
            "execution_provider_instance_id": "provider.compatibility",
            "available": bool(provider_manifest.get("enabled", True)),
            "catalog_revision": CATALOG_REVISION,
        }
        model_path = manifest_path.parent / "models.json"
        if not model_path.is_file():
            continue
        raw_models = _read_json(model_path)
        raw_models = raw_models.get("models") if isinstance(raw_models, Mapping) else None
        if not isinstance(raw_models, list):
            raise ValueError("model catalog models payload is invalid")
        for raw in raw_models:
            if isinstance(raw, Mapping):
                item = _model(provider_id, raw, provider_manifest)
                model_items[(provider_id, item["model_id"])] = item
    for manifest_path in sorted(_EXTENSION_ROOT.glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        provider_id = str(manifest.get("id") or "").strip()
        if not provider_id:
            raise ValueError("extension model catalog provider ID is missing")
        provider_items[provider_id] = {
            "provider_id": provider_id,
            "display_name": str(manifest.get("display_name") or provider_id),
            "kind": str(manifest.get("kind") or "unknown"),
            "capabilities": _strings(manifest.get("catalog_features")),
            "execution_provider_instance_id": "provider.compatibility",
            "available": bool(manifest.get("enabled", True)),
            "catalog_revision": CATALOG_REVISION,
        }
        for model_path in sorted((manifest_path.parent / "models").glob("*.json")):
            raw = _read_json(model_path)
            item = _model(provider_id, raw, manifest)
            model_items[(provider_id, item["model_id"])] = item
    providers = list(provider_items.values())
    models = list(model_items.values())
    providers.sort(key=lambda item: item["provider_id"])
    models.sort(
        key=lambda item: (
            int(item["priority"]),
            item["provider_id"],
            item["model_id"],
        )
    )
    return providers, models


def _model(
    provider_id: str,
    value: Mapping[str, Any],
    provider_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    raw_model_id = str(value.get("model_id") or value.get("id") or "").strip()
    if not raw_model_id:
        raise ValueError("model catalog model ID is missing")
    capability_map = value.get("capabilities")
    capability_map = capability_map if isinstance(capability_map, Mapping) else {}
    capabilities = sorted(str(key) for key, enabled in capability_map.items() if enabled is True)
    modalities = ["text"]
    if capability_map.get("image_input"):
        modalities.append("image")
    if capability_map.get("audio_input"):
        modalities.append("audio")
    context_length = value.get("context_length", value.get("context_window", 0))
    pricing = value.get("pricing")
    pricing = pricing if isinstance(pricing, Mapping) else {}
    return {
        "model_id": str(value.get("id") or f"{provider_id}/{raw_model_id}"),
        "provider_model_id": raw_model_id,
        "provider_id": provider_id,
        "execution_provider_instance_id": "provider.compatibility",
        "health_provider_instance_id": f"provider.{provider_id}",
        "display_name": str(value.get("display_name") or raw_model_id),
        "capabilities": capabilities,
        "modalities": modalities,
        "context_length": _integer(context_length),
        "input_cost": _number(pricing.get("input")),
        "output_cost": _number(pricing.get("output")),
        "priority": _integer(value.get("priority"), default=100),
        "available": bool(value.get("enabled", True) and provider_manifest.get("enabled", True)),
        "data_residency": str(value.get("data_residency") or "unknown"),
        "catalog_revision": CATALOG_REVISION,
    }


def _merge_openrouter_inventory(
    models: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return only OpenRouter's verified live or last-known-good inventory.

    A bundled external model list becomes wrong as soon as a model is removed,
    renamed, repriced, or hidden from the current account.  In particular,
    expired ``:free`` variants must not survive as selectable models merely
    because they remain in an old pack artifact.
    """
    inventory_models, source, stale = _openrouter_inventory()
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in inventory_models:
        key = _model_key(item)
        if key is not None:
            by_key[key] = dict(item)
    merged = list(by_key.values())
    merged.sort(
        key=lambda item: (
            _integer(item.get("priority"), default=1000),
            str(item.get("provider_id") or ""),
            str(item.get("model_id") or ""),
        )
    )
    return merged, {
        _OPENROUTER_PROVIDER_ID: {
            "source": source,
            "stale": stale,
            "model_count": len(inventory_models),
            "static_models_ignored": len([item for item in models if _model_key(item) is not None]),
        }
    }


def _model_key(item: Mapping[str, Any]) -> tuple[str, str] | None:
    provider_id = str(item.get("provider_id") or "").strip()
    model_id = str(item.get("provider_model_id") or item.get("model_id") or "").strip()
    if provider_id == _OPENROUTER_PROVIDER_ID and model_id.startswith(f"{provider_id}/"):
        model_id = model_id[len(provider_id) + 1 :]
    return (provider_id, model_id) if provider_id and model_id else None


def _openrouter_inventory() -> tuple[list[dict[str, Any]], str, bool]:
    """Return fresh inventory, then stale last-known-good, then static fallback.

    The official public endpoint is used with no credential or user-configured
    URL. A caller never waits on an in-progress refresh, and a refresh itself
    has one bounded request with no retries.
    """
    now = int(time.time())
    memory = _valid_inventory(_OPENROUTER_MEMORY_INVENTORY)
    if memory is not None and _inventory_is_fresh(memory, now):
        return list(memory["models"]), "live", False

    persisted = _load_openrouter_inventory_cache()
    if persisted is not None and _inventory_is_fresh(persisted, now):
        _set_memory_inventory(persisted)
        return list(persisted["models"]), "last_known_good", False

    if _OPENROUTER_INVENTORY_LOCK.acquire(blocking=False):
        try:
            memory = _valid_inventory(_OPENROUTER_MEMORY_INVENTORY)
            if memory is not None and _inventory_is_fresh(memory, now):
                return list(memory["models"]), "live", False
            fetched = _fetch_openrouter_inventory()
            if fetched:
                snapshot = {
                    "version": 1,
                    "saved_at": now,
                    "expires_at": now + _OPENROUTER_INVENTORY_TTL_SECONDS,
                    "models": fetched,
                }
                _set_memory_inventory(snapshot)
                _save_openrouter_inventory_cache(snapshot)
                return list(fetched), "live", False
        finally:
            _OPENROUTER_INVENTORY_LOCK.release()

    fallback = memory or persisted
    if fallback is not None:
        return list(fallback["models"]), "last_known_good", True
    return [], "static", False


def _set_memory_inventory(snapshot: Mapping[str, Any]) -> None:
    global _OPENROUTER_MEMORY_INVENTORY
    _OPENROUTER_MEMORY_INVENTORY = {
        "version": 1,
        "saved_at": _integer(snapshot.get("saved_at")),
        "expires_at": _integer(snapshot.get("expires_at")),
        "models": [dict(item) for item in snapshot.get("models", [])],
    }


def _inventory_is_fresh(snapshot: Mapping[str, Any], now: int) -> bool:
    return _integer(snapshot.get("expires_at")) > now


def _valid_inventory(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    models = value.get("models")
    if not isinstance(models, list) or not models:
        return None
    normalized = [dict(item) for item in models if _is_catalog_model(item)]
    if len(normalized) != len(models):
        return None
    return {
        "version": _integer(value.get("version")),
        "saved_at": _integer(value.get("saved_at")),
        "expires_at": _integer(value.get("expires_at")),
        "models": normalized,
    }


def _is_catalog_model(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("provider_id") == _OPENROUTER_PROVIDER_ID
        and _safe_model_identifier(value.get("provider_model_id"))
        and str(value.get("model_id") or "")
        == f"{_OPENROUTER_PROVIDER_ID}/{value.get('provider_model_id')}"
        and isinstance(value.get("capabilities"), list)
    )


def _fetch_openrouter_inventory() -> list[dict[str, Any]]:
    """Fetch and normalize the public OpenRouter `/models` response once."""
    request = urllib.request.Request(
        _OPENROUTER_MODELS_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "Tobkiri-Model-Catalog/1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=_openrouter_fetch_timeout_seconds(),
        ) as response:
            status = getattr(response, "status", 200)
            if isinstance(status, int) and not 200 <= status < 300:
                return []
            headers = getattr(response, "headers", None)
            content_length = _integer(
                headers.get("Content-Length") if isinstance(headers, Mapping) else None
            )
            if content_length > _OPENROUTER_MAX_RESPONSE_BYTES:
                return []
            raw_bytes = response.read(_OPENROUTER_MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, urllib.error.HTTPError):
        return []
    if len(raw_bytes) > _OPENROUTER_MAX_RESPONSE_BYTES:
        return []
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return []
    raw_models = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(raw_models, list):
        return []
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_models[:_OPENROUTER_MAX_MODELS]:
        model = _normalize_openrouter_model(raw)
        provider_model_id = str(model.get("provider_model_id") or "") if model else ""
        if model is not None and provider_model_id not in seen:
            seen.add(provider_model_id)
            models.append(model)
    return models


def _normalize_openrouter_model(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    provider_model_id = str(raw.get("id") or "").strip()
    if not _safe_model_identifier(provider_model_id):
        return None
    architecture = raw.get("architecture")
    architecture = architecture if isinstance(architecture, Mapping) else {}
    top_provider = raw.get("top_provider")
    top_provider = top_provider if isinstance(top_provider, Mapping) else {}
    input_modalities = _safe_string_list(
        architecture.get("input_modalities") or raw.get("input_modalities")
    )
    output_modalities = _safe_string_list(
        architecture.get("output_modalities") or raw.get("output_modalities")
    )
    modality_tokens = _modality_tokens(architecture.get("modality") or raw.get("modality"))
    input_tokens = set(input_modalities) | modality_tokens
    output_tokens = set(output_modalities) | modality_tokens
    parameter_tokens = set(_safe_string_list(raw.get("supported_parameters")))
    text_capable = "text" in input_tokens and "text" in output_tokens
    supports_reasoning = bool(
        parameter_tokens.intersection({"reasoning", "reasoning_effort", "include_reasoning"})
    )
    supports_tools = bool(
        parameter_tokens.intersection({"tools", "tool_choice", "parallel_tool_calls"})
    )
    supports_structured = bool(
        parameter_tokens.intersection(
            {"response_format", "structured_outputs", "json_schema", "json_mode"}
        )
    )
    capabilities = {
        "audio_input": "audio" in input_tokens,
        "chat": text_capable,
        "image_input": "image" in input_tokens,
        "json_schema": bool(parameter_tokens.intersection({"structured_outputs", "json_schema"})),
        "parallel_tool_calls": "parallel_tool_calls" in parameter_tokens,
        "reasoning": supports_reasoning,
        "streaming": text_capable,
        "structured_output": supports_structured,
        "text_input": text_capable,
        "text_output": text_capable,
        "thinking": supports_reasoning,
        "tool_calling": supports_tools,
        "tool_calls": supports_tools,
        "vision": "image" in input_tokens,
    }
    if text_capable:
        model_type = "reasoning" if supports_reasoning else "chat"
    elif "embedding" in output_tokens or "embeddings" in output_tokens:
        model_type = "embedding"
    elif "image" in output_tokens:
        model_type = "image"
    elif "audio" in output_tokens:
        model_type = "audio"
    else:
        model_type = "unknown"
    context_length = _bounded_positive_int(
        raw.get("context_length")
        or top_provider.get("context_length")
        or top_provider.get("max_context_length")
    )
    pricing = raw.get("pricing")
    pricing = pricing if isinstance(pricing, Mapping) else {}
    display_name = _safe_display_name(
        raw.get("name") or provider_model_id,
        fallback=provider_model_id,
    )
    return {
        "model_id": f"{_OPENROUTER_PROVIDER_ID}/{provider_model_id}",
        "provider_model_id": provider_model_id,
        "provider_id": _OPENROUTER_PROVIDER_ID,
        "execution_provider_instance_id": "provider.compatibility",
        "health_provider_instance_id": "provider.openrouter",
        "display_name": display_name,
        "type": model_type,
        "capabilities": sorted(key for key, enabled in capabilities.items() if enabled),
        "modalities": [
            "text",
            *(["image"] if "image" in input_tokens else []),
            *(["audio"] if "audio" in input_tokens else []),
        ],
        "context_length": context_length,
        "input_cost": _non_negative_number(pricing.get("prompt", pricing.get("input"))),
        "output_cost": _non_negative_number(pricing.get("completion", pricing.get("output"))),
        "priority": 1000,
        "available": True,
        "data_residency": "unknown",
        "catalog_revision": CATALOG_REVISION,
        "metadata": {
            "capability_confidence": "provider_reported",
            "capability_source": "openrouter_models_api",
            "input_modalities": input_modalities,
            "inventory_source": "openrouter_models_api",
            "output_modalities": output_modalities,
            "source": "openrouter_models_api",
            "source_endpoint": "/models",
        },
    }


def _safe_model_identifier(value: Any) -> bool:
    model_id = str(value or "").strip()
    return (
        1 <= len(model_id) <= 255
        and "/" in model_id
        and model_id.isprintable()
        and not any(character.isspace() for character in model_id)
    )


def _safe_display_name(value: Any, *, fallback: str) -> str:
    display_name = str(value or "").strip()
    if not display_name or not display_name.isprintable():
        return fallback
    return display_name[:240]


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            item.strip().lower()
            for item in (str(entry or "") for entry in value)
            if item.strip() and item.isprintable() and len(item.strip()) <= 64
        }
    )


def _modality_tokens(value: Any) -> set[str]:
    raw = str(value or "").strip().lower()
    if not raw or len(raw) > 256 or not raw.isprintable():
        return set()
    return {
        token.strip()
        for token in raw.replace("->", ",").replace("+", ",").split(",")
        if token.strip()
    }


def _bounded_positive_int(value: Any) -> int:
    return min(20_000_000, max(0, _integer(value)))


def _non_negative_number(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and result >= 0 else None


def _openrouter_fetch_timeout_seconds() -> int:
    value = _integer(
        os.environ.get(
            "RUMI_MODEL_CATALOG_OPENROUTER_TIMEOUT_SECONDS",
            _OPENROUTER_FETCH_TIMEOUT_SECONDS,
        ),
        default=_OPENROUTER_FETCH_TIMEOUT_SECONDS,
    )
    return min(5, max(1, value))


def _openrouter_inventory_cache_path() -> Path:
    configured = str(os.environ.get("RUMI_USER_DATA") or "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        root = Path(__file__).resolve().parents[3] / "user_data"
    return root / "packs" / "rumi_model_catalog_pack" / "openrouter.inventory.lkg.json"


def _load_openrouter_inventory_cache() -> dict[str, Any] | None:
    path = _openrouter_inventory_cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return _valid_inventory(payload)


def _save_openrouter_inventory_cache(snapshot: Mapping[str, Any]) -> None:
    path = _openrouter_inventory_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except OSError:
        return


def _catalog_revision() -> str:
    lines = []
    pack_root = _ROOT.parent.parent
    paths = list(_ROOT.glob("*/*.json"))
    paths.extend(_EXTENSION_ROOT.glob("**/*.json"))
    for path in sorted(paths):
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest = hashlib.sha256(content).hexdigest()
        relative_path = path.relative_to(pack_root).as_posix()
        lines.append(f"{digest}  {relative_path}\n")
    return "sha256:" + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("model catalog resource is not an object")
    return value


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if str(item).strip()})


def _integer(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
