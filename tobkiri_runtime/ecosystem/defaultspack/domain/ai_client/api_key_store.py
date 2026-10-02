from __future__ import annotations

import os
import re
import json
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

from .oauth_store import provider_has_oauth_connection, provider_oauth_status
from .provider_program import provider_program_manifests


PROVIDER_SECRET_KEYS: Dict[str, List[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "ai21": ["AI21_API_KEY"],
    "assemblyai": ["ASSEMBLYAI_API_KEY"],
    "avian": ["AVIAN_API_KEY"],
    "azure-openai": ["AZURE_OPENAI_API_KEY"],
    "baidu-qianfan": ["QIANFAN_API_KEY"],
    "black-forest-labs": ["BFL_API_KEY"],
    "alibaba-dashscope": ["DASHSCOPE_API_KEY"],
    "cerebras": ["CEREBRAS_API_KEY"],
    "cohere": ["COHERE_API_KEY"],
    "cloudflare-workers-ai": ["CLOUDFLARE_API_TOKEN"],
    "deepgram": ["DEEPGRAM_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "deepinfra": ["DEEPINFRA_API_KEY"],
    "databricks-model-serving": ["DATABRICKS_TOKEN"],
    "elevenlabs": ["ELEVENLABS_API_KEY"],
    "fireworks": ["FIREWORKS_API_KEY"],
    "fal-ai": ["FAL_KEY", "FAL_AI_API_KEY"],
    "friendli": ["FRIENDLI_API_KEY"],
    "github-models": ["GITHUB_TOKEN", "GH_TOKEN"],
    "glm": ["GLM_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "hyperbolic": ["HYPERBOLIC_API_KEY"],
    "ibm-watsonx": ["WATSONX_API_KEY", "IBM_WATSONX_API_KEY"],
    "huggingface-inference": ["HF_TOKEN", "HUGGINGFACE_API_KEY"],
    "inference-net": ["INFERENCE_NET_API_KEY", "INFERENCENET_API_KEY"],
    "jina-ai": ["JINA_API_KEY"],
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "google-vertex-ai": ["VERTEX_AI_ACCESS_TOKEN", "GOOGLE_VERTEX_AI_ACCESS_TOKEN"],
    "gitlawb-opengateway": ["GITLAWB_OPENGATEWAY_API_KEY"],
    "genspark": ["GENSPARK_API_KEY"],
    "llama_cpp": ["LLAMACPP_API_KEY"],
    "litellm-proxy": ["LITELLM_API_KEY"],
    "lmstudio": ["LMSTUDIO_API_KEY"],
    "longcat": ["LONGCAT_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "moonshotai": ["MOONSHOT_API_KEY"],
    "nvidia": ["NVIDIA_API_KEY", "NGC_API_KEY"],
    "nebius": ["NEBIUS_API_KEY"],
    "novita": ["NOVITA_API_KEY"],
    "ollama": ["OLLAMA_API_KEY"],
    "opencode-go": ["OPENCODE_GO_API_KEY", "OPENCODE_ZEN_API_KEY"],
    "opencode-zen": ["OPENCODE_ZEN_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "openai_compatible": ["OPENAI_COMPATIBLE_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "perplexity": ["PERPLEXITY_API_KEY"],
    "portkey-ai-gateway": ["PORTKEY_API_KEY"],
    "replicate": ["REPLICATE_API_TOKEN"],
    "sambanova": ["SAMBANOVA_API_KEY"],
    "siliconflow": ["SILICONFLOW_API_KEY"],
    "stability-ai": ["STABILITY_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
    "tencent-hunyuan": ["HUNYUAN_API_KEY", "TENCENT_HUNYUAN_API_KEY"],
    "upstage": ["UPSTAGE_API_KEY"],
    "vercel-ai-gateway": ["AI_GATEWAY_API_KEY", "VERCEL_AI_GATEWAY_API_KEY"],
    "voyage-ai": ["VOYAGE_API_KEY"],
    "vllm": ["VLLM_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "xiaomi-token-plan-ams": [
        "XIAOMI_MIMO_TOKEN_PLAN_AMS_API_KEY",
    ],
    "xiaomi-token-plan-cn": [
        "XIAOMI_MIMO_TOKEN_PLAN_CN_API_KEY",
    ],
    "xiaomi-token-plan-sgp": [
        "XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY",
        "XIAOMI_MIMO_TOKEN_PLAN_API_KEY",
        "MIMO_API_KEY",
    ],
    "xiaomi-mimo-global": ["XIAOMI_MIMO_GLOBAL_API_KEY", "MIMO_API_KEY"],
}

_NAMED_API_PREFIX = "RUMIAPI"
_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9_]+")
_KIND_LLM = "llm"
_KIND_CUSTOM = "custom"
_VALID_KINDS = {_KIND_LLM, _KIND_CUSTOM}
_CREDENTIAL_MODE_API_KEY = "api_key"
_CREDENTIAL_MODE_NONE = "none"


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _secrets_dir(pack_root: Path | None = None) -> Path:
    if pack_root is None:
        # This is a routing override only; the value is a directory path and
        # never contains credential material.  Secret values remain broker
        # backed and are never read from the process environment.
        configured_override = os.getenv("RUMI_DEFAULTSPACK_SECRETS_DIR", "").strip()
        if configured_override:
            return Path(configured_override).expanduser()
        configured_user_data = os.getenv("RUMI_USER_DATA", "").strip()
        if configured_user_data:
            return Path(configured_user_data).expanduser() / "secrets"
    return (pack_root or _pack_root()) / "user_data" / "secrets"


def _metadata_path(pack_root: Path | None = None) -> Path:
    return _secrets_dir(pack_root) / "provider_api_keys.json"


def _custom_providers_path(pack_root: Path | None = None) -> Path:
    return _secrets_dir(pack_root) / "custom_providers.json"


def _normalize_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_KINDS else _KIND_LLM


def _normalize_credential_mode(value: Any) -> str:
    """Keep unauthenticated connections explicit rather than storing a fake key."""
    return _CREDENTIAL_MODE_NONE if str(value or "").strip().lower() in {
        "none", "no_auth", "no-auth", "unauthenticated"
    } else _CREDENTIAL_MODE_API_KEY


def _is_loopback_endpoint(value: Any) -> bool:
    """Only a loopback endpoint may be intentionally saved without a secret."""
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        host = str(parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _read_custom_providers(pack_root: Path | None = None) -> dict[str, dict[str, Any]]:
    path = _custom_providers_path(pack_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        provider_id = str(value.get("provider_id") or key or "").strip()
        if not provider_id:
            continue
        cleaned[provider_id] = {
            "provider_id": provider_id,
            "label": str(value.get("label") or provider_id).strip() or provider_id,
            "kind": _normalize_kind(value.get("kind")),
        }
    return cleaned


def _write_custom_providers(data: dict[str, dict[str, Any]], pack_root: Path | None = None) -> None:
    path = _custom_providers_path(pack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def list_custom_providers(*, pack_root: Path | None = None) -> list[dict[str, Any]]:
    return sorted(
        _read_custom_providers(pack_root).values(),
        key=lambda item: str(item.get("provider_id") or ""),
    )


def register_custom_provider(
    provider_id: str,
    *,
    label: str | None = None,
    kind: str | None = None,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    cleaned_id = _slug(provider_id, fallback="", max_length=18).lower()
    if not cleaned_id:
        return {"success": False, "error": "provider_id is required"}
    if cleaned_id in PROVIDER_SECRET_KEYS:
        # Built-in providers do not need to be registered as custom.
        return {
            "success": True,
            "provider_id": cleaned_id,
            "label": label or cleaned_id,
            "kind": _KIND_LLM,
            "builtin": True,
        }
    providers = _read_custom_providers(pack_root)
    providers[cleaned_id] = {
        "provider_id": cleaned_id,
        "label": str(label or cleaned_id).strip() or cleaned_id,
        "kind": _normalize_kind(kind),
    }
    _write_custom_providers(providers, pack_root)
    return {"success": True, **providers[cleaned_id]}


def delete_custom_provider(provider_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    cleaned_id = _slug(provider_id, fallback="", max_length=18).lower()
    if not cleaned_id:
        return {"success": False, "error": "provider_id is required"}
    providers = _read_custom_providers(pack_root)
    if cleaned_id not in providers:
        return {"success": True, "provider_id": cleaned_id, "missing": True}
    providers.pop(cleaned_id, None)
    _write_custom_providers(providers, pack_root)
    return {"success": True, "provider_id": cleaned_id, "deleted": True}


def _read_api_metadata(pack_root: Path | None = None) -> dict[str, dict[str, Any]]:
    path = _metadata_path(pack_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _write_api_metadata(data: dict[str, dict[str, Any]], pack_root: Path | None = None) -> None:
    path = _metadata_path(pack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _metadata_patch(
    *,
    provider_id: str,
    api_id: str,
    name: str,
    existing: dict[str, Any] | None = None,
    base_url: str | None = None,
    allowed_models: Any = None,
    default_model: str | None = None,
    notes: str | None = None,
    quota_label: str | None = None,
    monthly_budget_usd: float | None = None,
    monthly_request_limit: int | None = None,
    kind: str | None = None,
    credential_mode: str | None = None,
) -> dict[str, Any]:
    metadata = dict(existing or {})
    metadata.update(
        {
            "provider_id": str(provider_id or "").strip(),
            "api_id": str(api_id or "").strip(),
            "name": str(name or api_id or provider_id).strip(),
        }
    )
    optional_strings = {
        "base_url": base_url,
        "default_model": default_model,
        "notes": notes,
        "quota_label": quota_label,
    }
    for key, value in optional_strings.items():
        if value is None:
            continue
        cleaned = str(value or "").strip()
        if cleaned:
            metadata[key] = cleaned
        else:
            metadata.pop(key, None)

    # Numeric usage limits: positive number stored, otherwise removed.
    if monthly_budget_usd is not None:
        try:
            budget = float(monthly_budget_usd)
        except (TypeError, ValueError):
            budget = 0.0
        if budget > 0:
            metadata["monthly_budget_usd"] = budget
        else:
            metadata.pop("monthly_budget_usd", None)

    if monthly_request_limit is not None:
        try:
            limit = int(monthly_request_limit)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            metadata["monthly_request_limit"] = limit
        else:
            metadata.pop("monthly_request_limit", None)

    if allowed_models is not None:
        models = _normalize_allowed_models(allowed_models)
        if models:
            metadata["allowed_models"] = models
        else:
            metadata.pop("allowed_models", None)

    if kind is not None:
        normalized = _normalize_kind(kind)
        # Persist non-default kind only; default 'llm' is implied.
        if normalized == _KIND_LLM:
            metadata.pop("kind", None)
        else:
            metadata["kind"] = normalized
    if credential_mode is not None:
        normalized_mode = _normalize_credential_mode(credential_mode)
        if normalized_mode == _CREDENTIAL_MODE_NONE:
            metadata["credential_mode"] = normalized_mode
        else:
            metadata.pop("credential_mode", None)
    return metadata


def _normalize_allowed_models(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace(",", "\n").splitlines()
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        return []
    normalized: list[str] = []
    for item in raw_items:
        model_id = str(item or "").strip()
        if model_id and model_id not in normalized:
            normalized.append(model_id)
    return normalized


def _get_store(pack_root: Path | None = None):
    from core_runtime.secrets_store import SecretsStore

    return SecretsStore(str(_secrets_dir(pack_root)))


def _reset_ai_client() -> None:
    try:
        from domain.ai_client.client import AIClient

        AIClient._instance = None
    except Exception:
        pass


def _slug(value: str, *, fallback: str = "DEFAULT", max_length: int = 32) -> str:
    normalized = _SLUG_PATTERN.sub("_", str(value or "").strip()).strip("_").upper()
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        normalized = fallback
    return normalized[:max_length]


def named_provider_secret_key(provider_id: str, api_id: str | None = None, name: str | None = None) -> str:
    provider_slug = _slug(provider_id, fallback="PROVIDER", max_length=18)
    api_slug = _slug(api_id or name or "DEFAULT", fallback="DEFAULT", max_length=36)
    key = f"{_NAMED_API_PREFIX}_{provider_slug}_{api_slug}"
    return key[:64]


def _provider_from_named_key(key: str) -> str:
    prefix = f"{_NAMED_API_PREFIX}_"
    if not key.startswith(prefix):
        return ""
    remainder = key[len(prefix):]
    # Provider ids contain separators (for example ``azure-ai-foundry``).
    # Splitting on the first underscore silently turned a saved
    # ``RUMIAPI_AZURE_AI_FOUNDRY_MAIN`` credential into provider ``azure``.
    # Resolve the longest canonical provider slug instead.  The program list
    # is deliberately included because it contains providers without a legacy
    # environment-variable secret name.
    provider_ids = set(PROVIDER_SECRET_KEYS) | set(provider_program_manifests())
    candidates = sorted(
        (
            (_slug(provider_id, max_length=18).upper(), provider_id)
            for provider_id in provider_ids
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    upper_remainder = remainder.upper()
    for provider_slug, provider_id in candidates:
        if upper_remainder == provider_slug or upper_remainder.startswith(provider_slug + "_"):
            return provider_id
    return remainder.split("_", 1)[0].lower()


def _api_id_from_named_key(key: str, provider_id: str) -> str:
    provider_slug = _slug(provider_id, fallback="PROVIDER", max_length=18)
    prefix = f"{_NAMED_API_PREFIX}_{provider_slug}_"
    if key.startswith(prefix):
        return key[len(prefix):].lower()
    return key.lower()


def provider_secret_key(provider_id: str) -> str:
    keys = PROVIDER_SECRET_KEYS.get(str(provider_id or "").strip(), [])
    return keys[0] if keys else ""


def provider_secret_keys(provider_id: str) -> List[str]:
    return list(PROVIDER_SECRET_KEYS.get(str(provider_id or "").strip(), []))


def _read_secret_value(key: str, caller_id: str, *, pack_root: Path | None = None) -> str:
    return str(
        _get_store(pack_root)._internal_read_value(
            key,
            caller_id=caller_id,
        )
        or ""
    ).strip()


def _refresh_provider_env(provider_id: str, *, pack_root: Path | None = None) -> bool:
    """Invalidate provider instances after a broker-backed update.

    Credentials are intentionally not copied into ``os.environ``.  The old
    name is retained as a compatibility hook for callers that refresh the
    provider registry after saving a connection.
    """

    provider_id = str(provider_id or "").strip()
    configured = provider_has_api_key(provider_id, pack_root=pack_root)
    _reset_ai_client()
    return configured


def provider_has_api_key(provider_id: str, *, pack_root: Path | None = None) -> bool:
    provider_id = str(provider_id or "").strip()
    keys = provider_secret_keys(provider_id)
    if provider_id != "xiaomi-token-plan-sgp":
        # The unqualified MiMo key belongs only to the explicitly selected
        # SGP token-plan connection; it must not enable another region.
        keys = [key for key in keys if key != "MIMO_API_KEY"]
    for key in keys:
        secret_path = _secrets_dir(pack_root) / f"{key}.json"
        if secret_path.exists() and _get_store(pack_root).has_secret(key):
            return True
    for item in provider_named_api_keys(provider_id, pack_root=pack_root):
        if item.get("configured"):
            return True
    return False


def set_provider_api_key(
    provider_id: str,
    value: str,
    *,
    pack_root: Path | None = None,
    api_id: str | None = None,
    name: str | None = None,
    base_url: str | None = None,
    allowed_models: Any = None,
    default_model: str | None = None,
    notes: str | None = None,
    quota_label: str | None = None,
    monthly_budget_usd: float | None = None,
    monthly_request_limit: int | None = None,
    kind: str | None = None,
    credential_mode: str | None = None,
) -> dict[str, Any]:
    normalized_provider = str(provider_id or "").strip()
    # Program providers without a legacy ENV variable still need a durable
    # default connection.  Store it as an explicit named connection rather
    # than rejecting the provider as "unsupported".
    program_provider = normalized_provider in provider_program_manifests()
    named = bool(api_id or name) or (program_provider and not provider_secret_key(normalized_provider))
    key = (
        named_provider_secret_key(normalized_provider, api_id=api_id or "DEFAULT", name=name)
        if named
        else provider_secret_key(normalized_provider)
    )
    if not key:
        return {"success": False, "provider_id": provider_id, "error": "unsupported provider"}
    is_builtin = normalized_provider in PROVIDER_SECRET_KEYS or program_provider
    resolved_kind = _normalize_kind(kind) if kind is not None else None
    if resolved_kind is None:
        # Reuse the previously stored kind if any, otherwise default by provider type.
        existing_metadata = _read_api_metadata(pack_root).get(key, {})
        if isinstance(existing_metadata, dict) and existing_metadata.get("kind"):
            resolved_kind = _normalize_kind(existing_metadata.get("kind"))
        else:
            resolved_kind = _KIND_LLM if is_builtin else _KIND_LLM
    if not is_builtin and named and normalized_provider:
        # Auto-register the custom provider so it shows up in the UI dropdown.
        register_custom_provider(
            normalized_provider,
            label=normalized_provider,
            kind=resolved_kind,
            pack_root=pack_root,
        )
    normalized_api_id = str(api_id or _api_id_from_named_key(key, provider_id)).strip()
    display_name = str(name or normalized_api_id or provider_id).strip()

    cleaned = str(value or "").strip()
    resolved_credential_mode = _normalize_credential_mode(credential_mode)
    if resolved_credential_mode == _CREDENTIAL_MODE_NONE:
        if not named:
            return {"success": False, "provider_id": provider_id, "error": "an unauthenticated connection needs a named API entry"}
        if cleaned:
            return {"success": False, "provider_id": provider_id, "error": "an unauthenticated connection cannot include an API key"}
        if not _is_loopback_endpoint(base_url):
            return {"success": False, "provider_id": provider_id, "error": "an unauthenticated connection must use a loopback base URL"}
        store = _get_store(pack_root)
        # First-time endpoint connections have nothing to delete.  Clearing a
        # prior key is still required when converting an existing entry.
        if store.has_secret(key):
            deleted = store.delete_secret(
                key,
                actor="defaultspack",
                reason=f"save unauthenticated {provider_id} connection",
            )
            if not deleted.success:
                return {"success": False, "provider_id": provider_id, "error": deleted.error}
        metadata = _read_api_metadata(pack_root)
        metadata[key] = _metadata_patch(
            provider_id=provider_id,
            api_id=normalized_api_id,
            name=display_name,
            existing=metadata.get(key, {}),
            base_url=base_url,
            allowed_models=allowed_models,
            default_model=default_model,
            notes=notes,
            quota_label=quota_label,
            monthly_budget_usd=monthly_budget_usd,
            monthly_request_limit=monthly_request_limit,
            kind=resolved_kind,
            credential_mode=resolved_credential_mode,
        )
        _write_api_metadata(metadata, pack_root)
        _refresh_provider_env(provider_id, pack_root=pack_root)
        return {
            "success": True,
            "provider_id": provider_id,
            "api_id": normalized_api_id,
            "name": display_name,
            "key": key,
            "configured": True,
            "created": False,
            "kind": resolved_kind,
            "credential_mode": resolved_credential_mode,
            "base_url": str(base_url or "").strip(),
            "allowed_models": _normalize_allowed_models(allowed_models),
            "default_model": str(default_model or "").strip(),
            "notes": str(notes or "").strip(),
            "quota_label": str(quota_label or "").strip(),
            "monthly_budget_usd": float(monthly_budget_usd) if monthly_budget_usd is not None and float(monthly_budget_usd) > 0 else None,
            "monthly_request_limit": int(monthly_request_limit) if monthly_request_limit is not None and int(monthly_request_limit) > 0 else None,
            "error": None,
        }
    if not cleaned:
        result = _get_store(pack_root).delete_secret(
            key,
            actor="defaultspack",
            reason=f"clear {provider_id} api key",
        )
        if result.success:
            if named:
                metadata = _read_api_metadata(pack_root)
                metadata.pop(key, None)
                _write_api_metadata(metadata, pack_root)
                _refresh_provider_env(provider_id, pack_root=pack_root)
            _reset_ai_client()
        return {
            "success": bool(result.success),
            "provider_id": provider_id,
            "api_id": normalized_api_id,
            "name": display_name,
            "key": key,
            "configured": False,
            "cleared": True,
            "error": result.error,
        }

    result = _get_store(pack_root).set_secret(
        key,
        cleaned,
        actor="defaultspack",
        reason=f"set {provider_id} api key",
    )
    if result.success:
        if named:
            metadata = _read_api_metadata(pack_root)
            metadata[key] = _metadata_patch(
                provider_id=provider_id,
                api_id=normalized_api_id,
                name=display_name,
                existing=metadata.get(key, {}),
                base_url=base_url,
                allowed_models=allowed_models,
                default_model=default_model,
                notes=notes,
                quota_label=quota_label,
                monthly_budget_usd=monthly_budget_usd,
                monthly_request_limit=monthly_request_limit,
                kind=resolved_kind,
                credential_mode=resolved_credential_mode,
            )
            _write_api_metadata(metadata, pack_root)
        _reset_ai_client()
    return {
        "success": bool(result.success),
        "provider_id": provider_id,
        "api_id": normalized_api_id,
        "name": display_name,
        "key": key,
        "configured": bool(result.success),
        "created": bool(result.created),
        "kind": resolved_kind,
        "credential_mode": resolved_credential_mode,
        "base_url": str(base_url or "").strip(),
        "allowed_models": _normalize_allowed_models(allowed_models),
        "default_model": str(default_model or "").strip(),
        "notes": str(notes or "").strip(),
        "quota_label": str(quota_label or "").strip(),
        "monthly_budget_usd": float(monthly_budget_usd) if monthly_budget_usd is not None and float(monthly_budget_usd) > 0 else None,
        "monthly_request_limit": int(monthly_request_limit) if monthly_request_limit is not None and int(monthly_request_limit) > 0 else None,
        "error": result.error,
    }


def delete_provider_api_key(
    provider_id: str,
    api_id: str,
    *,
    pack_root: Path | None = None,
) -> dict[str, Any]:
    api_id = str(api_id or "").strip()
    if not api_id:
        return {"success": False, "provider_id": provider_id, "error": "api_id is required"}
    return set_provider_api_key(provider_id, "", pack_root=pack_root, api_id=api_id)


def rename_provider_api_key(
    provider_id: str,
    api_id: str,
    name: str,
    *,
    pack_root: Path | None = None,
    new_api_id: str | None = None,
    base_url: str | None = None,
    allowed_models: Any = None,
    default_model: str | None = None,
    notes: str | None = None,
    quota_label: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    api_id = str(api_id or "").strip()
    display_name = str(name or new_api_id or api_id).strip()
    target_api_id = str(new_api_id or name or api_id).strip()
    if not provider_id or not api_id or not target_api_id:
        return {"success": False, "provider_id": provider_id, "api_id": api_id, "error": "provider_id and api_id are required"}

    old_key = named_provider_secret_key(provider_id, api_id=api_id)
    new_key = named_provider_secret_key(provider_id, api_id=target_api_id)
    metadata = _read_api_metadata(pack_root)
    if old_key == new_key:
        metadata[old_key] = _metadata_patch(
            provider_id=provider_id,
            api_id=api_id,
            name=display_name,
            existing=metadata.get(old_key, {}),
            base_url=base_url,
            allowed_models=allowed_models,
            default_model=default_model,
            notes=notes,
            quota_label=quota_label,
            kind=kind,
        )
        _write_api_metadata(metadata, pack_root)
        return {
            "success": True,
            "provider_id": provider_id,
            "api_id": api_id,
            "name": display_name,
            "key": old_key,
            "configured": True,
            "renamed": True,
        }

    if _get_store(pack_root).has_secret(new_key):
        return {"success": False, "provider_id": provider_id, "api_id": api_id, "error": "target api name already exists"}

    value = _read_secret_value(
        old_key,
        f"defaultspack.ai_client:{provider_id}:{api_id}:rename",
        pack_root=pack_root,
    )
    if not value:
        return {"success": False, "provider_id": provider_id, "api_id": api_id, "error": "api key not found"}

    saved = set_provider_api_key(
        provider_id,
        value,
        pack_root=pack_root,
        api_id=target_api_id,
        name=display_name,
        base_url=base_url,
        allowed_models=allowed_models,
        default_model=default_model,
        notes=notes,
        quota_label=quota_label,
        kind=kind,
    )
    if not saved.get("success"):
        return saved

    deleted = _get_store(pack_root).delete_secret(
        old_key,
        actor="defaultspack",
        reason=f"rename {provider_id} api key",
    )
    if deleted.success:
        metadata = _read_api_metadata(pack_root)
        metadata.pop(old_key, None)
        metadata[new_key] = _metadata_patch(
            provider_id=provider_id,
            api_id=str(saved.get("api_id") or target_api_id),
            name=display_name,
            existing=metadata.get(new_key, {}),
            base_url=base_url,
            allowed_models=allowed_models,
            default_model=default_model,
            notes=notes,
            quota_label=quota_label,
            kind=kind,
        )
        _write_api_metadata(metadata, pack_root)
        _refresh_provider_env(provider_id, pack_root=pack_root)
    return {
        "success": bool(deleted.success),
        "provider_id": provider_id,
        "api_id": str(saved.get("api_id") or target_api_id),
        "name": display_name,
        "key": new_key,
        "configured": bool(deleted.success),
        "renamed": bool(deleted.success),
        "error": deleted.error,
    }


def load_provider_api_keys_into_env(*, pack_root: Path | None = None) -> dict[str, bool]:
    """Return provider configuration status without mutating process globals.

    The historic function name remains for API compatibility.  It is now a
    pure status query; host/provider contracts carry material explicitly.
    """

    return {
        provider_id: provider_has_api_key(provider_id, pack_root=pack_root)
        for provider_id in PROVIDER_SECRET_KEYS
    }


def provider_named_api_keys(provider_id: str = "", *, pack_root: Path | None = None) -> list[dict[str, Any]]:
    requested_provider = str(provider_id or "").strip()
    if not _secrets_dir(pack_root).exists():
        return []
    store = _get_store(pack_root)
    metadata = _read_api_metadata(pack_root)
    items: list[dict[str, Any]] = []
    store_metadata = {str(meta.key or ""): meta for meta in store.list_keys()}
    for key in sorted(set(store_metadata) | set(metadata)):
        meta = store_metadata.get(key)
        stored_meta = metadata.get(key, {})
        # A no-auth endpoint deliberately has no secret-store record.  Keep its
        # metadata-visible connection even if an older secret was deleted.
        if not key.startswith(f"{_NAMED_API_PREFIX}_") or (
            meta is not None
            and meta.deleted
            and _normalize_credential_mode(stored_meta.get("credential_mode")) != _CREDENTIAL_MODE_NONE
        ):
            continue
        key_provider = str(stored_meta.get("provider_id") or _provider_from_named_key(key)).strip()
        if requested_provider and key_provider != requested_provider:
            continue
        api_id = str(stored_meta.get("api_id") or _api_id_from_named_key(key, key_provider)).strip()
        display_name = str(stored_meta.get("name") or api_id.replace("_", " ").title())
        credential_mode = _normalize_credential_mode(stored_meta.get("credential_mode"))
        no_auth_connection = credential_mode == _CREDENTIAL_MODE_NONE and bool(str(stored_meta.get("base_url") or "").strip())
        item = {
            "api_id": api_id,
            "name": display_name,
            "provider_id": key_provider,
            "key": key,
            "label": f"{key_provider}:{api_id}:***",
            "configured": bool(meta and meta.exists) or no_auth_connection,
            "created_at": getattr(meta, "created_at", ""),
            "updated_at": getattr(meta, "updated_at", ""),
            "kind": _normalize_kind(stored_meta.get("kind")),
            "base_url": str(stored_meta.get("base_url") or ""),
            "allowed_models": _normalize_allowed_models(stored_meta.get("allowed_models", [])),
            "default_model": str(stored_meta.get("default_model") or ""),
            "notes": str(stored_meta.get("notes") or ""),
            "quota_label": str(stored_meta.get("quota_label") or ""),
            "credential_mode": credential_mode,
        }
        items.append(item)
    return sorted(items, key=lambda item: (str(item.get("provider_id")), str(item.get("api_id"))))


def provider_api_metadata(provider_id: str, api_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    provider_id = str(provider_id or "").strip()
    api_id = str(api_id or "").strip()
    if not provider_id or not api_id:
        return {}
    key = named_provider_secret_key(provider_id, api_id=api_id)
    metadata = _read_api_metadata(pack_root).get(key, {})
    if not isinstance(metadata, dict):
        return {}
    result = dict(metadata)
    result["allowed_models"] = _normalize_allowed_models(result.get("allowed_models", []))
    return result


def read_provider_api_key(provider_id: str, api_id: str, *, pack_root: Path | None = None) -> str | None:
    key = named_provider_secret_key(provider_id, api_id=api_id)
    value = _get_store(pack_root)._internal_read_value(
        key,
        caller_id=f"defaultspack.ai_client:{provider_id}:{api_id}",
    )
    if value:
        return value
    for legacy_key in provider_secret_keys(provider_id):
        value = _get_store(pack_root)._internal_read_value(
            legacy_key,
            caller_id=f"defaultspack.ai_client:{provider_id}:legacy",
        )
        if value:
            return value
    return None


def provider_key_status(*, pack_root: Path | None = None) -> list[dict[str, Any]]:
    program_manifests = provider_program_manifests()
    builtin_provider_ids = sorted(set(PROVIDER_SECRET_KEYS) | set(program_manifests))
    builtin_rows = [
        {
            "provider_id": provider_id,
            "key": keys[0] if keys else named_provider_secret_key(provider_id, api_id="DEFAULT"),
            "keys": list(keys),
            "kind": _KIND_LLM,
            "builtin": True,
            "label": str(program_manifests.get(provider_id, {}).get("display_name") or provider_id),
            "configured": (
                provider_has_api_key(provider_id, pack_root=pack_root)
                or provider_has_oauth_connection(provider_id, pack_root=pack_root)
            ),
            "apis": provider_named_api_keys(provider_id, pack_root=pack_root),
            "oauth": provider_oauth_status(provider_id, pack_root=pack_root),
        }
        for provider_id in builtin_provider_ids
        for keys in [PROVIDER_SECRET_KEYS.get(provider_id, [])]
    ]

    seen_ids = {row["provider_id"] for row in builtin_rows}
    custom_definitions = _read_custom_providers(pack_root)
    # Surface providers that appear via stored named keys even if no custom registration exists.
    discovered_provider_ids: set[str] = set()
    for api in provider_named_api_keys("", pack_root=pack_root):
        provider_id = str(api.get("provider_id") or "").strip()
        if provider_id and provider_id not in seen_ids:
            discovered_provider_ids.add(provider_id)

    custom_provider_ids = sorted(set(custom_definitions.keys()) | discovered_provider_ids)
    custom_rows: list[dict[str, Any]] = []
    for provider_id in custom_provider_ids:
        if provider_id in seen_ids:
            continue
        definition = custom_definitions.get(provider_id) or {}
        kind_value = _normalize_kind(definition.get("kind"))
        # Prefer the kind from the most recent named key if available.
        for api in provider_named_api_keys(provider_id, pack_root=pack_root):
            api_kind = _normalize_kind(api.get("kind"))
            if api_kind:
                kind_value = api_kind
                break
        apis = provider_named_api_keys(provider_id, pack_root=pack_root)
        custom_rows.append(
            {
                "provider_id": provider_id,
                "key": named_provider_secret_key(provider_id, api_id="DEFAULT"),
                "keys": [],
                "kind": kind_value,
                "builtin": False,
                "label": str(definition.get("label") or provider_id),
                "configured": any(api.get("configured") for api in apis),
                "apis": apis,
                "oauth": provider_oauth_status(provider_id, pack_root=pack_root),
            }
        )
    return builtin_rows + custom_rows


def builtin_provider_ids() -> list[str]:
    return sorted(set(PROVIDER_SECRET_KEYS) | set(provider_program_manifests()))
