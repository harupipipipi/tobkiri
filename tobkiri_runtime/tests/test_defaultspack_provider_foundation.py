from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestDefaultspackProviderCatalog(unittest.TestCase):
    def test_provider_catalog_contains_major_and_local_entries(self):
        from ecosystem.defaultspack.domain.ai_client.providers import (
            get_provider_catalog,
        )

        providers = {item["provider_id"]: item for item in get_provider_catalog()}
        self.assertIn("openai", providers)
        self.assertIn("anthropic", providers)
        self.assertIn("ollama", providers)
        self.assertEqual(providers["ollama"]["kind"], "local")

    def test_provider_catalog_manifest_coverage_has_no_orphans(self):
        from ecosystem.defaultspack.domain.ai_client.providers import (
            validate_provider_catalog_coverage,
        )

        self.assertEqual(validate_provider_catalog_coverage(), [])

    def test_model_catalog_exposes_cross_provider_identity_metadata(self):
        from ecosystem.defaultspack.backend.ai_client.provider_catalog import (
            list_model_catalog,
        )

        class Client:
            def list_models(self, provider=None):
                assert provider == "openai"
                return [{
                    "id": "openai/account-visible-model",
                    "qualified_model_id": "openai/account-visible-model",
                    "provider_id": "openai",
                    "model_id": "account-visible-model",
                    "display_name": "Account Visible Model",
                    "type": "chat",
                    "metadata": {"source": "remote_models_endpoint"},
                }]

        with patch(
            "ecosystem.defaultspack.backend.ai_client.provider_catalog._runtime_client",
            return_value=Client(),
        ):
            models = list_model_catalog(provider="openai")
        self.assertTrue(models)
        sample = next(model for model in models if model["model_id"] == "account-visible-model")
        self.assertEqual(sample["canonical_model_id"], "account-visible-model")
        self.assertEqual(sample["same_model_across_providers_key"], "account-visible-model")
        self.assertEqual(sample["qualified_model_id"], "openai/account-visible-model")

    def test_detect_available_providers_registers_openai_compatible_gateways(self):
        from ecosystem.defaultspack.domain.ai_client.providers import (
            detect_available_providers,
        )

        env = {
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_BASE_URL": "https://openrouter.example/v1",
        }
        with patch.dict(os.environ, env, clear=False):
            providers = detect_available_providers()
        self.assertIn("openrouter", providers)

    def test_provider_catalog_marks_google_configured_when_only_gemini_key_is_set(self):
        from ecosystem.defaultspack.domain.ai_client.providers import (
            get_provider_catalog,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "GEMINI_API_KEY": "test-key",
                "RUMI_DEFAULTSPACK_SECRETS_DIR": str(Path(tmpdir) / "secrets"),
            }
            with patch.dict(os.environ, env, clear=True):
                providers = {item["provider_id"]: item for item in get_provider_catalog()}

        google = providers["google"]
        self.assertIn("GEMINI_API_KEY", google["env_vars"])
        self.assertTrue(google["availability"]["configured"])
        self.assertIn(
            google["availability"]["configuration_source"],
            {"GEMINI_API_KEY", "defaultspack_secret"},
        )

    def test_provider_catalog_does_not_mark_google_configured_from_application_credentials(self):
        from ecosystem.defaultspack.domain.ai_client.providers import (
            get_provider_catalog,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/service-account.json",
                "RUMI_DEFAULTSPACK_SECRETS_DIR": str(Path(tmpdir) / "secrets"),
            }
            with patch.dict(os.environ, env, clear=True):
                providers = {item["provider_id"]: item for item in get_provider_catalog()}

        google = providers["google"]
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", google["env_vars"])
        self.assertFalse(google["availability"]["configured"])

    def test_provider_catalog_marks_google_configured_from_secret_store(self):
        from core_runtime.secrets_store import SecretsStore
        from ecosystem.defaultspack.domain.ai_client.providers import (
            get_all_known_models,
            get_provider_catalog,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_dir = Path(tmpdir) / "secrets"
            store = SecretsStore(str(secrets_dir))
            store.set_secret("GOOGLE_API_KEY", "secret-key", actor="test")
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}, clear=True):
                providers = {item["provider_id"]: item for item in get_provider_catalog()}
                models = get_all_known_models("google")

        google = providers["google"]
        self.assertEqual(google["availability"]["configuration_source"], "defaultspack_secret")
        self.assertTrue(google["availability"]["configured"])
        self.assertEqual(models, [])

    def test_provider_catalog_marks_google_configured_from_browser_oauth(self):
        from ecosystem.defaultspack.domain.ai_client.providers import (
            get_provider_catalog,
        )
        from ecosystem.defaultspack.domain.ai_client.oauth_store import (
            save_provider_oauth_client_config,
            save_provider_oauth_connection,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                save_provider_oauth_client_config(
                    "google",
                    """
                    {
                      "installed": {
                        "client_id": "test-client.apps.googleusercontent.com",
                        "client_secret": "test-secret"
                      }
                    }
                    """,
                    pack_root=pack_root,
                )
                save_provider_oauth_connection(
                    "google",
                    {
                        "access_token": "oauth-access-token",
                        "refresh_token": "oauth-refresh-token",
                        "expires_in": 3600,
                        "scope": "openid email profile https://www.googleapis.com/auth/generative-language",
                    },
                    userinfo={"email": "user@example.test", "name": "OAuth User"},
                    pack_root=pack_root,
                )
                providers = {item["provider_id"]: item for item in get_provider_catalog()}

        google = providers["google"]
        self.assertTrue(google["availability"]["configured"])
        self.assertEqual(google["availability"]["configuration_source"], "browser_oauth")


class TestDefaultspackToolPermissionPolicy(unittest.TestCase):
    def test_permission_policy_round_trip_and_checker_behavior(self):
        from ecosystem.defaultspack.backend.tool.permission_policy import (
            ToolPermissionPolicyStore,
        )
        from ecosystem.defaultspack.domain.tool.permission_checker import (
            PermissionChecker,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tool_permission_policy.json"
            manager = ToolPermissionPolicyStore(path=path)
            stored = manager.update({"tools": {"dangerous_tool": "deny"}})
            self.assertEqual(stored["tools"]["dangerous_tool"], "deny")

            with patch(
                "ecosystem.defaultspack.domain.tool.permission_checker.get_tool_permission_policy_manager",
                return_value=manager,
            ):
                checker = PermissionChecker()
                self.assertFalse(checker.check("dangerous_tool", {}))

            manager.update({"tools": {"ask_tool": "ask"}})
            with patch(
                "ecosystem.defaultspack.domain.tool.permission_checker.get_tool_permission_policy_manager",
                return_value=manager,
            ):
                checker = PermissionChecker()
                self.assertFalse(checker.check("ask_tool", {}))
                decision = checker.decide("ask_tool", {})
                self.assertTrue(decision["requires_approval"])


class TestDefaultspackHttpRegistryContract(unittest.TestCase):
    def test_registry_requests_include_method_marker(self):
        transport_path = (
            Path(__file__).resolve().parent.parent
            / "ecosystem"
            / "defaultspack"
            / "transport"
            / "http.py"
        )
        source = transport_path.read_text(encoding="utf-8")
        self.assertIn('request_data["_method"] = method', source)
        self.assertIn('request_data["_actual_method"] = method', source)


if __name__ == "__main__":
    unittest.main()
