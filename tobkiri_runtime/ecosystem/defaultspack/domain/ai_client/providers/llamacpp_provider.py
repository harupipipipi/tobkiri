from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import time
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request

from core_runtime.paths import USER_DATA_DIR

from .openai_compatible_provider import OpenAICompatibleProvider


class LlamaCppProvider(OpenAICompatibleProvider):
    """Discover llama.cpp single-server and router inventories without loading models."""

    provider_name = "llamacpp"
    display_name = "llama.cpp"
    DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        *,
        cache_ttl_seconds: int = 60,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        resolved = self._normalize_base_url(base_url or self.DEFAULT_BASE_URL)
        self._reject_insecure_remote_credential(resolved, api_key)
        super().__init__(
            api_key=api_key,
            base_url=resolved,
            provider_id=self.provider_name,
            display_name=self.display_name,
            default_base_url=self.DEFAULT_BASE_URL,
            credential_required=False,
            known_models=[],
            extra_headers=dict(extra_headers or {}),
            remote_model_discovery=False,
        )
        self._inventory_ttl_seconds = max(10, int(cache_ttl_seconds))

    @classmethod
    def from_manifest(
        cls,
        manifest: Mapping[str, Any],
        *,
        api_key: str = "",
        model_manifests: list[dict[str, Any]] | None = None,
        allow_declared_models: bool = True,
    ) -> LlamaCppProvider:
        """Create the native adapter while intentionally ignoring static model seeds."""

        del model_manifests, allow_declared_models
        config = manifest.get("config")
        config = dict(config) if isinstance(config, Mapping) else {}
        return cls(
            api_key=api_key,
            base_url=str(manifest.get("default_base_url") or cls.DEFAULT_BASE_URL),
            cache_ttl_seconds=int(config.get("model_cache_ttl_seconds", 60) or 60),
            extra_headers=(
                manifest.get("headers") if isinstance(manifest.get("headers"), Mapping) else {}
            ),
        )

    @staticmethod
    def _normalize_base_url(value: Any) -> str:
        text = str(value or "").strip()
        if "://" not in text:
            text = f"http://{text}"
        parsed = urllib.parse.urlsplit(text)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("llama.cpp base URL must be a credential-free HTTP(S) endpoint")
        path = parsed.path.rstrip("/")
        if not path.endswith("/v1"):
            path = f"{path}/v1"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    @staticmethod
    def _reject_insecure_remote_credential(base_url: str, api_key: str) -> None:
        if not api_key:
            return
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme == "https" or LlamaCppProvider._is_loopback(parsed.hostname):
            return
        raise ValueError("llama.cpp API keys require HTTPS outside loopback")

    @staticmethod
    def _is_loopback(hostname: str | None) -> bool:
        host = str(hostname or "").strip().strip("[]").lower()
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _server_base(self) -> str:
        if self._base_url.endswith("/v1"):
            return self._base_url[: -len("/v1")].rstrip("/")
        return self._base_url.rstrip("/")

    def list_models(self) -> list[dict[str, Any]]:
        """Return the fresh or last-known-good native server inventory."""

        return self._inventory(force=False)

    def refresh_models(self) -> list[dict[str, Any]]:
        """Refresh read-only inventory without invoking router reload semantics."""

        return self._inventory(force=True)

    def _inventory(self, *, force: bool) -> list[dict[str, Any]]:
        cache = self._load_cache()
        now = int(time.time())
        if not force and cache and int(cache.get("expires_at") or 0) > now:
            return self._cached_models(cache.get("models"), stale=False)
        try:
            models = self._fetch_models()
        except Exception:
            models = None
        if models is not None:
            self._save_cache(models, now=now)
            return deepcopy(models)
        return self._cached_models(cache.get("models"), stale=True) if cache else []

    def _fetch_models(self) -> list[dict[str, Any]]:
        router_payload: dict[str, Any] | None
        try:
            router_payload = self._get_json("/models")
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 405}:
                raise
            router_payload = None
        router_models = router_payload.get("data") if isinstance(router_payload, dict) else None
        if isinstance(router_models, list):
            return self._normalize_models(router_models, router=True)

        single_payload = self._get_json("/v1/models")
        single_models = (
            single_payload.get("data") if isinstance(single_payload, dict) else None
        )
        if not isinstance(single_models, list):
            raise ValueError("llama.cpp model endpoint returned an invalid response")
        models = self._normalize_models(single_models, router=False)
        props = self._get_json("/props") if models else {}
        for model in models:
            self._merge_props(model, props)
        return models

    def _normalize_models(
        self,
        raw_models: list[Any],
        *,
        router: bool,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_models:
            if not isinstance(raw, Mapping):
                continue
            model_id = str(raw.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            status = raw.get("status")
            status_map = dict(status) if isinstance(status, Mapping) else {}
            load_state = str(status_map.get("value") or status or "").strip()
            if not load_state:
                load_state = "unknown" if router else "loaded"
            architecture = raw.get("architecture")
            architecture = dict(architecture) if isinstance(architecture, Mapping) else {}
            meta = raw.get("meta")
            meta = dict(meta) if isinstance(meta, Mapping) else {}
            input_modalities = self._string_list(architecture.get("input_modalities"))
            output_modalities = self._string_list(architecture.get("output_modalities"))
            embedding = "embedding" in output_modalities
            chat_template = bool(meta.get("chat_template"))
            model_type = "embedding" if embedding else "chat" if chat_template else "unknown"
            context_window = self._integer(
                meta.get("n_ctx_train") or meta.get("n_ctx") or raw.get("context_length")
            )
            metadata = {
                "source": "llamacpp_router_models" if router else "llamacpp_openai_models",
                "inventory_endpoint": "/models" if router else "/v1/models",
                "server_mode": "router" if router else "single",
                "load_state": load_state,
                "load_failed": bool(status_map.get("failed")) or load_state == "failed",
                "exit_code": status_map.get("exit_code"),
                "model_path": raw.get("path") or raw.get("root"),
                "context_window": context_window,
                "parameter_count": self._integer(meta.get("n_params")),
                "model_size": self._integer(meta.get("size")),
                "input_modalities": input_modalities,
                "output_modalities": output_modalities,
                "catalog_cache_state": "fresh",
                "capability_confidence": (
                    "provider_reported" if input_modalities or output_modalities else "unknown"
                ),
            }
            capabilities: dict[str, bool | None] = {
                "text_input": (
                    "text" in input_modalities if input_modalities else None
                ),
                "text_output": (
                    "text" in output_modalities if output_modalities else None
                ),
                "streaming": None,
                "image_input": (
                    "image" in input_modalities if input_modalities else None
                ),
                "audio_input": (
                    "audio" in input_modalities if input_modalities else None
                ),
                "tool_calling": None,
                "json_schema": None,
                "structured_output": None,
            }
            output.append(
                {
                    "id": f"llamacpp/{model_id}",
                    "qualified_model_id": f"llamacpp/{model_id}",
                    "provider_id": "llamacpp",
                    "provider": "llamacpp",
                    "model_id": model_id,
                    "name": model_id,
                    "display_name": self._display_name(model_id, raw.get("path")),
                    "type": model_type,
                    "context_window": context_window,
                    "max_context": context_window,
                    "capabilities": capabilities,
                    "metadata": metadata,
                }
            )
        return output

    @staticmethod
    def _display_name(model_id: str, model_path: Any) -> str:
        if not model_path and not ("/" in model_id or "\\" in model_id):
            return model_id
        return Path(str(model_path or model_id).replace("\\", "/")).name or model_id

    @staticmethod
    def _merge_props(model: dict[str, Any], props: Any) -> None:
        if not isinstance(props, Mapping):
            return
        generation = props.get("default_generation_settings")
        generation = dict(generation) if isinstance(generation, Mapping) else {}
        context_window = LlamaCppProvider._integer(
            generation.get("n_ctx") or props.get("n_ctx")
        )
        if context_window:
            model["context_window"] = context_window
            model["max_context"] = context_window
            model["metadata"]["context_window"] = context_window
        caps = props.get("chat_template_caps")
        caps = dict(caps) if isinstance(caps, Mapping) else {}
        modalities = props.get("modalities")
        modalities = dict(modalities) if isinstance(modalities, Mapping) else {}
        has_chat_template = bool(props.get("chat_template"))
        if has_chat_template:
            model["type"] = "chat"
            model["capabilities"]["text_input"] = True
            model["capabilities"]["text_output"] = True
            model["capabilities"]["streaming"] = True
        model["capabilities"]["tool_calling"] = bool(
            caps.get("supports_tools") or caps.get("tool_use")
        )
        model["capabilities"]["json_schema"] = bool(
            caps.get("supports_json_schema") or caps.get("grammar")
        )
        model["capabilities"]["structured_output"] = model["capabilities"][
            "json_schema"
        ]
        if "vision" in modalities:
            model["capabilities"]["image_input"] = bool(modalities.get("vision"))
        if "audio" in modalities:
            model["capabilities"]["audio_input"] = bool(modalities.get("audio"))
        model["metadata"].update(
            {
                "chat_template_present": has_chat_template,
                "chat_template_capabilities": caps,
                "is_sleeping": bool(props.get("is_sleeping")),
                "build_info": props.get("build_info"),
                "capability_confidence": "provider_reported",
            }
        )

    def model_props(self, model_id: str) -> dict[str, Any]:
        """Read router properties with llama.cpp autoload explicitly disabled."""

        query = urllib.parse.urlencode({"model": model_id, "autoload": "false"})
        return self._get_json(f"/props?{query}")

    def probe(self) -> dict[str, Any]:
        """Return read-only server health, treating a loading 503 as connected."""

        try:
            payload = self._get_json("/health")
        except urllib.error.HTTPError as exc:
            return {
                "connected": exc.code == 503,
                "status": "loading" if exc.code == 503 else "http_error",
                "status_code": exc.code,
            }
        except Exception:
            return {"connected": False, "status": "unreachable", "status_code": None}
        return {
            "connected": True,
            "status": str(payload.get("status") or "ok"),
            "status_code": 200,
        }

    def _get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._server_base()}{path}",
            headers=self._headers(content_type=""),
            method="GET",
        )
        with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("llama.cpp returned a non-object response")
        return payload

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip().lower() for item in value if str(item or "").strip()]

    def _scope(self) -> str:
        endpoint = self._base_url.rstrip("/").encode("utf-8")
        key = (self._api_key or "no-credential").encode("utf-8")
        return hmac.new(key, endpoint, hashlib.sha256).hexdigest()[:24]

    def _cache_path(self) -> Path:
        root = Path(USER_DATA_DIR) / "shared" / "provider_model_cache"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"llamacpp.{self._scope()}.models.json"

    def _load_cache(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("scope") != self._scope():
            return None
        return payload

    def _save_cache(self, models: list[dict[str, Any]], *, now: int) -> None:
        payload = {
            "provider_id": "llamacpp",
            "scope": self._scope(),
            "saved_at": now,
            "expires_at": now + self._inventory_ttl_seconds,
            "models": models,
        }
        try:
            self._cache_path().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return

    @staticmethod
    def _cached_models(raw: Any, *, stale: bool) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        output: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("provider_id") != "llamacpp":
                continue
            cached = deepcopy(item)
            cached["metadata"] = {
                **dict(cached.get("metadata") or {}),
                "catalog_cache_state": "stale" if stale else "fresh",
            }
            output.append(cached)
        return output
