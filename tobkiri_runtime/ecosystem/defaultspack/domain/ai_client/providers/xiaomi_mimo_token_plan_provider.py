from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from .component_metadata import model_manifests_from_provider_components
from .openai_compatible_provider import OpenAICompatibleProvider


_TOKEN_PLAN_MODELS: List[Dict[str, Any]] = [
    {
        "model_id": "mimo-v2.5-pro",
        "name": "MiMo V2.5 Pro",
        "display_name": "MiMo V2.5 Pro",
        "type": "reasoning",
        "defaults": {"chat": True, "coding": True, "agent": True, "reasoning": True},
        "capabilities": {
            "chat": True,
            "streaming": True,
            "reasoning": True,
            "tool_calls": True,
            "vision": False,
        },
        "supports_thinking": True,
        "thinking_levels": ["low", "medium", "high", "xhigh"],
        "default_thinking_level": "medium",
        "context_window": 1048576,
        "max_context": 1048576,
        "max_context_tokens": 1048576,
        "metadata": {
            "token_plan": True,
            "thinking_format": "deepseek",
            "tool_call_type": "openai",
            "reasoning_content_roundtrip_required_with_tool_calls": True,
            "request_defaults": {"top_p": 0.95},
        },
    },
    {
        "model_id": "mimo-v2.5",
        "name": "MiMo V2.5",
        "display_name": "MiMo V2.5",
        "type": "reasoning",
        "defaults": {"chat": True, "reasoning": True, "fast": True},
        "capabilities": {
            "chat": True,
            "streaming": True,
            "reasoning": True,
            "tool_calls": True,
            "vision": True,
        },
        "supports_thinking": True,
        "thinking_levels": ["low", "medium", "high", "xhigh"],
        "default_thinking_level": "medium",
        "metadata": {
            "token_plan": True,
            "thinking_format": "deepseek",
            "tool_call_type": "openai",
            "reasoning_content_roundtrip_required_with_tool_calls": True,
            "native_multimodal": True,
            "vision_verified": True,
            "request_defaults": {"top_p": 0.95},
        },
    },
    {
        "model_id": "mimo-v2-pro",
        "name": "MiMo V2 Pro",
        "display_name": "MiMo V2 Pro",
        "type": "reasoning",
        "defaults": {"reasoning": True},
        "capabilities": {
            "chat": True,
            "streaming": True,
            "reasoning": True,
            "tool_calls": True,
            "vision": False,
        },
        "supports_thinking": True,
        "thinking_levels": ["low", "medium", "high", "xhigh"],
        "default_thinking_level": "medium",
        "metadata": {
            "token_plan": True,
            "thinking_format": "deepseek",
            "tool_call_type": "openai",
            "reasoning_content_roundtrip_required_with_tool_calls": True,
            "request_defaults": {"top_p": 0.95},
        },
    },
    {
        "model_id": "mimo-v2-omni",
        "name": "MiMo V2 Omni",
        "display_name": "MiMo V2 Omni",
        "type": "vision",
        "defaults": {"vision": True, "agent": True},
        "capabilities": {
            "chat": True,
            "streaming": True,
            "reasoning": True,
            "tool_calls": True,
            "vision": True,
        },
        "supports_thinking": True,
        "thinking_levels": ["low", "medium", "high", "xhigh"],
        "default_thinking_level": "medium",
        "context_window": 1048576,
        "max_context": 1048576,
        "max_context_tokens": 1048576,
        "metadata": {
            "token_plan": True,
            "thinking_format": "deepseek",
            "tool_call_type": "openai",
            "reasoning_content_roundtrip_required_with_tool_calls": True,
            "native_multimodal": True,
            "vision_verified": True,
            "request_defaults": {"top_p": 0.95},
        },
    },
    {
        "model_id": "mimo-v2-omni",
        "name": "MiMo V2 Omni",
        "display_name": "MiMo V2 Omni",
        "type": "chat",
        "defaults": {"chat": True, "vision": True},
        "capabilities": {
            "chat": True,
            "streaming": True,
            "reasoning": True,
            "tool_calls": True,
            "vision": True,
        },
        "supports_thinking": True,
        "thinking_levels": ["low", "medium", "high", "xhigh"],
        "default_thinking_level": "medium",
        "metadata": {
            "token_plan": True,
            "thinking_format": "deepseek",
            "tool_call_type": "openai",
            "vision_verified": True,
            "request_defaults": {"top_p": 0.95},
        },
    },
]


