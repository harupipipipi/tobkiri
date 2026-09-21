from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


OPENROUTER_CURATED_ALLOWLIST = [
    {
        "id": "openrouter/tencent/hy3-preview:free",
        "model_id": "tencent/hy3-preview:free",
        "name": "Tencent Hy3 preview (free)",
        "display_name": "Tencent Hy3 preview (free)",
        "provider": "openrouter",
        "provider_id": "openrouter",
        "type": "chat",
        "defaults": {"chat": True, "fast": True},
    },
    {
        "id": "openrouter/cohere/north-mini-code:free",
        "model_id": "cohere/north-mini-code:free",
        "name": "Cohere North Mini Code (free)",
        "display_name": "Cohere North Mini Code (free)",
        "provider": "openrouter",
        "provider_id": "openrouter",
        "type": "chat",
        "defaults": {"chat": True, "coding": True, "fast": True},
    },
]


def _openrouter_catalog_models():
    return [dict(model) for model in OPENROUTER_CURATED_ALLOWLIST]


class TestDefaultspackProviderExpansion(unittest.TestCase):
    def test_detect_available_providers_accepts_new_openai_compatible_provider_keys(self):
        from domain.ai_client.providers import detect_available_providers

        with patch.dict(
            os.environ,
            {"XAI_API_KEY": "x-key", "GROQ_API_KEY": "g-key", "DEEPSEEK_API_KEY": "d-key"},
            clear=True,
        ):
            providers = detect_available_providers()

        self.assertIn("xai", providers)
        self.assertIn("groq", providers)
        self.assertIn("deepseek", providers)

    def test_generic_provider_loads_profile_models_from_user_data(self):
        from domain.ai_client.providers.provider_catalog import XaiProvider

        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "profiles"
            target_dir = profile_dir / "grok-code-fast-1"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "profile.json").write_text(
                json.dumps(
                    {
                        "provider_id": "xai",
                        "model_id": "grok-code-fast-1",
                        "display_name": "Grok Code Fast 1",
                        "metadata": {"type": "chat"},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(XaiProvider, "profile_dir", return_value=profile_dir):
                provider = XaiProvider()
                model_ids = {item["id"] for item in provider.list_models()}

        self.assertIn("xai/grok-code-fast-1", model_ids)
        self.assertIn("xai/grok-4", model_ids)

    def test_get_all_known_models_includes_generic_provider_catalog(self):
        from domain.ai_client.providers import get_all_known_models

        model_ids = {item["id"] for item in get_all_known_models()}

        self.assertIn("groq/llama-3.3-70b-versatile", model_ids)
        # Curated entries are only an offline fallback; the exact Together
        # inventory is refreshed dynamically from the account-visible endpoint.
        self.assertTrue(any(model_id.startswith("together/") for model_id in model_ids))
        self.assertIn("mistral/mistral-large-latest", model_ids)

    def test_ai_client_lists_auto_registered_generic_provider_models(self):
        from domain.ai_client.client import AIClient
        from domain.ai_client.providers.provider_catalog import MistralProvider

        AIClient._instance = None
        with patch.object(
            MistralProvider,
            "_remote_discovered_models",
            return_value=[],
        ), patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "m-key", "RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS": "1"},
            clear=True,
        ):
            client = AIClient()

        try:
            models = client.list_models(provider="mistral")
        finally:
            AIClient._instance = None

        model_ids = {item["id"] for item in models}
        self.assertIn("mistral/mistral-large-latest", model_ids)
        self.assertIn("mistral/mistral-embed", model_ids)

    def test_openrouter_provider_uses_only_live_inventory_and_rejects_unknown_models(self):
        from domain.ai_client.providers.openrouter_provider import OpenRouterProvider

        with patch.object(
            OpenRouterProvider,
            "_remote_discovered_models",
            return_value=[{
                "id": "openrouter/openai/live-model",
                "model_id": "openai/live-model",
                "provider_id": "openrouter",
                "provider": "openrouter",
                "name": "Live model",
                "display_name": "Live model",
                "type": "chat",
            }],
        ), patch.object(
            OpenRouterProvider,
            "_load_remote_model_cache",
            return_value={"models": [{"id": "openai/live-model", "name": "Live model", "type": "chat"}]},
        ):
            provider = OpenRouterProvider()
            models = provider.list_models()

            model_ids = {item["id"] for item in models}
            self.assertEqual(model_ids, {"openrouter/openai/live-model"})
            self.assertIn("openrouter/openai/live-model", model_ids)
            for model in models:
                provider._assert_supported_model(model["model_id"])
                provider._assert_supported_model(model["id"])

            with self.assertRaisesRegex(RuntimeError, "live or last-known-good catalog"):
                provider._assert_supported_model("openai/gpt-4o-mini")

    def test_openrouter_empty_seed_does_not_hide_live_account_models(self):
        from domain.ai_client.providers.openrouter_provider import OpenRouterProvider

        provider = OpenRouterProvider(known_models=[])
        with patch.object(
            provider,
            "_remote_discovered_models",
            return_value=[{
                "id": "openrouter/account-visible-model",
                "model_id": "account-visible-model",
                "provider_id": "openrouter",
                "provider": "openrouter",
                "type": "chat",
            }],
        ):
            models = provider.list_models()

        self.assertEqual([item["id"] for item in models], ["openrouter/account-visible-model"])

    def test_gitlawb_opengateway_includes_mimo_v2_omni(self):
        from domain.ai_client.client import AIClient
        from domain.ai_client.providers.gitlawb_opengateway_provider import GitlawbOpengatewayProvider

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ,
                {
                    "RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS": "1",
                    "RUMI_DEFAULTSPACK_SECRETS_DIR": str(Path(tmpdir) / "secrets"),
                    "GITLAWB_OPENGATEWAY_API_KEY": "test-ogw-token",
                },
                clear=True,
            ):
                client = AIClient()

            try:
                models = client.list_models(provider="gitlawb-opengateway")
                provider, model_name = client.resolve_provider("gitlawb-opengateway/mimo-v2-omni")
            finally:
                AIClient._instance = None

        model = next(item for item in models if item["id"] == "gitlawb-opengateway/mimo-v2-omni")
        self.assertEqual(model_name, "mimo-v2-omni")
        self.assertEqual(getattr(provider, "provider_id", ""), "gitlawb-opengateway")
        self.assertIn("vision", model.get("capabilities", []))
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("Authorization", GitlawbOpengatewayProvider()._headers())

    def test_ai_client_does_not_stub_unconfigured_openrouter_completion(self):
        from domain.ai_client.client import AIClient

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir}, clear=True):
                client = AIClient()

            try:
                with self.assertRaisesRegex(RuntimeError, "openrouter: provider is not configured"):
                    client.complete(
                        "openrouter/tencent/hy3-preview:free",
                        [{"role": "user", "content": "hello"}],
                        [],
                        {},
                    )
            finally:
                AIClient._instance = None

    def test_api_routes_read_models_before_legacy_apis(self):
        from domain.ai_client.client import AIClient

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "frontend_settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {"model_api_routes": "google/gemini-test: google/models-main"},
                        "apis": {"model_api_routes": "google/gemini-test: google/apis-legacy"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                client = AIClient()

            try:
                with patch.object(client, "_settings_path", return_value=settings_path):
                    routes = client._api_routes()
            finally:
                AIClient._instance = None

        self.assertEqual(routes["google/gemini-test"], ["google/models-main"])

    def test_structured_api_routes_take_priority_over_text_routes(self):
        from domain.ai_client.client import AIClient

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "frontend_settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "api_routes": [
                                {"model": "google/gemini-test", "apis": ["google/main", "google/backup"]}
                            ],
                            "model_api_routes": "google/gemini-test: google/legacy",
                        }
                    }
                ),
                encoding="utf-8",
            )
            client = AIClient()

            try:
                with patch.object(client, "_settings_path", return_value=settings_path):
                    routes = client._api_routes()
            finally:
                AIClient._instance = None

        self.assertEqual(routes["google/gemini-test"], ["google/main", "google/backup"])

    def test_named_api_metadata_persists_api_bound_model_data(self):
        from domain.ai_client.api_key_store import provider_api_metadata, provider_named_api_keys, set_provider_api_key

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir}, clear=True):
                set_provider_api_key(
                    "longcat",
                    "secret",
                    api_id="work",
                    name="work",
                    base_url="https://api.longcat.chat/openai/v1",
                    allowed_models=["LongCat-Flash-Chat"],
                    default_model="LongCat-Flash-Chat",
                    notes="fast longcat route",
                    quota_label="paid",
                )

                metadata = provider_api_metadata("longcat", "work")
                keys = provider_named_api_keys("longcat")

        self.assertEqual(metadata["base_url"], "https://api.longcat.chat/openai/v1")
        self.assertEqual(metadata["allowed_models"], ["LongCat-Flash-Chat"])
        self.assertEqual(metadata["default_model"], "LongCat-Flash-Chat")
        self.assertEqual(keys[0]["quota_label"], "paid")

    def test_composite_fallback_chain_uses_next_model_on_quota_error(self):
        from domain.ai_client.client import AIClient

        class FailingProvider:
            def complete(self, model, messages, tools, params):
                raise RuntimeError("429 rate limit")

        class SuccessProvider:
            def complete(self, model, messages, tools, params):
                return {"content": [{"type": "text", "text": f"ok:{model}"}], "finish_reason": "stop"}

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "frontend_settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "composite_models": [
                                {
                                    "id": "combo/default",
                                    "mode": "fallback_chain",
                                    "members": ["fail/model-a", "win/model-b"],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            client = AIClient()
            client.register_provider("fail", FailingProvider())
            client.register_provider("win", SuccessProvider())

            try:
                with patch.object(client, "_settings_path", return_value=settings_path):
                    response = client.complete("combo/default", [{"role": "user", "content": "hello"}])
            finally:
                AIClient._instance = None

        self.assertEqual(response["content"][0]["text"], "ok:model-b")

    def test_composite_fallback_chain_honors_member_conditions(self):
        from domain.ai_client.client import AIClient

        class EchoProvider:
            def complete(self, model, messages, tools, params):
                return {"content": [{"type": "text", "text": f"ok:{model}"}], "finish_reason": "stop"}

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "frontend_settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "composite_models": [
                                {
                                    "id": "combo/conditional",
                                    "mode": "fallback_chain",
                                    "members": [
                                        {"model": "plain/text", "conditions": {"has_images": False}},
                                        {"model": "vision/omni", "conditions": {"has_images": True}},
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            client = AIClient()
            client.register_provider("plain", EchoProvider())
            client.register_provider("vision", EchoProvider())

            try:
                with patch.object(client, "_settings_path", return_value=settings_path):
                    response = client.complete(
                        "combo/conditional",
                        [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "describe this"},
                                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                                ],
                            }
                        ],
                    )
            finally:
                AIClient._instance = None

        self.assertEqual(response["content"][0]["text"], "ok:omni")

    def test_composite_fallback_chain_uses_fallback_on_error_kind(self):
        from domain.ai_client.client import AIClient

        class TimeoutProvider:
            def complete(self, model, messages, tools, params):
                raise RuntimeError("request timed out")

        class SuccessProvider:
            def complete(self, model, messages, tools, params):
                return {"content": [{"type": "text", "text": f"ok:{model}"}], "finish_reason": "stop"}

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "frontend_settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "composite_models": [
                                {
                                    "id": "combo/error-routing",
                                    "mode": "fallback_chain",
                                    "members": [
                                        {"model": "slow/model-a", "fallback_on": ["timeout"]},
                                        "win/model-b",
                                    ],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            client = AIClient()
            client.register_provider("slow", TimeoutProvider())
            client.register_provider("win", SuccessProvider())

            try:
                with patch.object(client, "_settings_path", return_value=settings_path):
                    response = client.complete("combo/error-routing", [{"role": "user", "content": "hello"}])
            finally:
                AIClient._instance = None

        self.assertEqual(response["content"][0]["text"], "ok:model-b")

    def test_composite_ensemble_merges_member_answers_without_synthesizer(self):
        from domain.ai_client.client import AIClient

        class EchoProvider:
            def __init__(self, prefix):
                self.prefix = prefix

            def complete(self, model, messages, tools, params):
                return {"content": [{"type": "text", "text": f"{self.prefix}:{model}"}], "finish_reason": "stop"}

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "frontend_settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "composite_models": [
                                {
                                    "id": "combo/ensemble",
                                    "mode": "ensemble",
                                    "members": ["a/one", "b/two"],
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            client = AIClient()
            client.register_provider("a", EchoProvider("a"))
            client.register_provider("b", EchoProvider("b"))

            try:
                with patch.object(client, "_settings_path", return_value=settings_path):
                    response = client.complete("combo/ensemble", [{"role": "user", "content": "hello"}])
            finally:
                AIClient._instance = None

        text = response["content"][0]["text"]
        self.assertIn("[a/one]", text)
        self.assertIn("[b/two]", text)
        self.assertEqual(set(response["metadata"]["ensemble"]["members"]), {"a/one", "b/two"})

    def test_api_route_stream_keeps_named_key_until_generator_is_consumed(self):
        from domain.ai_client import client as client_module
        from domain.ai_client.client import AIClient
        from core_runtime.authority.models import AuthorityDecision

        class StreamingProvider:
            def __init__(self):
                self._api_key = "original-secret"
                self.keys_seen = []

            def stream(self, model, messages, tools, params):
                def chunks():
                    self.keys_seen.append(self._api_key)
                    yield {"type": "delta", "model": model, "api_key": self._api_key}

                return chunks()

        class AllowAuthority:
            def check(self, **kwargs):
                return AuthorityDecision(
                    allowed=True,
                    permission_id=kwargs["permission_id"],
                    principal_id=kwargs["principal_id"],
                    reason="allowed",
                    resource=kwargs["resource"],
                )

        provider = StreamingProvider()
        AIClient._instance = None
        with patch.dict(os.environ, {}, clear=True):
            client = AIClient()
        client.register_provider("google", provider)

        try:
            with (
                patch.object(client, "_api_routes", return_value={"google/gemini-test": ["google/backup"]}),
                patch.object(
                    client_module,
                    "provider_named_api_keys",
                    return_value=[{"provider_id": "google", "api_id": "backup", "configured": True}],
                ),
                patch(
                    "core_runtime.authority.get_authority_service",
                    return_value=AllowAuthority(),
                ),
                patch.object(client_module, "read_provider_api_key", return_value="named-route-secret"),
            ):
                stream = client.stream("google/gemini-test", [{"role": "user", "content": "hello"}])

                self.assertEqual(provider._api_key, "original-secret")
                chunks = list(stream)

            self.assertEqual(provider.keys_seen, ["named-route-secret"])
            self.assertEqual(chunks[0]["api_key"], "named-route-secret")
            self.assertEqual(provider._api_key, "original-secret")
        finally:
            AIClient._instance = None

    def test_provider_models_with_slashes_are_not_misread_as_api_bound_profiles(self):
        from domain.ai_client.api_key_store import set_provider_api_key
        from domain.ai_client.client import AIClient

        AIClient._instance = None
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir}, clear=True):
                set_provider_api_key("nvidia", "secret")
                client = AIClient()

                try:
                    self.assertIsNone(client._api_bound_profile_parts("nvidia/meta/llama-3.3-70b-instruct"))
                    provider, model_name = client.resolve_provider("nvidia/meta/llama-3.3-70b-instruct")
                finally:
                    AIClient._instance = None

        self.assertEqual(provider.provider_id, "nvidia")
        self.assertEqual(model_name, "meta/llama-3.3-70b-instruct")


if __name__ == "__main__":
    unittest.main()
