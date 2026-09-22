from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Protocol

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.ai_client.model_pack_router import select_model_pack
from domain.ai_client.model_pack import ModelPack
from domain.ai_client.model_pack_store import ModelPackStore
from domain.ai_client.api_key_store import (
    provider_api_metadata,
    provider_has_api_key,
    provider_named_api_keys,
    read_provider_api_key,
)
from domain.ai_client.authority_resource import (
    build_provider_authority_resource,
    provider_authority_reason,
)
from domain.ai_client.authority_gate import provider_requires_authority
from domain.ai_client.capabilities.registry import get_model_provider_capabilities
from domain.ai_client.model_metadata_schema import (
    context_window_value,
    normalize_capability_map,
    normalize_routing_defaults,
)
from domain.ai_client import rumi_process
from domain.ai_client.rumi_process_runner import RumiProcessRunner
from domain.ai_client.oauth_store import provider_has_oauth_connection
from domain.ai_client.providers import (
    _cloud_runtime_enabled,
    build_profile_catalog,
    detect_available_providers,
    detect_rumi_provider,
    get_all_known_models,
    get_provider_catalog,
    get_provider_catalog_map,
)

_HIDDEN_RUNTIME_LIST_PROVIDER_IDS = {"human-operator", "rumi"}


class _RemovedAuthorityService(Protocol):
    """Type-only shape for the deleted, fail-closed compatibility boundary."""

    def one_shot_approval_issued(self, **kwargs: object) -> bool: ...

    def check(self, **kwargs: object) -> object: ...

    def consume_one_shot_approvals_atomically(self, items: list[dict[str, object]]) -> object: ...


def _removed_authority_boundary() -> _RemovedAuthorityService:
    """Preserve the deleted authority service's fail-closed runtime boundary."""
    from core_runtime.legacy_runtime_removed import removed_authority_service

    removed_authority_service()
    raise RuntimeError("legacy authority workflow is unavailable")


class AuthorityApprovalRequired(RuntimeError):
    def __init__(self, decision):
        self.decision = decision
        super().__init__(getattr(decision, "reason", "") or "Authority approval required")


class DirectProviderInvocationDenied(PermissionError):
    """Raised when code attempts to bypass the captured Pack v4 provider path."""


