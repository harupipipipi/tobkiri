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


class TestDefaultspackCustomProviderRegistry(unittest.TestCase):
    def test_provider_key_status_never_falls_back_to_legacy_default_authority(self):
        from domain.ai_client import api_key_store

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.dict(
                    os.environ,
                    {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir},
                    clear=True,
                ),
                patch.object(
                    api_key_store,
                    "_provider_authority_status",
                    return_value={},
                ),
                patch.object(
                    api_key_store,
                    "provider_has_api_key",
                    side_effect=AssertionError("legacy authority consulted"),
                ),
            ):
                rows = api_key_store.provider_key_status()

        opencode = next(row for row in rows if row["provider_id"] == "opencode-zen")
        self.assertFalse(opencode["default_api_key_configured"])
        self.assertEqual(opencode["credential_presence"], "missing")

    def test_provider_key_status_projects_v4_default_credential_without_secret_data(self):
        from domain.ai_client import api_key_store

        authority = {
            "opencode-zen": {
                "provider_id": "opencode-zen",
                "status": "unknown",
                "runtime": {
                    "verified": False,
                    "observed_at": None,
                },
                "credential": {
                    "configured": True,
                    "source": "provider_default",
                    "masked": True,
                    "scopes": ["ai.generate"],
                    "opaque_id": "credential-status:fixture",
                    "reason_code": "not_verified",
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.dict(
                    os.environ,
                    {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir},
                    clear=True,
                ),
                patch.object(
                    api_key_store,
                    "_provider_authority_status",
                    return_value=authority,
                ),
            ):
                rows = api_key_store.provider_key_status()

        opencode = next(row for row in rows if row["provider_id"] == "opencode-zen")
        self.assertTrue(opencode["configured"])
        self.assertTrue(opencode["default_api_key_configured"])
        self.assertEqual(opencode["credential_presence"], "present")
        self.assertEqual(opencode["credential_source"], "provider_default")
        self.assertTrue(opencode["credential_readonly"])
        self.assertEqual(
            opencode["credential_opaque_id"],
            "credential-status:fixture",
        )
        self.assertEqual(opencode["credential_usability"], "present_unverified")
        self.assertEqual(opencode["credential_health"]["status"], "present_unverified")
        self.assertEqual(opencode["credential_health"]["granted_scopes"], ["ai.generate"])
        rendered = repr(opencode)
        self.assertNotIn("OPENCODE_ZEN_API_KEY", rendered)
        self.assertNotIn("credential:opaque", rendered)

    def test_provider_key_status_keeps_verified_health_separate_from_presence(self):
        from domain.ai_client.api_key_store import _provider_status_row

        base = {
            "provider_id": "opencode-zen",
            "apis": [],
        }
        unavailable = _provider_status_row(
            provider_id="opencode-zen",
            authority={
                "status": "unavailable",
                "runtime": {"verified": True, "observed_at": 123.0},
                "credential": {"configured": True, "scopes": []},
            },
            default_api_key_configured=False,
            oauth_configured=False,
            row=base,
        )
        usable = _provider_status_row(
            provider_id="opencode-zen",
            authority={
                "status": "available",
                "runtime": {"verified": True, "observed_at": 124.0},
                "credential": {"configured": True, "scopes": []},
            },
            default_api_key_configured=False,
            oauth_configured=False,
            row=base,
        )

        self.assertEqual(unavailable["credential_presence"], "present")
        self.assertEqual(unavailable["credential_usability"], "unavailable")
        self.assertEqual(usable["credential_presence"], "present")
        self.assertEqual(usable["credential_usability"], "verified_usable")

    def test_named_key_for_unknown_provider_auto_registers_custom_provider(self):
        from domain.ai_client.api_key_store import (
            list_custom_providers,
            provider_key_status,
            set_provider_api_key,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir}, clear=True):
                result = set_provider_api_key(
                    "tavily",
                    "test-secret",
                    api_id="main",
                    name="main",
                    kind="custom",
                )
                self.assertTrue(result["success"], result)
                self.assertEqual(result["kind"], "custom")

                providers = list_custom_providers()
                provider_ids = [item["provider_id"] for item in providers]
                self.assertIn("tavily", provider_ids)
                tavily = next(item for item in providers if item["provider_id"] == "tavily")
                self.assertEqual(tavily["kind"], "custom")

                status = provider_key_status()
                tavily_status = next(row for row in status if row["provider_id"] == "tavily")
                self.assertFalse(tavily_status["builtin"])
                self.assertEqual(tavily_status["kind"], "custom")
                self.assertTrue(tavily_status["configured"])
                self.assertEqual(len(tavily_status["apis"]), 1)
                self.assertEqual(tavily_status["apis"][0]["kind"], "custom")

    def test_register_custom_provider_creates_entry_without_keys(self):
        from domain.ai_client.api_key_store import (
            delete_custom_provider,
            list_custom_providers,
            provider_key_status,
            register_custom_provider,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir}, clear=True):
                result = register_custom_provider("searchapi", label="SearchAPI", kind="custom")
                self.assertTrue(result["success"])
                self.assertEqual(result["kind"], "custom")
                self.assertEqual(
                    [item["provider_id"] for item in list_custom_providers()],
                    ["searchapi"],
                )

                status = provider_key_status()
                row = next(row for row in status if row["provider_id"] == "searchapi")
                self.assertFalse(row["configured"])
                self.assertEqual(row["kind"], "custom")
                self.assertEqual(row["apis"], [])

                deleted = delete_custom_provider("searchapi")
                self.assertTrue(deleted["success"])
                self.assertEqual(list_custom_providers(), [])

    def test_register_custom_provider_rejects_blank_id(self):
        from domain.ai_client.api_key_store import register_custom_provider

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir}, clear=True):
                result = register_custom_provider("   ")
                self.assertFalse(result["success"])

    def test_provider_key_status_includes_kind_and_builtin_for_known_providers(self):
        from domain.ai_client.api_key_store import provider_key_status

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": tmpdir}, clear=True):
                rows = provider_key_status()
        google = next(row for row in rows if row["provider_id"] == "google")
        self.assertTrue(google["builtin"])
        self.assertEqual(google["kind"], "llm")


if __name__ == "__main__":
    unittest.main()