class XiaomiMimoTokenPlanProvider(OpenAICompatibleProvider):
    """Xiaomi MiMo Token Plan OpenAI-compatible runtime provider."""

    MODEL_IDS = {str(model["model_id"]) for model in _TOKEN_PLAN_MODELS}

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        api_key: str = "",
        api_key_env: list[str],
        base_url_env: str,
        default_base_url: str,
        region: str,
    ) -> None:
        catalog_models = model_manifests_from_provider_components(provider_id)
        injected_api_key = str(api_key or "").strip()
        explicit_token_plan_opt_in = bool(injected_api_key)
        if not catalog_models and explicit_token_plan_opt_in:
            # This provider owns a fixed token-plan allowlist. A selected
            # model catalog may refine it, but an unselected external catalog
            # must not erase the provider's usable plan models once the user
            # has explicitly configured that token-plan connection.
            catalog_models = deepcopy(_TOKEN_PLAN_MODELS)
        for model in catalog_models:
            routing = model.get("routing") if isinstance(model.get("routing"), dict) else {}
            default_for = routing.get("default_for", [])
            if isinstance(default_for, list) and "reasoning" in default_for:
                model["type"] = "reasoning"
        super().__init__(
            provider_id=provider_id,
            display_name=display_name,
            api_key=injected_api_key,
            api_key_env=api_key_env,
            base_url_env=base_url_env,
            default_base_url=default_base_url,
            credential_required=True,
            known_models=catalog_models,
            remote_model_discovery=True,
        )
        # The generic base class supports legacy environment fallback for
        # other providers.  Xiaomi token-plan availability is caller-owned:
        # keep only the explicitly injected credential on this provider.
        self._api_key = injected_api_key
        # Keep the generic provider-program scanner's KNOWN_MODELS contract
        # empty. Token-plan models are a credential-scoped provider contract,
        # not a generic checked-in program inventory.
        self._token_plan_models = self._normalize_known_models(catalog_models)
        self.KNOWN_MODELS = []
        self.region = region

    def _headers(self, content_type="application/json"):
        headers = {
            "User-Agent": "RumiAI/1.0",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["api-key"] = self._api_key
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _assert_supported_model(self, model: str) -> None:
        model_ref = str(model or "").strip()
        prefix = f"{self.provider_id}/"
        model_id = model_ref[len(prefix):] if model_ref.startswith(prefix) else model_ref
        allowed = {
            str(item.get("model_id") or "").strip()
            for item in self._token_plan_models
            if isinstance(item, dict)
        }
        # A profile may intentionally omit the optional model-catalog pack.
        # In that case the provider cannot make a catalog-backed support claim;
        # preserve the provider's remote-discovery contract instead of turning
        # catalog absence into a false unsupported-model rejection.
        if not allowed:
            return
        if model_id not in allowed:
            raise RuntimeError(
                f"unsupported model for {self.provider_id}: {model}; "
                f"allowed models: {', '.join(sorted(allowed))}"
            )

    def _translate_model_params(self, model, params):
        translated = dict(params or {})
        extra_body = dict(
            translated.get("extra_body") if isinstance(translated.get("extra_body"), dict) else {}
        )
        model_entry = self._token_plan_model_entry(model)
        supports_thinking = bool(model_entry.get("supports_thinking"))

        raw_level = (
            str(translated.pop("thinking_level", "") or translated.pop("reasoning_effort", ""))
            .strip()
            .lower()
        )
        if not isinstance(extra_body.get("thinking"), dict):
            if raw_level == "none":
                extra_body["thinking"] = {"type": "disabled"}
            elif supports_thinking:
                # Xiaomi MiMo uses a DeepSeek-style thinking toggle instead of
                # OpenAI reasoning_effort. Keep the prompt simple and let the
                # provider use its own default internal depth when enabled.
                extra_body["thinking"] = {"type": "enabled"}
        translated.pop("reasoning_effort", None)
        if supports_thinking and extra_body:
            translated["extra_body"] = extra_body
        elif extra_body:
            extra_body.pop("thinking", None)
            if extra_body:
                translated["extra_body"] = extra_body
        return translated

    def _token_plan_model_entry(self, model: str) -> Dict[str, Any]:
        """Return the credential-scoped metadata for one token-plan model."""
        model_ref = str(model or "").strip()
        prefix = f"{self.provider_id}/"
        model_id = model_ref[len(prefix):] if model_ref.startswith(prefix) else model_ref
        qualified = f"{self.provider_id}/{model_id}" if model_id else model_ref
        for item in self._token_plan_models:
            if not isinstance(item, dict):
                continue
            if model_ref in {
                str(item.get("id") or "").strip(),
                str(item.get("model_id") or "").strip(),
            } or qualified == str(item.get("id") or "").strip():
                return item
        return {}

    def list_models(self) -> List[Dict[str, Any]]:
        models = self._merge_remote_models(self._token_plan_models)
        for model in models:
            metadata = dict(model.get("metadata") or {})
            metadata.update({"region": self.region, "token_plan_region_scoped": True})
            model["metadata"] = metadata
        return models

    def complete(self, model, messages, tools, params):
        self._assert_supported_model(model)
        return super().complete(model, messages, tools, params)

    def stream(self, model, messages, tools, params):
        self._assert_supported_model(model)
        return super().stream(model, messages, tools, params)


class XiaomiMimoTokenPlanAmsProvider(XiaomiMimoTokenPlanProvider):
    def __init__(self, *, api_key: str = "") -> None:
        super().__init__(
            provider_id="xiaomi-token-plan-ams",
            display_name="Xiaomi MiMo Token Plan AMS",
            api_key=api_key,
            api_key_env=[
                "XIAOMI_MIMO_TOKEN_PLAN_AMS_API_KEY",
            ],
            base_url_env="XIAOMI_MIMO_TOKEN_PLAN_AMS_BASE_URL",
            default_base_url="https://token-plan-ams.xiaomimimo.com/v1",
            region="ams",
        )


class XiaomiMimoTokenPlanCnProvider(XiaomiMimoTokenPlanProvider):
    def __init__(self, *, api_key: str = "") -> None:
        super().__init__(
            provider_id="xiaomi-token-plan-cn",
            display_name="Xiaomi MiMo Token Plan CN",
            api_key=api_key,
            api_key_env=[
                "XIAOMI_MIMO_TOKEN_PLAN_CN_API_KEY",
            ],
            base_url_env="XIAOMI_MIMO_TOKEN_PLAN_CN_BASE_URL",
            default_base_url="https://token-plan-cn.xiaomimimo.com/v1",
            region="cn",
        )


class XiaomiMimoTokenPlanSgpProvider(XiaomiMimoTokenPlanProvider):
    def __init__(self, *, api_key: str = "") -> None:
        super().__init__(
            provider_id="xiaomi-token-plan-sgp",
            display_name="Xiaomi MiMo Token Plan SGP",
            api_key=api_key,
            api_key_env=[
                "XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY",
                "XIAOMI_MIMO_TOKEN_PLAN_API_KEY",
                "MIMO_API_KEY",
            ],
            base_url_env="XIAOMI_MIMO_TOKEN_PLAN_SGP_BASE_URL",
            default_base_url="https://token-plan-sgp.xiaomimimo.com/v1",
            region="sgp",
        )