class AIClient:
    """AI Client - provider routing with profile and catalog compatibility."""

    _instance: AIClient | None = None
    _initialized: bool

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._providers = {}
        self._profiles = {}
        self._register_default_provider()
        self._auto_register_providers()
        self._auto_register_rumi()

    def _register_default_provider(self):
        from domain.ai_client.providers.stub_provider import StubProvider

        self._providers["stub"] = StubProvider()

    def _auto_register_providers(self):
        """環境変数が設定されているプロバイダーを自動登録する。"""
        try:
            available = detect_available_providers()
            provider_catalog = get_provider_catalog_map()
            cloud_enabled = _cloud_runtime_enabled()
            local_default_enabled = self._local_default_runtime_enabled()
            for name, instance in available.items():
                entry = provider_catalog.get(name, {})
                availability = (
                    entry.get("availability", {})
                    if isinstance(entry.get("availability"), dict)
                    else {}
                )
                if (
                    availability.get("configuration_source")
                    in {"default_local_endpoint", "builtin_local_provider"}
                    and not local_default_enabled
                ):
                    continue
                if not cloud_enabled and entry.get("kind") not in {"builtin", "local"}:
                    if not provider_has_api_key(name) and not provider_has_oauth_connection(name):
                        continue
                self._providers[name] = instance
        except Exception:
            pass

    @staticmethod
    def _local_default_runtime_enabled():
        value = (
            str(os.environ.get("RUMI_DEFAULTSPACK_ENABLE_LOCAL_PROVIDERS", "") or "")
            .strip()
            .lower()
        )
        return value in {"1", "true", "yes", "on"}

    def _auto_register_rumi(self):
        """rumi プロバイダーを自動登録する（他のプロバイダーが1つ以上ある場合のみ）。"""
        try:
            rumi = detect_rumi_provider(self)
            if rumi is not None:
                self._providers["rumi"] = rumi
        except Exception:
            pass

    def register_provider(self, name, provider):
        """プロバイダーを動的に登録する。"""
        self._providers[name] = provider

    def register_profile(self, name, profile=None, provider="", model="", **kwargs):
        """互換的にプロファイルを登録する。"""
        if isinstance(profile, dict):
            payload = dict(profile)
        else:
            payload = dict(kwargs)
            if profile is not None and not provider:
                provider = str(profile)
            if provider:
                payload["provider"] = provider
            if model:
                payload["model"] = model
        self._profiles[name] = payload

    def _active_provider_ids(self):
        return {
            provider_id
            for provider_id in self._providers.keys()
            if provider_id not in _HIDDEN_RUNTIME_LIST_PROVIDER_IDS
        }

    def _provider_model_candidates(self, provider_name):
        provider = self._providers.get(provider_name)
        if provider is None:
            return []
        listed: list[object] = []
        if callable(getattr(provider, "list_models", None)):
            try:
                listed = provider.list_models() or []
            except Exception:
                listed = []
        if not listed and hasattr(provider, "KNOWN_MODELS"):
            listed = getattr(provider, "KNOWN_MODELS", []) or []
        return listed

    @staticmethod
    def _normalize_runtime_model(provider_id, provider_entry, raw):
        if isinstance(raw, str):
            model_id = raw.split("/", 1)[1] if "/" in raw else raw
            qualified_model_id = raw if "/" in raw else f"{provider_id}/{model_id}"
            display_name = model_id
            model_type = "chat"
            defaults = {}
            metadata: dict[str, object] = {}
            capabilities = []
            context_window = 0
            max_context = 0
            supports_thinking = False
            thinking_levels = []
        elif isinstance(raw, dict):
            qualified_model_id = str(raw.get("id", "")).strip()
            model_id = str(raw.get("model_id", "")).strip()
            if qualified_model_id and "/" in qualified_model_id and not model_id:
                _, model_id = qualified_model_id.split("/", 1)
            if not model_id:
                model_id = str(raw.get("model_name") or raw.get("name") or "").strip()
            if not model_id:
                return None
            if not qualified_model_id:
                qualified_model_id = f"{provider_id}/{model_id}"
            display_name = str(raw.get("display_name") or raw.get("name") or model_id)
            model_type = str(raw.get("type", "chat"))
            defaults = normalize_routing_defaults(raw)
            routing = dict(raw.get("routing", {})) if isinstance(raw.get("routing"), dict) else {}
            metadata = dict(raw.get("metadata", {}))
            raw_capabilities = raw.get("capabilities", [])
            capability_map = normalize_capability_map(raw_capabilities)
            capability_map.setdefault(
                "chat", bool(capability_map.get("text_input") or capability_map.get("text_output"))
            )
            capability_map.setdefault("vision", bool(capability_map.get("image_input")))
            capability_map.setdefault("reasoning", bool(capability_map.get("thinking")))
            capability_map.setdefault("tool_calls", bool(capability_map.get("tool_calling")))
            capabilities = [str(key) for key, value in capability_map.items() if value]
            if capability_map and "capabilities" not in metadata:
                metadata["capabilities"] = capability_map
            context_window = context_window_value(raw, default=0)
            max_context = context_window
            thinking_value = raw.get("thinking")
            thinking: dict[str, object] = (
                {str(key): value for key, value in thinking_value.items()}
                if isinstance(thinking_value, dict)
                else {}
            )
            supports_thinking = bool(
                raw.get("supports_thinking")
                or capability_map.get("thinking")
                or thinking.get("supported")
                or metadata.get("supports_thinking")
            )
            levels_value = thinking.get("levels")
            if not isinstance(levels_value, list):
                levels_value = raw.get("thinking_levels")
            if not isinstance(levels_value, list):
                levels_value = metadata.get("thinking_levels")
            thinking_levels = list(levels_value) if isinstance(levels_value, list) else []
            if supports_thinking and not thinking_levels:
                thinking_levels = ["low", "medium", "high", "xhigh"]
        else:
            return None

        normalized = {
            "id": qualified_model_id,
            "qualified_model_id": qualified_model_id,
            "provider": provider_id,
            "provider_id": provider_id,
            "provider_display_name": provider_entry.get("display_name", provider_id),
            "model_id": model_id,
            "model_name": model_id,
            "name": display_name,
            "display_name": display_name,
            "type": model_type,
            "context_window": context_window,
            "max_context": max_context,
            "max_context_tokens": max_context,
            "supports_thinking": supports_thinking,
            "thinking_levels": thinking_levels,
            "default_thinking_level": (
                raw.get(
                    "default_thinking_level",
                    thinking.get(
                        "default_level",
                        metadata.get(
                            "default_thinking_level", "medium" if supports_thinking else None
                        ),
                    ),
                )
                if isinstance(raw, dict)
                else None
            ),
            "capabilities": capabilities,
            "routing": routing if isinstance(raw, dict) else {},
            "thinking": thinking if isinstance(raw, dict) else {},
            "availability": dict(provider_entry.get("availability", {})),
            "supports_invoke": bool(
                provider_entry.get("availability", {}).get("supports_invoke", False)
            ),
            "defaults": defaults,
            "metadata": metadata,
        }
        provider_capabilities = get_model_provider_capabilities(
            qualified_model_id,
            {
                **normalized,
                "capabilities": capabilities,
                "metadata": metadata,
                "context_window": context_window,
                "max_context": max_context,
                "supports_thinking": supports_thinking,
                "thinking_levels": thinking_levels,
                "routing": normalized.get("routing", {}),
                "thinking": normalized.get("thinking", {}),
            },
        )
        normalized["provider_capabilities"] = provider_capabilities
        normalized["metadata"].update(
            {
                "provider_model_key": qualified_model_id,
                "provider_display_name": provider_entry.get("display_name", provider_id),
                "provider_kind": provider_entry.get("kind", ""),
                "availability_status": provider_entry.get("availability", {}).get("status"),
                "max_context": max_context,
                "supports_thinking": supports_thinking,
                "thinking_levels": thinking_levels,
                "routing": normalized.get("routing", {}),
                "thinking": normalized.get("thinking", {}),
                "provider_capabilities": provider_capabilities,
            }
        )
        return normalized

    def _runtime_model_matches(self, model_ref):
        active_provider_ids = self._active_provider_ids()
        catalog_map = get_provider_catalog_map(active_provider_ids=active_provider_ids)
        matches = []
        seen = set()
        for provider_id in active_provider_ids:
            provider_entry = catalog_map.get(provider_id, {})
            provider_entry.setdefault("display_name", provider_id)
            provider_entry.setdefault("availability", {"active": True, "supports_invoke": True})
            for raw in self._provider_model_candidates(provider_id):
                candidate = self._normalize_runtime_model(provider_id, provider_entry, raw)
                if candidate is None:
                    continue
                candidate_key = (candidate["provider_id"], candidate["model_id"])
                if candidate_key in seen:
                    continue
                seen.add(candidate_key)
                if model_ref in {
                    candidate["qualified_model_id"],
                    candidate["id"],
                    candidate["model_id"],
                    candidate["name"],
                    candidate["display_name"],
                }:
                    matches.append(candidate)
        return matches

    def resolve_provider(self, model_str):
        """model文字列("provider/model" or "profile_name")から解決する。"""
        if "/" in model_str:
            provider_name, model_name = model_str.split("/", 1)
        else:
            profile = self._profiles.get(model_str)
            if profile:
                provider_name = profile.get("provider") or profile.get("provider_id") or "stub"
                model_name = (
                    profile.get("model")
                    or profile.get("model_id")
                    or profile.get("qualified_model_id")
                    or model_str
                )
                if isinstance(model_name, str) and "/" in model_name:
                    resolved_provider, resolved_model = model_name.split("/", 1)
                    provider_name = provider_name or resolved_provider
                    model_name = resolved_model
            else:
                matches = []
                seen = set()
                for item in self.list_models():
                    item_key = (item.get("provider_id"), item.get("model_id"))
                    if item_key in seen:
                        continue
                    if model_str in {
                        item.get("model_id"),
                        item.get("qualified_model_id"),
                        item.get("id"),
                        item.get("name"),
                        item.get("display_name"),
                        item.get("disambiguated_name"),
                    }:
                        seen.add(item_key)
                        matches.append(item)
                for item in self._runtime_model_matches(model_str):
                    item_key = (item.get("provider_id"), item.get("model_id"))
                    if item_key not in seen:
                        seen.add(item_key)
                        matches.append(item)
                if len(matches) == 1:
                    provider_name = matches[0].get("provider_id", "stub")
                    model_name = matches[0].get("model_id", model_str)
                else:
                    provider_name = "stub"
                    model_name = model_str
        provider = self._providers.get(provider_name, self._providers["stub"])
        return provider, model_name

    def _settings_path(self):
        from domain.frontend_settings_store import defaultspack_frontend_settings_path

        return defaultspack_frontend_settings_path(None)

    def _api_routes(self):
        data = self._settings_data()
        if not data:
            return {}
        models = data.get("models") if isinstance(data.get("models"), dict) else {}
        apis = data.get("apis") if isinstance(data.get("apis"), dict) else {}
        routes = {}
        for item in self._structured_api_routes(models.get("api_routes") or apis.get("api_routes")):
            model_ref = str(item.get("model") or "").strip()
            route_refs = [
                str(route).strip() for route in item.get("routes", []) if str(route).strip()
            ]
            if model_ref and route_refs:
                routes[model_ref] = route_refs
        raw_routes = models.get("model_api_routes") or apis.get("model_api_routes") or ""
        if isinstance(raw_routes, list):
            raw_routes = "\n".join(str(item) for item in raw_routes)
        for raw_line in str(raw_routes or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^(.+?):\s+(.+)$", line)
            if not match:
                continue
            model_ref = match.group(1).strip()
            route_refs = [item.strip() for item in match.group(2).split(",") if item.strip()]
            if model_ref and route_refs:
                routes.setdefault(model_ref, route_refs)
        return routes

    def _settings_data(self):
        try:
            return json.loads(self._settings_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _jsonish(value, fallback):
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return fallback
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return fallback
        return value

    def _structured_api_routes(self, value):
        parsed = self._jsonish(value, [])
        if isinstance(parsed, dict):
            raw_items = [
                {"model": key, **(route if isinstance(route, dict) else {"routes": route})}
                for key, route in parsed.items()
            ]
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = []
        routes = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            model_ref = str(item.get("model") or item.get("profile_id") or "").strip()
            raw_routes = item.get("routes", item.get("apis", item.get("api_refs", [])))
            if isinstance(raw_routes, str):
                route_refs = [part.strip() for part in raw_routes.split(",") if part.strip()]
            elif isinstance(raw_routes, list):
                route_refs = [str(part).strip() for part in raw_routes if str(part or "").strip()]
            else:
                route_refs = []
            if model_ref and route_refs:
                routes.append({"model": model_ref, "routes": route_refs})
        return routes

    def _routes_for_model(self, model):
        routes = self._api_routes()
        if model in routes:
            return routes[model]
        if isinstance(model, str) and "/" in model:
            model_id = model.split("/", 1)[1]
            return routes.get(model_id, [])
        return []

    @staticmethod
    def _route_parts(route_ref):
        cleaned = str(route_ref or "").strip()
        if "/" not in cleaned:
            return cleaned, "main"
        provider_id, api_id = cleaned.split("/", 1)
        return provider_id.strip(), api_id.strip() or "main"

    @staticmethod
    def _is_rate_limit_error(exc):
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "429",
                "rate limit",
                "rate_limit",
                "quota",
                "resource_exhausted",
                "provider_error",
                "provider error",
                "timeout",
                "timed out",
                "temporarily",
                "503",
                "502",
                "504",
            )
        )

    def _model_for_route(self, model, provider_id):
        if isinstance(model, str) and "/" in model:
            _, model_id = model.split("/", 1)
            return f"{provider_id}/{model_id}"
        return f"{provider_id}/{model}"

    @staticmethod
    def _provider_unconfigured_message(model):
        provider_name = "stub"
        if isinstance(model, str) and "/" in model:
            provider_name = model.split("/", 1)[0] or provider_name
        return (
            f"{provider_name}: provider is not configured. "
            "Configure a real or local AI provider before sending a message."
        )

    @staticmethod
    def _authority_context_from_params(params):
        if not isinstance(params, dict):
            return {}
        value = params.get("_authority_context")
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _authority_token_for_permission(context, permission_id):
        permission_id = str(permission_id or "").strip()
        tokens = context.get("approval_tokens") if isinstance(context, dict) else None
        if isinstance(tokens, dict):
            raw = tokens.get(permission_id)
            if isinstance(raw, dict):
                request_id = str(
                    raw.get("request_id") or raw.get("approval_request_id") or ""
                ).strip()
                token = str(raw.get("approval_token") or raw.get("token") or "").strip()
                if request_id and token:
                    return request_id, token
        context_permission = (
            str(context.get("permission_id") or "").strip() if isinstance(context, dict) else ""
        )
        if context_permission and context_permission != permission_id:
            return "", ""
        request_id = (
            str(context.get("request_id") or "").strip() if isinstance(context, dict) else ""
        )
        token = (
            str(context.get("approval_token") or "").strip() if isinstance(context, dict) else ""
        )
        return request_id, token

    def _has_authority_token_for_permission(self, params, permission_id):
        context = self._authority_context_from_params(params)
        _, token = self._authority_token_for_permission(context, permission_id)
        return bool(token)

    @staticmethod
    def _strip_authority_params(params):
        clean = dict(params or {})
        clean.pop("_authority_context", None)
        clean.pop("_v4_authority_kernel_admitted", None)
        return clean

    def _authority_batch_consume_item(self, params, permission_id, decision):
        if not getattr(decision, "allowed", False):
            return None
        if getattr(decision, "reason", "") != "One-shot approval verified":
            return None
        context = self._authority_context_from_params(params)
        request_id, approval_token = self._authority_token_for_permission(context, permission_id)
        request_id = request_id or str(context.get("request_id") or "").strip()
        if not request_id or not approval_token:
            return None
        return {
            "request_id": request_id,
            "principal_id": getattr(decision, "principal_id", "")
            or str(context.get("principal_id") or "defaultspack"),
            "permission_id": permission_id,
            "resource": dict(getattr(decision, "resource", {}) or {}),
            "approval_token": approval_token,
        }

    @staticmethod
    def _provider_api_key_configured(provider_id, api_id):
        provider_id = str(provider_id or "").strip()
        api_id = str(api_id or "").strip() or "legacy"
        if not provider_id:
            return False
        if api_id not in {"", "main", "legacy"}:
            for item in provider_named_api_keys(provider_id):
                if str(item.get("api_id") or "").strip() == api_id and item.get("configured"):
                    return True
        return provider_has_api_key(provider_id)

    @staticmethod
    def _provider_requires_authority(provider_id, provider, api_id="legacy"):
        return provider_requires_authority(provider_id, provider=provider, api_id=api_id)

    def _provider_id_for_provider(self, provider, model_ref=""):
        if isinstance(model_ref, str) and "/" in model_ref:
            provider_id = model_ref.split("/", 1)[0].strip()
            if provider_id:
                return provider_id
        for provider_id, candidate in self._providers.items():
            if candidate is provider:
                return provider_id
        return ""

    def _check_authority_for_provider_api(
        self,
        *,
        permission_id,
        resource_kind,
        provider_id,
        api_id,
        model_id,
        model_ref,
        params,
        provider=None,
        stream=False,
        reason=None,
        consume_approval_token=True,
    ):
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            return

        context = self._authority_context_from_params(params)
        principal_id = str(context.get("principal_id") or "defaultspack")
        api_metadata = provider_api_metadata(provider_id, api_id or "legacy")
        resource = build_provider_authority_resource(
            permission_id=permission_id,
            resource_kind=resource_kind,
            provider_id=provider_id,
            api_id=api_id or "legacy",
            model_id=model_id,
            model_ref=model_ref,
            provider=provider,
            api_metadata=api_metadata,
            stream=stream,
        )

        service = _removed_authority_boundary()
        request_id, approval_token = self._authority_token_for_permission(context, permission_id)
        effective_request_id = request_id or str(context.get("request_id") or "").strip()
        if (
            context.get("allow_consumed_one_shot_tokens_for_run")
            and effective_request_id
            and approval_token
        ):
            issued = getattr(service, "one_shot_approval_issued", None)
            if callable(issued):
                try:
                    issued_unconsumed = bool(
                        issued(
                            request_id=effective_request_id,
                            permission_id=permission_id,
                            token=approval_token,
                            conversation_id=context.get("conversation_id"),
                            principal_id=principal_id,
                            resource=resource,
                        )
                    )
                    issued_consumed = bool(
                        issued(
                            request_id=effective_request_id,
                            permission_id=permission_id,
                            token=approval_token,
                            conversation_id=context.get("conversation_id"),
                            principal_id=principal_id,
                            resource=resource,
                            include_consumed=True,
                        )
                    )
                except TypeError:
                    issued_unconsumed = False
                    issued_consumed = False
                except Exception:
                    issued_unconsumed = False
                    issued_consumed = False
                if not issued_unconsumed and issued_consumed:
                    from core_runtime.authority.models import AuthorityDecision

                    return AuthorityDecision(
                        allowed=True,
                        permission_id=permission_id,
                        principal_id=principal_id,
                        reason="Trusted consumed one-shot approval",
                        request_id=effective_request_id,
                        resource=resource,
                    )
        decision = service.check(
            principal_id=principal_id,
            permission_id=permission_id,
            resource=resource,
            reason=reason or provider_authority_reason(permission_id, resource),
            conversation_id=context.get("conversation_id"),
            profile_id=context.get("profile_id"),
            node_id=context.get("node_id"),
            graph_id=context.get("graph_id"),
            request_id=request_id or context.get("request_id"),
            approval_token=approval_token,
            consume_approval_token=consume_approval_token,
        )
        if not getattr(decision, "allowed", False):
            raise AuthorityApprovalRequired(decision)
        return decision

    def _check_authority_for_model_api(
        self,
        *,
        provider_id,
        api_id,
        model_id,
        model_ref,
        params,
        provider=None,
        stream=False,
        consume_approval_token=True,
    ):
        return self._check_authority_for_provider_api(
            permission_id="model.invoke",
            resource_kind="model",
            provider_id=provider_id,
            api_id=api_id,
            model_id=model_id,
            model_ref=model_ref,
            params=params,
            provider=provider,
            stream=stream,
            reason=None,
            consume_approval_token=consume_approval_token,
        )

    def _check_authority_for_api_key_use(
        self,
        *,
        provider_id,
        api_id,
        model_id,
        model_ref,
        params,
        provider=None,
        stream=False,
        consume_approval_token=True,
    ):
        return self._check_authority_for_provider_api(
            permission_id="api_key.use",
            resource_kind="api_key",
            provider_id=provider_id,
            api_id=api_id,
            model_id=model_id,
            model_ref=model_ref,
            params=params,
            provider=provider,
            stream=stream,
            reason=None,
            consume_approval_token=consume_approval_token,
        )

    def _check_authority_for_network_egress(
        self,
        *,
        provider_id,
        api_id,
        model_id,
        model_ref,
        params,
        provider=None,
        stream=False,
        consume_approval_token=True,
    ):
        return self._check_authority_for_provider_api(
            permission_id="network.egress",
            resource_kind="network",
            provider_id=provider_id,
            api_id=api_id,
            model_id=model_id,
            model_ref=model_ref,
            params=params,
            provider=provider,
            stream=stream,
            reason=None,
            consume_approval_token=consume_approval_token,
        )

    def _check_authority_for_model_and_api_key_use(
        self,
        *,
        provider_id,
        api_id,
        model_id,
        model_ref,
        params,
        provider=None,
        stream=False,
    ):
        authority_context = params.get("_authority_context") if isinstance(params, dict) else None
        provider_call_key = f"{provider_id}:{api_id}:{model_id}:{bool(stream)}"
        if isinstance(authority_context, dict):
            verified_provider_calls = authority_context.get("_provider_one_shot_verified_for_run")
            if (
                isinstance(verified_provider_calls, list)
                and provider_call_key in verified_provider_calls
            ):
                return
        checks = [
            (
                "model.invoke",
                lambda *, consume_approval_token=True: self._check_authority_for_model_api(
                    provider_id=provider_id,
                    api_id=api_id,
                    model_id=model_id,
                    model_ref=model_ref,
                    params=params,
                    provider=provider,
                    stream=stream,
                    consume_approval_token=consume_approval_token,
                ),
            ),
            (
                "api_key.use",
                lambda *, consume_approval_token=True: self._check_authority_for_api_key_use(
                    provider_id=provider_id,
                    api_id=api_id,
                    model_id=model_id,
                    model_ref=model_ref,
                    params=params,
                    provider=provider,
                    stream=stream,
                    consume_approval_token=consume_approval_token,
                ),
            ),
            (
                "network.egress",
                lambda *, consume_approval_token=True: self._check_authority_for_network_egress(
                    provider_id=provider_id,
                    api_id=api_id,
                    model_id=model_id,
                    model_ref=model_ref,
                    params=params,
                    provider=provider,
                    stream=stream,
                    consume_approval_token=consume_approval_token,
                ),
            ),
        ]
        if self._has_authority_token_for_permission(params, "model.invoke"):
            missing_related = [
                item
                for item in checks
                if item[0] != "model.invoke"
                and not self._has_authority_token_for_permission(params, item[0])
            ]
            if missing_related:
                checks = missing_related + [item for item in checks if item not in missing_related]
        token_consumes: list[dict[str, object]] = []
        rechecks: list[Callable[..., object]] = []
        for permission_id, check_fn in checks:
            decision = check_fn(consume_approval_token=False)
            consume_item = self._authority_batch_consume_item(params, permission_id, decision)
            if consume_item:
                token_consumes.append(consume_item)
            else:
                rechecks.append(check_fn)
        for check in rechecks:
            check(consume_approval_token=True)
        if token_consumes:
            decision = _removed_authority_boundary().consume_one_shot_approvals_atomically(
                token_consumes
            )
            if not getattr(decision, "allowed", False):
                raise AuthorityApprovalRequired(decision)
            if isinstance(authority_context, dict):
                current = authority_context.get("_provider_one_shot_verified_for_run")
                verified = list(current) if isinstance(current, list) else []
                if provider_call_key not in verified:
                    verified.append(provider_call_key)
                authority_context["_provider_one_shot_verified_for_run"] = verified

    def _api_route_attempts(self, model, route_refs, params=None, stream=False):
        self._deny_direct_provider_invocation()
        attempts = []
        for route_ref in route_refs:
            provider_id, api_id = self._route_parts(route_ref)
            if not provider_id:
                continue
            route_model = self._model_for_route(model, provider_id)
            provider, model_name = self.resolve_provider(route_model)
            if provider.__class__.__name__ == "StubProvider":
                continue
            if not self._provider_api_key_configured(provider_id, api_id):
                continue
            self._check_authority_for_model_and_api_key_use(
                provider_id=provider_id,
                api_id=api_id,
                model_id=model_name,
                model_ref=model,
                params=params,
                provider=provider,
                stream=stream,
            )
            api_key = read_provider_api_key(provider_id, api_id)
            if not api_key:
                continue
            attempts.append(
                (provider, model_name, api_key, provider_api_metadata(provider_id, api_id))
            )
        return attempts

    def _api_bound_profile_parts(self, model):
        if not isinstance(model, str) or "/" not in model:
            return None
        parts = model.split("/")
        if len(parts) < 3:
            return None
        provider_id = parts[0].strip()
        api_id = parts[1].strip()
        model_id = "/".join(parts[2:]).strip()
        if not provider_id or not api_id or not model_id:
            return None
        named_key = next(
            (
                item
                for item in provider_named_api_keys(provider_id)
                if str(item.get("api_id") or "").strip() == api_id and item.get("configured")
            ),
            None,
        )
        if not named_key:
            return None
        metadata = provider_api_metadata(provider_id, api_id)
        allowed = {
            str(item) for item in metadata.get("allowed_models", []) if str(item or "").strip()
        }
        if allowed and model_id not in allowed and f"{provider_id}/{model_id}" not in allowed:
            return None
        return provider_id, api_id, model_id, metadata

    def _call_api_bound_profile(self, method_name, model, messages, tools=None, params=None):
        self._deny_direct_provider_invocation()
        parts = self._api_bound_profile_parts(model)
        if parts is None:
            return None, False
        provider_id, api_id, model_id, metadata = parts
        route_model = f"{provider_id}/{model_id}"
        provider, model_name = self.resolve_provider(route_model)
        if provider.__class__.__name__ == "StubProvider":
            return None, False
        self._check_authority_for_model_and_api_key_use(
            provider_id=provider_id,
            api_id=api_id,
            model_id=model_id,
            model_ref=model,
            params=params,
            provider=provider,
            stream=(method_name == "stream"),
        )
        api_key = read_provider_api_key(provider_id, api_id)
        if not api_key:
            return None, False
        if method_name == "stream":
            return self._stream_with_api_routes(
                [(provider, model_name, api_key, metadata)], messages, tools, params
            ), True
        return self._call_provider_with_overrides(
            provider, model_name, api_key, metadata, method_name, messages, tools, params
        ), True

    def _call_provider_with_overrides(
        self,
        provider,
        model_name,
        api_key,
        metadata,
        method_name,
        messages,
        tools=None,
        params=None,
    ):
        self._deny_direct_provider_invocation()
        had_key = hasattr(provider, "_api_key")
        previous_key = getattr(provider, "_api_key", None)
        had_base_url = hasattr(provider, "_base_url")
        previous_base_url = getattr(provider, "_base_url", None)
        had_base_url_attr = hasattr(provider, "BASE_URL")
        previous_base_url_attr = getattr(provider, "BASE_URL", None)
        base_url = str((metadata or {}).get("base_url") or "").strip().rstrip("/")
        try:
            if api_key:
                provider._api_key = api_key
            if base_url:
                provider._base_url = base_url
                provider.BASE_URL = base_url
            method = getattr(provider, method_name)
            return method(model_name, messages, tools or [], self._strip_authority_params(params))
        finally:
            if had_key:
                provider._api_key = previous_key
            elif api_key and hasattr(provider, "_api_key"):
                delattr(provider, "_api_key")
            if had_base_url:
                provider._base_url = previous_base_url
            elif base_url and hasattr(provider, "_base_url"):
                delattr(provider, "_base_url")
            if had_base_url_attr:
                provider.BASE_URL = previous_base_url_attr
            elif base_url and hasattr(provider, "BASE_URL"):
                delattr(provider, "BASE_URL")

    def _call_with_api_routes(self, method_name, model, messages, tools=None, params=None):
        self._deny_direct_provider_invocation()
        routed, handled = self._call_api_bound_profile(method_name, model, messages, tools, params)
        if handled:
            return routed, True
        route_refs = self._routes_for_model(model)
        if not route_refs:
            return None, False

        if method_name == "stream":
            route_attempts = self._api_route_attempts(model, route_refs, params=params, stream=True)
            if not route_attempts:
                return None, False
            return self._stream_with_api_routes(route_attempts, messages, tools, params), True

        last_error = None
        for provider, model_name, api_key, metadata in self._api_route_attempts(
            model, route_refs, params=params, stream=False
        ):
            try:
                return self._call_provider_with_overrides(
                    provider, model_name, api_key, metadata, method_name, messages, tools, params
                ), True
            except Exception as exc:
                last_error = exc
                if not self._is_rate_limit_error(exc):
                    raise
        if last_error is not None:
            raise last_error
        return None, False

    def _stream_with_api_routes(self, route_attempts, messages, tools=None, params=None):
        self._deny_direct_provider_invocation()
        last_error = None
        for provider, model_name, api_key, metadata in route_attempts:
            had_key = hasattr(provider, "_api_key")
            previous_key = getattr(provider, "_api_key", None)
            had_base_url = hasattr(provider, "_base_url")
            previous_base_url = getattr(provider, "_base_url", None)
            had_base_url_attr = hasattr(provider, "BASE_URL")
            previous_base_url_attr = getattr(provider, "BASE_URL", None)
            base_url = str((metadata or {}).get("base_url") or "").strip().rstrip("/")
            yielded = False
            try:
                if api_key:
                    provider._api_key = api_key
                if base_url:
                    provider._base_url = base_url
                    provider.BASE_URL = base_url
                for chunk in provider.stream(
                    model_name, messages, tools or [], self._strip_authority_params(params)
                ):
                    yielded = True
                    yield chunk
                return
            except Exception as exc:
                last_error = exc
                if yielded or not self._is_rate_limit_error(exc):
                    raise
            finally:
                if had_key:
                    provider._api_key = previous_key
                elif api_key and hasattr(provider, "_api_key"):
                    delattr(provider, "_api_key")
                if had_base_url:
                    provider._base_url = previous_base_url
                elif base_url and hasattr(provider, "_base_url"):
                    delattr(provider, "_base_url")
                if had_base_url_attr:
                    provider.BASE_URL = previous_base_url_attr
                elif base_url and hasattr(provider, "BASE_URL"):
                    delattr(provider, "BASE_URL")
        if last_error is not None:
            raise last_error

    def _composite_models(self):
        data = self._settings_data()
        models = data.get("models") if isinstance(data.get("models"), dict) else {}
        raw = self._jsonish(models.get("composite_models"), [])
        if isinstance(raw, dict):
            items = [
                {"id": key, **(value if isinstance(value, dict) else {})}
                for key, value in raw.items()
            ]
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        composites = {}
        for item in items:
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            composite_id = str(
                item.get("id") or item.get("profile_id") or item.get("name") or ""
            ).strip()
            if composite_id:
                composites[composite_id] = item
        return composites

    def _composite_for_model(self, model):
        if not isinstance(model, str):
            return None
        composites = self._composite_models()
        if model in composites:
            return composites[model]
        if "/" in model:
            tail = model.split("/", 1)[1]
            return composites.get(tail)
        return None

    def _model_pack_for_model(self, model):
        if not isinstance(model, str):
            return None
        store = ModelPackStore(
            self._settings_data().get("models")
            if isinstance(self._settings_data().get("models"), dict)
            else {}
        )
        return store.get(model)

    def _complete_model_pack(self, model_pack, messages, tools=None, params=None):
        params = dict(params or {})
        if (
            str(getattr(model_pack, "id", "") or "").strip() == rumi_process.RUMI_MODEL_PACK_ID
            and str(params.get("rumi_base_model_override") or "").strip()
        ):
            model_pack = ModelPack.from_dict(
                rumi_process.default_rumi_model_pack(
                    base_model=str(params.get("rumi_base_model_override")).strip()
                )
            )
        selection = select_model_pack(
            model_pack,
            {
                "user_text": self._messages_text(messages),
                "has_images": self._messages_have_images(messages),
                "requires_tool_calling": bool(tools),
                "requested_thinking_level": str((params or {}).get("thinking_level") or ""),
                "task_hints": (params or {}).get("task_hints")
                if isinstance((params or {}).get("task_hints"), dict)
                else {},
            },
            settings=self._settings_data().get("models")
            if isinstance(self._settings_data().get("models"), dict)
            else {},
        )
        if selection is None or not selection.ordered_members:
            raise RuntimeError("model pack has no runnable members")
        pack_mode = str(getattr(model_pack, "mode", "fallback_chain") or "fallback_chain")
        composite_mode = (
            pack_mode if pack_mode in {"ensemble", "review_chain"} else "fallback_chain"
        )
        composite = {
            "id": selection.pack_id,
            "mode": composite_mode,
            "members": selection.ordered_members,
        }
        metadata = getattr(model_pack, "metadata", {}) if model_pack is not None else {}
        if isinstance(metadata, dict):
            composite["metadata"] = dict(metadata)
        budget = getattr(model_pack, "budget", {}) if model_pack is not None else {}
        if isinstance(budget, dict):
            composite["budget"] = dict(budget)
        safety = getattr(model_pack, "safety", {}) if model_pack is not None else {}
        if isinstance(safety, dict):
            composite["safety"] = dict(safety)
        merge_model = (
            str(metadata.get("merge_model") or "").strip() if isinstance(metadata, dict) else ""
        )
        if merge_model:
            composite["merge_model"] = merge_model
        response = self._complete_composite(composite, messages, tools, params)
        if isinstance(response, dict):
            response_metadata = dict(response.get("metadata") or {})
            response_metadata["model_pack"] = selection.to_dict()
            response["metadata"] = response_metadata
        return response

    def _complete_composite(self, composite, messages, tools=None, params=None):
        mode = str(composite.get("mode") or composite.get("type") or "fallback_chain")
        members = composite.get("members", composite.get("models", composite.get("chain", [])))
        if isinstance(members, str):
            members = [part.strip() for part in members.split(",") if part.strip()]
        if not isinstance(members, list) or not members:
            raise RuntimeError("composite model has no members")
        if mode == "ensemble":
            return self._complete_ensemble(composite, members, messages, tools, params)
        if mode == "review_chain":
            return self._complete_review_chain(composite, members, messages, tools, params)
        return self._complete_fallback_chain(members, messages, tools, params)

    def _member_model(self, member):
        if isinstance(member, dict):
            return str(member.get("model") or member.get("profile_id") or "").strip()
        return str(member or "").strip()

    def _complete_fallback_chain(self, members, messages, tools=None, params=None):
        last_error = None
        for member in members:
            model = self._member_model(member)
            if not model:
                continue
            if not self._member_conditions_match(member, messages, tools, params):
                continue
            try:
                next_params = dict(params or {})
                next_params["_composite_depth"] = (
                    int(next_params.get("_composite_depth", 0) or 0) + 1
                )
                return self.complete(model, messages, tools or [], next_params)
            except Exception as exc:
                last_error = exc
                if not self._should_fallback_from_member_error(member, exc):
                    raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("composite fallback chain had no runnable members")

    def _member_conditions_match(self, member, messages, tools=None, params=None):
        if not isinstance(member, dict):
            return True
        conditions = member.get("conditions") or member.get("when") or {}
        if not isinstance(conditions, dict) or not conditions:
            return True
        has_images = self._messages_have_images(messages)
        has_tools = bool(tools)
        if "has_images" in conditions and bool(conditions.get("has_images")) != has_images:
            return False
        if (
            "requires_vision" in conditions
            and bool(conditions.get("requires_vision")) != has_images
        ):
            return False
        if "has_tools" in conditions and bool(conditions.get("has_tools")) != has_tools:
            return False
        if "requires_tools" in conditions and bool(conditions.get("requires_tools")) != has_tools:
            return False
        text_contains = conditions.get("text_contains") or conditions.get("contains")
        if text_contains and not self._condition_text_matches(
            text_contains, self._messages_text(messages)
        ):
            return False
        task_types = conditions.get("task_types") or conditions.get("task_type")
        if task_types and not self._condition_task_type_matches(task_types, params or {}):
            return False
        return True

    def _should_fallback_from_member_error(self, member, exc):
        fallback_on = member.get("fallback_on") if isinstance(member, dict) else None
        values = self._fallback_on_values(fallback_on)
        if not values:
            return self._is_rate_limit_error(exc)
        if "*" in values or "any" in values or "all" in values:
            return True
        kind = self._error_kind(exc)
        aliases = {
            "429": "rate_limit",
            "rate_limit_error": "rate_limit",
            "rate-limit": "rate_limit",
            "quota_exceeded": "quota",
            "resource_exhausted": "quota",
            "provider error": "provider_error",
            "server_error": "provider_error",
            "5xx": "provider_error",
            "timed_out": "timeout",
        }
        normalized = {aliases.get(value, value) for value in values}
        return kind in normalized

    @staticmethod
    def _fallback_on_values(value):
        if isinstance(value, str):
            raw = re.split(r"[,\s]+", value)
        elif isinstance(value, list):
            raw = value
        else:
            raw = []
        return {str(item or "").strip().casefold() for item in raw if str(item or "").strip()}

    @staticmethod
    def _error_kind(exc):
        message = str(exc).casefold()
        if "429" in message or "rate limit" in message or "rate_limit" in message:
            return "rate_limit"
        if "quota" in message or "resource_exhausted" in message:
            return "quota"
        if "timeout" in message or "timed out" in message:
            return "timeout"
        if (
            "401" in message
            or "403" in message
            or "unauthorized" in message
            or "forbidden" in message
        ):
            return "unauthorized"
        if any(
            token in message
            for token in ("provider_error", "provider error", "502", "503", "504", "temporarily")
        ):
            return "provider_error"
        return "unknown"

    @classmethod
    def _condition_text_matches(cls, expected, text):
        haystack = str(text or "").casefold()
        if isinstance(expected, str):
            needles = [expected]
        elif isinstance(expected, list):
            needles = expected
        else:
            return True
        needles = [
            str(item or "").strip().casefold() for item in needles if str(item or "").strip()
        ]
        return not needles or any(needle in haystack for needle in needles)

    @staticmethod
    def _condition_task_type_matches(expected, params):
        hints = params.get("task_hints") if isinstance(params.get("task_hints"), dict) else {}
        actual = (
            str(params.get("task_type") or hints.get("task_type") or hints.get("type") or "")
            .strip()
            .casefold()
        )
        if not actual:
            return False
        if isinstance(expected, str):
            options = [expected]
        elif isinstance(expected, list):
            options = expected
        else:
            return True
        return actual in {
            str(item or "").strip().casefold() for item in options if str(item or "").strip()
        }

    @staticmethod
    def _messages_text(messages):
        parts = []
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        parts.append(str(block.get("text") or block.get("content") or ""))
        return "\n".join(parts)

    @staticmethod
    def _messages_have_images(messages):
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "").casefold()
                    mime = str(block.get("mime_type") or block.get("mime") or "").casefold()
                    if block_type in {"image", "image_url", "input_image"} or mime.startswith(
                        "image/"
                    ):
                        return True
        return False

    def _complete_ensemble(self, composite, members, messages, tools=None, params=None):
        member_models = [
            self._member_model(member) for member in members if self._member_model(member)
        ]
        if not member_models:
            raise RuntimeError("composite ensemble has no runnable members")
        responses = []
        errors = []

        def call_member(model):
            next_params = dict(params or {})
            next_params["_composite_depth"] = int(next_params.get("_composite_depth", 0) or 0) + 1
            return model, self.complete(model, messages, tools or [], next_params)

        with ThreadPoolExecutor(max_workers=min(4, len(member_models))) as executor:
            futures = [executor.submit(call_member, model) for model in member_models]
            for future in as_completed(futures):
                try:
                    model, response = future.result()
                    responses.append(
                        {
                            "model": model,
                            "response": response,
                            "text": self._response_text(response),
                        }
                    )
                except Exception as exc:
                    errors.append(str(exc))
        if not responses:
            raise RuntimeError("all ensemble members failed: " + "; ".join(errors))
        merge_model = str(
            composite.get("merge_model") or composite.get("synthesizer_model") or ""
        ).strip()
        if merge_model:
            synthesis_prompt = [
                {
                    "role": "system",
                    "content": "Merge multiple model answers into one concise final answer. Preserve correct details and note uncertainty only when answers conflict.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "original_messages": messages,
                            "member_answers": [
                                {"model": item["model"], "answer": item["text"]}
                                for item in responses
                            ],
                            "member_errors": errors,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            next_params = dict(params or {})
            next_params["_composite_depth"] = int(next_params.get("_composite_depth", 0) or 0) + 1
            merged = self.complete(merge_model, synthesis_prompt, [], next_params)
            metadata = dict(merged.get("metadata") or {}) if isinstance(merged, dict) else {}
            metadata["ensemble"] = {
                "members": [item["model"] for item in responses],
                "errors": errors,
            }
            if isinstance(merged, dict):
                merged["metadata"] = metadata
            return merged
        return {
            "content": [
                {
                    "type": "text",
                    "text": "\n\n".join(f"[{item['model']}]\n{item['text']}" for item in responses),
                }
            ],
            "finish_reason": "ensemble",
            "usage": {},
            "metadata": {
                "ensemble": {"members": [item["model"] for item in responses], "errors": errors}
            },
        }

    def _complete_review_chain(self, composite, members, messages, tools=None, params=None):
        params = dict(params or {})
        runnable_members = [
            member
            for member in members
            if self._member_model(member)
            and self._member_conditions_match(member, messages, tools, params)
        ]
        if not runnable_members:
            raise RuntimeError("review_chain composite has no runnable members")

        generator_member = self._review_chain_member(
            runnable_members,
            {"generator", "primary", "drafter", "planner"},
            default_index=0,
        )
        reviewer_member = self._review_chain_member(
            runnable_members,
            {"reviewer", "judge", "critic"},
            default_index=1 if len(runnable_members) > 1 else 0,
        )
        generator_model = self._member_model(generator_member)
        reviewer_model = self._member_model(reviewer_member)
        composite_metadata = (
            composite.get("metadata") if isinstance(composite.get("metadata"), dict) else {}
        )
        budget = composite.get("budget") if isinstance(composite.get("budget"), dict) else {}
        generator_model = self._resolve_rumi_member_model(generator_model, params)
        reviewer_model = self._resolve_rumi_member_model(reviewer_model, params)
        context = rumi_process.context_for_request(messages, tools or [], params)
        max_reviews = RumiProcessRunner._positive_int(
            params.get("max_review_rounds")
            or budget.get("max_review_rounds")
            or composite_metadata.get("max_review_rounds"),
            default=2,
            upper=5,
        )
        base_model_metadata = (
            rumi_process.rumi_base_model_metadata(generator_model)
            if composite.get("id") == rumi_process.RUMI_MODEL_PACK_ID
            or composite_metadata.get("builtin")
            else {}
        )
        process = {
            "trace_id": rumi_process.trace_id(),
            "process_version": rumi_process.RUMI_PROCESS_VERSION,
            "mode": context["mode"],
            "deepthink_enabled": rumi_process.deepthink_enabled(params),
            "base_model": generator_model,
            **base_model_metadata,
            "reviewer_model": reviewer_model,
            "events": [],
            "watchdog": {
                "max_review_rounds": max_reviews,
                "quarantine_on_exhaustion": True,
            },
            "criteria": list(rumi_process.RUMI_CRITERIA),
            "action_preflight_required": bool(context.get("action_preflight_required")),
        }
        if process["deepthink_enabled"]:
            harness_tool_selection = rumi_process.select_harness_tools(
                messages, tools or [], params
            )
            context["harness_tool_selection"] = harness_tool_selection
            process["mode"] = "deepthink"
            process["warnings"] = [rumi_process.RUMI_DEEPTHINK_WARNING_JA]
            process["tooling"] = {
                "model_tool_ids": harness_tool_selection.get("model_tool_ids", []),
                "harness_tool_ids": harness_tool_selection.get("harness_tool_ids", []),
                "vision_tool_ids": harness_tool_selection.get("vision_tool_ids", []),
                "model_tools_are_separate_from_harness_tools": True,
            }
        runner = RumiProcessRunner(
            complete=self.complete,
            response_text=self._response_text,
            error_kind=self._error_kind,
        )
        return runner.run_review_chain(
            composite=composite,
            generator_member=generator_member,
            reviewer_member=reviewer_member,
            generator_model=generator_model,
            reviewer_model=reviewer_model,
            messages=messages,
            tools=tools or [],
            params=params,
            context=context,
            process=process,
            max_reviews=max_reviews,
        )

    @staticmethod
    def _review_chain_member(members, roles, default_index=0):
        for member in members:
            metadata_value = member.get("metadata") if isinstance(member, dict) else None
            metadata: dict[str, object] = (
                {str(key): value for key, value in metadata_value.items()}
                if isinstance(metadata_value, dict)
                else {}
            )
            role_value = ""
            if isinstance(member, dict):
                role_value = str(metadata.get("role") or member.get("role") or "")
            role = str(role_value).strip().casefold()
            if role in roles:
                return member
        index = min(max(0, int(default_index or 0)), len(members) - 1)
        return members[index]

    def _resolve_rumi_member_model(self, model, params=None):
        model_id = str(model or "").strip()
        if model_id != rumi_process.RUMI_BASE_MODEL:
            return model
        if isinstance(params, dict) and params.get("rumi_require_intended_base_model"):
            return model
        available_models: list[str] = []
        for profile in self.list_models():
            if not isinstance(profile, dict):
                continue
            for key in ("id", "profile_id", "qualified_model_id", "model_ref"):
                value = str(profile.get(key) or "").strip()
                if value:
                    available_models.append(value)
        return rumi_process.resolve_rumi_base_model(
            available_models,
            available_providers=set(self._providers.keys()),
        )

    @staticmethod
    def _response_text(response):
        if not isinstance(response, dict):
            return str(response or "")
        content = response.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or block.get("content") or ""))
                else:
                    parts.append(str(block))
            return "\n".join(part for part in parts if part)
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else {}
            if isinstance(message, dict):
                return str(message.get("content") or "")
        return ""

    def complete(self, model, messages, tools=None, params=None):
        del model, messages, tools, params
        self._deny_direct_provider_invocation()

    def stream(self, model, messages, tools=None, params=None):
        del model, messages, tools, params
        self._deny_direct_provider_invocation()

    @staticmethod
    def _provider_params(params):
        provider_params = dict(params or {})
        for key in (
            "deepthink_enabled",
            "deepthink",
            "rumi_deepthink",
            "deepthink_max_review_iterations",
            "deepthink_user_rejection_review_cycles",
            "deepthink_max_sections",
            "deepthink_loop_breaker",
            "rumi_base_model_override",
            "rumi_require_intended_base_model",
            "_authority_context",
        ):
            provider_params.pop(key, None)
        return provider_params

    def supports_stream(self, model):
        provider, _ = self.resolve_provider(model)
        try:
            from domain.ai_client.base_provider import BaseProvider

            return provider.__class__.stream is not BaseProvider.stream
        except Exception:
            return callable(getattr(provider, "stream", None))

    def list_models(self, provider=None):
        """登録済みプロバイダーの既知モデル一覧を返す。"""
        active_provider_ids = self._active_provider_ids()
        if provider is not None and provider not in active_provider_ids:
            return []

        models = get_all_known_models(
            provider_id=provider,
            active_provider_ids=active_provider_ids,
        )
        models = [model for model in models if model.get("provider_id") in active_provider_ids]

        catalog_map = get_provider_catalog_map(active_provider_ids=active_provider_ids)
        seen = {model.get("qualified_model_id") for model in models}
        provider_ids = [provider] if provider else sorted(active_provider_ids)
        for provider_id in provider_ids:
            provider_entry = catalog_map.get(provider_id)
            if provider_entry is None:
                continue
            for raw in self._provider_model_candidates(provider_id):
                candidate = self._normalize_runtime_model(provider_id, provider_entry, raw)
                if candidate is None:
                    continue
                qualified_model_id = candidate.get("qualified_model_id")
                if qualified_model_id in seen:
                    continue
                seen.add(qualified_model_id)
                models.append(candidate)
        return models

    def list_providers(self):
        active_provider_ids = self._active_provider_ids()
        catalog = get_provider_catalog(active_provider_ids=active_provider_ids)
        active = [
            provider for provider in catalog if provider.get("provider_id") in active_provider_ids
        ]
        known_ids = {provider.get("provider_id") for provider in active}
        for provider_id in sorted(active_provider_ids - known_ids):
            provider = self._providers.get(provider_id)
            active.append(
                {
                    "id": provider_id,
                    "provider_id": provider_id,
                    "name": getattr(provider, "display_name", provider_id.capitalize()),
                    "display_name": getattr(provider, "display_name", provider_id.capitalize()),
                    "kind": "custom",
                    "description": "",
                    "env_vars": [],
                    "base_url_envs": [],
                    "default_model": "",
                    "capabilities": [],
                    "availability": {
                        "active": True,
                        "available": True,
                        "configured": True,
                        "catalog_only": False,
                        "supports_invoke": callable(getattr(provider, "complete", None)),
                        "status": "active",
                    },
                    "metadata": {
                        "catalog_only": False,
                        "supports_invoke": callable(getattr(provider, "complete", None)),
                        "default_base_url": "",
                    },
                }
            )
        return active

    def list_profiles(self, provider=None):
        active_provider_ids = self._active_provider_ids()
        profiles = build_profile_catalog(
            active_provider_ids=active_provider_ids,
            custom_profiles=self._profiles,
        )
        try:
            from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

            service = ModelRuntimeSettingsService()
            profiles.extend(service.runtime_defined_profiles(service.get_settings()))
            profiles.extend(self._api_key_bound_profiles())
        except Exception:
            pass
        profiles = [
            profile
            for profile in profiles
            if (
                not profile.get("provider_id")
                or profile.get("provider_id") in active_provider_ids
                or profile.get("provider_id") == "composite"
                or (
                    isinstance(profile.get("metadata"), dict)
                    and profile["metadata"].get("api_bound")
                )
            )
        ]
        if provider is not None:
            profiles = [profile for profile in profiles if profile.get("provider_id") == provider]
        return profiles

    def _api_key_bound_profiles(self):
        profiles = []
        for api_key in provider_named_api_keys():
            provider_id = str(api_key.get("provider_id") or "").strip()
            api_id = str(api_key.get("api_id") or "").strip()
            allowed = [
                str(item).strip()
                for item in api_key.get("allowed_models", [])
                if str(item or "").strip()
            ]
            default_model = str(api_key.get("default_model") or "").strip()
            if default_model and default_model not in allowed:
                allowed.insert(0, default_model)
            configured = bool(
                api_key.get("configured") and self._provider_api_key_configured(provider_id, api_id)
            )
            availability = {
                "configured": configured,
                "active": configured,
                "status": "configured" if configured else "missing_api_key",
                "api_bound": True,
            }
            for model_id in allowed:
                display = f"{model_id} ({api_key.get('name') or api_id})"
                profile_id = f"{provider_id}/{api_id}/{model_id}"
                profiles.append(
                    {
                        "id": profile_id,
                        "profile_id": profile_id,
                        "qualified_model_id": profile_id,
                        "provider_id": provider_id,
                        "provider": provider_id,
                        "model_id": model_id,
                        "model": model_id,
                        "display_name": display,
                        "name": display,
                        "type": "chat",
                        "configured": configured,
                        "availability": dict(availability),
                        "metadata": {
                            "api_bound": True,
                            "api_id": api_id,
                            "base_url": api_key.get("base_url", ""),
                            "notes": api_key.get("notes", ""),
                            "quota_label": api_key.get("quota_label", ""),
                        },
                    }
                )
        return profiles

    def _check_authority_for_direct_provider_call(
        self, *, provider, provider_id, model_id, model_ref, params=None
    ):
        if not self._provider_requires_authority(provider_id, provider, "legacy"):
            return
        self._check_authority_for_model_and_api_key_use(
            provider_id=provider_id,
            api_id="legacy",
            model_id=model_id,
            model_ref=model_ref,
            params=params,
            provider=provider,
            stream=False,
        )

    def embed(self, model, input_text):
        del model, input_text
        self._deny_direct_provider_invocation()

    def image_gen(self, model, prompt, params=None):
        del model, prompt, params
        self._deny_direct_provider_invocation()

    def image_analyze(self, model, image, prompt):
        del model, image, prompt
        self._deny_direct_provider_invocation()

    def transcribe(self, model, audio, params=None):
        del model, audio, params
        self._deny_direct_provider_invocation()

    def tts(self, model, text, voice=None):
        del model, text, voice
        self._deny_direct_provider_invocation()

    @staticmethod
    def _deny_direct_provider_invocation() -> None:
        raise DirectProviderInvocationDenied(
            "direct AIClient provider invocation is unavailable; use the captured "
            "Pack v4 AI gateway"
        )
