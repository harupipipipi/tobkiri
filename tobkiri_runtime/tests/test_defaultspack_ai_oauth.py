from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class TestDefaultspackAiOauth(unittest.TestCase):
    @staticmethod
    def _google_client_json() -> str:
        return """
        {
          "installed": {
            "client_id": "test-client.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "redirect_uris": ["http://127.0.0.1"]
          }
        }
        """

    def test_google_oauth_status_tracks_client_and_connection(self):
        from domain.ai_client.oauth_store import (
            disconnect_provider_oauth,
            get_provider_access_token,
            provider_oauth_status,
            save_provider_oauth_client_config,
            save_provider_oauth_connection,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                saved_client = save_provider_oauth_client_config(
                    "google",
                    self._google_client_json(),
                    pack_root=pack_root,
                )
                status = provider_oauth_status("google", pack_root=pack_root)
                saved_connection = save_provider_oauth_connection(
                    "google",
                    {
                        "access_token": "oauth-access-token",
                        "refresh_token": "oauth-refresh-token",
                        "expires_in": 3600,
                        "scope": "openid email profile https://www.googleapis.com/auth/generative-language",
                        "token_type": "Bearer",
                    },
                    userinfo={"email": "user@example.test", "name": "OAuth User"},
                    pack_root=pack_root,
                )
                connected_status = provider_oauth_status("google", pack_root=pack_root)
                access_token = get_provider_access_token("google", pack_root=pack_root)
                disconnect_provider_oauth("google", pack_root=pack_root)
                disconnected_status = provider_oauth_status("google", pack_root=pack_root)

        self.assertTrue(saved_client["success"])
        self.assertTrue(status["client_configured"])
        self.assertFalse(status["connected"])
        self.assertTrue(saved_connection["success"])
        self.assertTrue(connected_status["connected"])
        self.assertEqual(connected_status["email"], "user@example.test")
        self.assertEqual(access_token, "oauth-access-token")
        self.assertFalse(disconnected_status["connected"])

    def test_google_oauth_start_builds_loopback_callback(self):
        from domain.ai_client.oauth_store import (
            save_provider_oauth_client_config,
            start_provider_oauth,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                save_provider_oauth_client_config("google", self._google_client_json(), pack_root=pack_root)
                started = start_provider_oauth(
                    "google",
                    request_headers={"Host": "127.0.0.1:8766"},
                    pack_root=pack_root,
                )

        self.assertTrue(started["success"])
        self.assertIn("accounts.google.com", started["authorize_url"])
        self.assertEqual(started["redirect_uri"], "http://127.0.0.1:8766/api/ai/oauth/google/callback")
        self.assertIn("state=", started["authorize_url"])

    def test_google_workspace_scope_mode_matches_drive_gmail_manifest_scopes(self):
        from domain.ai_client.oauth_store import (
            save_provider_oauth_client_config,
            start_provider_oauth,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                save_provider_oauth_client_config("google", self._google_client_json(), pack_root=pack_root)
                started = start_provider_oauth(
                    "google",
                    request_headers={"Host": "127.0.0.1:8766"},
                    scope_mode="google_workspace",
                    pack_root=pack_root,
                )

        self.assertTrue(started["success"], started)
        self.assertEqual(started["scope_mode"], "google_workspace")
        self.assertIn("https://www.googleapis.com/auth/drive.file", started["scopes"])
        self.assertIn("https://www.googleapis.com/auth/gmail.labels", started["scopes"])
        self.assertNotIn("https://www.googleapis.com/auth/generative-language", started["scopes"])
        self.assertIn(
            "scope=openid+email+profile+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.file+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.labels",
            started["authorize_url"],
        )

    def test_google_services_can_select_restricted_gmail_scope_mode(self):
        from domain.ai_client.oauth_store import (
            provider_oauth_status,
            save_provider_oauth_client_config,
            start_provider_oauth,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                save_provider_oauth_client_config("google", self._google_client_json(), pack_root=pack_root)
                started = start_provider_oauth(
                    "google",
                    request_headers={"Host": "127.0.0.1:8766"},
                    services=["identity", "gmail_metadata"],
                    pack_root=pack_root,
                )
                status = provider_oauth_status("google", pack_root=pack_root)

        self.assertTrue(started["success"], started)
        self.assertEqual(started["scope_mode"], "google_gmail_metadata")
        self.assertEqual(started["services"], ["identity", "gmail_metadata"])
        self.assertIn("https://www.googleapis.com/auth/gmail.metadata", started["scopes"])
        self.assertNotIn("https://www.googleapis.com/auth/gmail.readonly", started["scopes"])
        restricted_modes = {
            item["id"]: item
            for item in status["scope_modes"]
            if item.get("restricted")
        }
        self.assertIn("google_gmail_metadata", restricted_modes)
        self.assertIn("google_gmail_readonly", restricted_modes)
        self.assertIn("Restricted Gmail scopes", restricted_modes["google_gmail_metadata"]["warning"])

    def test_cloudflare_oauth_waits_for_self_host_scopes(self):
        from domain.ai_client.oauth_store import provider_oauth_status, start_provider_oauth

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            status = provider_oauth_status("cloudflare", pack_root=pack_root)
            started = start_provider_oauth(
                "cloudflare",
                request_headers={"Host": "127.0.0.1:8766"},
                pack_root=pack_root,
            )

        self.assertTrue(status["supported"])
        self.assertTrue(status["backend_supported"])
        self.assertFalse(status["connect_enabled"])
        self.assertEqual(status["connection_status"], "missing_scope_config")
        self.assertEqual(status["disabled_reason"], "Configure self-host OAuth")
        self.assertFalse(started["success"])
        self.assertEqual(started["error"], "oauth client config is not saved")

    def test_cloudflare_oauth_start_uses_saved_client_and_manifest_endpoint(self):
        from domain.ai_client.oauth_store import (
            provider_oauth_status,
            save_provider_oauth_client_config,
            start_provider_oauth,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            with patch.dict(
                os.environ,
                {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)},
                clear=True,
            ):
                saved = save_provider_oauth_client_config(
                    "cloudflare",
                    json.dumps(
                        {
                            "client_id": "cloudflare-client-id",
                            "client_secret": "cloudflare-client-secret",
                            "scopes": ["account:read", "user:read"],
                            "redirect_uris": [
                                "http://127.0.0.1:8766/api/ai/oauth/cloudflare/callback"
                            ],
                        }
                    ),
                    pack_root=pack_root,
                )
                status = provider_oauth_status("cloudflare", pack_root=pack_root)
                started = start_provider_oauth(
                    "cloudflare",
                    request_headers={"Host": "127.0.0.1:8766"},
                    pack_root=pack_root,
                )

        self.assertTrue(saved["success"], saved)
        self.assertTrue(status["supported"])
        self.assertTrue(status["backend_supported"])
        self.assertTrue(status["client_configured"])
        self.assertEqual(status["client_source"], "secret_store")
        self.assertTrue(status["client_can_clear"])
        self.assertTrue(status["connect_enabled"])
        self.assertEqual(status["connection_status"], "not_connected")
        self.assertTrue(started["success"], started)
        parsed = urllib.parse.urlparse(started["authorize_url"])
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://dash.cloudflare.com/oauth2/auth")
        self.assertEqual(params["client_id"], ["cloudflare-client-id"])
        self.assertEqual(params["scope"], ["account:read user:read"])
        self.assertEqual(params["redirect_uri"], ["http://127.0.0.1:8766/api/ai/oauth/cloudflare/callback"])
        self.assertIn("code_challenge", params)
        self.assertNotIn("include_granted_scopes", params)
        self.assertNotIn("access_type", params)

    def test_cloudflare_saved_access_token_counts_as_connected(self):
        from domain.ai_client.oauth_store import (
            get_provider_access_token,
            provider_oauth_status,
            save_provider_oauth_connection,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            with patch.dict(
                os.environ,
                {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)},
                clear=True,
            ):
                saved = save_provider_oauth_connection(
                    "cloudflare",
                    {
                        "access_token": "cloudflare-oauth-access",
                        "scope": "account:read",
                    },
                    pack_root=pack_root,
                )
                status = provider_oauth_status("cloudflare", pack_root=pack_root)
                access_token = get_provider_access_token("cloudflare", pack_root=pack_root)

        self.assertTrue(saved["success"], saved)
        self.assertTrue(status["supported"])
        self.assertTrue(status["connected"])
        self.assertEqual(status["connection_status"], "connected")
        self.assertEqual(status["capabilities"], ["cloudflare.account.read"])
        self.assertEqual(access_token, "cloudflare-oauth-access")

    def test_cloudflare_saved_access_token_can_request_pages_capabilities(self):
        from domain.ai_client.oauth_store import (
            provider_oauth_status,
            save_provider_oauth_connection,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            with patch.dict(
                os.environ,
                {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)},
                clear=True,
            ):
                saved = save_provider_oauth_connection(
                    "cloudflare",
                    {
                        "access_token": "cloudflare-oauth-access",
                        "scope": "account:read pages:write",
                        "requested_capabilities": [
                            "cloudflare.account.read",
                            "cloudflare.pages.project.write",
                            "cloudflare.pages.deployment.write",
                            "cloudflare.runner.deploy",
                        ],
                    },
                    pack_root=pack_root,
                )
                status = provider_oauth_status("cloudflare", pack_root=pack_root)

        self.assertTrue(saved["success"], saved)
        self.assertTrue(status["connected"])
        self.assertEqual(
            status["capabilities"],
            [
                "cloudflare.account.read",
            ],
        )
        self.assertEqual(
            status["approval_required_capabilities"],
            [
                "cloudflare.pages.deployment.write",
                "cloudflare.pages.project.write",
            ],
        )
        self.assertEqual(status["rejected_capabilities"], ["cloudflare.runner.deploy"])
        self.assertNotIn("cloudflare-oauth-access", json.dumps(status, ensure_ascii=False))

    def test_cloudflare_oauth_finish_uses_cloudflare_token_and_userinfo_endpoints(self):
        from domain.ai_client.oauth_store import (
            finish_provider_oauth,
            save_provider_oauth_client_config,
            start_provider_oauth,
        )

        captured: dict[str, str] = {}

        def fake_post(url: str, data: dict[str, str], *, timeout: float = 30.0) -> dict[str, object]:
            del timeout
            captured["post_url"] = url
            captured["client_secret"] = data.get("client_secret", "")
            return {
                "access_token": "cloudflare-access-token",
                "refresh_token": "cloudflare-refresh-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            }

        def fake_get(url: str, access_token: str, *, timeout: float = 30.0) -> dict[str, object]:
            del timeout
            captured["get_url"] = url
            captured["access_token"] = access_token
            return {"email": "cloudflare-user@example.test", "name": "Cloudflare User"}

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}, clear=True):
                saved = save_provider_oauth_client_config(
                    "cloudflare",
                    json.dumps(
                        {
                            "client_id": "cloudflare-client-id",
                            "client_secret": "cloudflare-client-secret",
                            "scopes": ["account:read"],
                        }
                    ),
                    pack_root=pack_root,
                )
                started = start_provider_oauth(
                    "cloudflare",
                    request_headers={"Host": "127.0.0.1:8766"},
                    pack_root=pack_root,
                )
                with patch("domain.ai_client.oauth_store._http_post_form", side_effect=fake_post), patch(
                    "domain.ai_client.oauth_store._http_get_json",
                    side_effect=fake_get,
                ):
                    result = finish_provider_oauth(
                        "cloudflare",
                        {"code": "oauth-code", "state": started["state"]},
                        pack_root=pack_root,
                    )

        self.assertTrue(saved["success"], saved)
        self.assertTrue(result["success"], result)
        self.assertEqual(captured["post_url"], "https://dash.cloudflare.com/oauth2/token")
        self.assertEqual(captured["get_url"], "https://dash.cloudflare.com/oauth2/userinfo")
        self.assertEqual(captured["client_secret"], "cloudflare-client-secret")
        self.assertEqual(captured["access_token"], "cloudflare-access-token")

    def test_connection_import_resolves_capabilities_from_manifest_not_json_claims(self):
        from domain.ai_client.oauth_store import get_provider_access_token, provider_oauth_status
        from domain.connections.store import import_connection_bundle

        raw_token = "github-imported-secret-token"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                imported = import_connection_bundle(
                    {
                        "schema": "rumi.connection.credential_bundle.v1",
                        "provider_id": "github",
                        "connection_id": "default",
                        "material_type": "oauth2_token",
                        "credentials": {"access_token": raw_token},
                        "scopes": ["read:user"],
                        "token_metadata": {
                            "account_label": "GitHub User",
                            "capabilities": ["github.repo.write"],
                        },
                    },
                    pack_root=pack_root,
                )
                status = provider_oauth_status("github", pack_root=pack_root)
                access_token = get_provider_access_token("github", pack_root=pack_root)

        self.assertTrue(imported["success"], imported)
        self.assertEqual(imported["capabilities"], ["github.user.read"])
        self.assertEqual(imported["approval_required_capabilities"], [])
        self.assertIn("github.repo.write", imported["rejected_capabilities"])
        self.assertNotIn("github.repo.write", imported["capabilities"])
        self.assertNotIn(raw_token, str(imported))
        self.assertTrue(status["connected"])
        self.assertEqual(status["credential_ref"]["provider_id"], "github")
        self.assertEqual(status["capabilities"], ["github.user.read"])
        self.assertEqual(access_token, raw_token)

    def test_connection_import_broad_github_repo_scope_does_not_grant_write_when_read_only_requested(self):
        from domain.connections.store import import_connection_bundle

        raw_token = "github-read-only-secret-token"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                imported = import_connection_bundle(
                    {
                        "schema": "rumi.connection.credential_bundle.v1",
                        "provider_id": "github",
                        "material_type": "oauth2_token",
                        "credentials": {"access_token": raw_token},
                        "scopes": ["repo"],
                        "requested_capabilities": ["github.repo.read"],
                        "token_metadata": {"capabilities": ["unknown.capability"]},
                    },
                    pack_root=pack_root,
                )

        self.assertTrue(imported["success"], imported)
        self.assertEqual(imported["capabilities"], ["github.repo.read"])
        self.assertNotIn("github.repo.write", imported["capabilities"])
        self.assertEqual(imported["approval_required_capabilities"], [])
        self.assertIn("unknown.capability", imported["rejected_capabilities"])
        self.assertNotIn(raw_token, str(imported))

    def test_connection_import_high_risk_capability_requires_approval(self):
        from domain.connections.store import import_connection_bundle

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                imported = import_connection_bundle(
                    {
                        "schema": "rumi.connection.credential_bundle.v1",
                        "provider_id": "github",
                        "material_type": "oauth2_token",
                        "credentials": {"access_token": "github-write-secret-token"},
                        "scopes": ["repo"],
                        "requested_capabilities": ["github.repo.write"],
                    },
                    pack_root=pack_root,
                )

        self.assertTrue(imported["success"], imported)
        self.assertEqual(imported["capabilities"], [])
        self.assertEqual(imported["approval_required_capabilities"], ["github.repo.write"])
        self.assertNotIn("github.repo.write", imported["rejected_capabilities"])

    def test_connection_import_cloudflare_pages_write_requires_approval_without_runner_deploy(self):
        from domain.connections.store import import_connection_bundle

        raw_token = "cloudflare-pages-secret-token"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                imported = import_connection_bundle(
                    {
                        "schema": "rumi.connection.credential_bundle.v1",
                        "provider_id": "cloudflare",
                        "material_type": "oauth2_token",
                        "credentials": {"access_token": raw_token},
                        "scopes": ["account:read", "pages:write"],
                        "requested_capabilities": [
                            "cloudflare.account.read",
                            "cloudflare.pages.project.write",
                            "cloudflare.pages.deployment.write",
                            "cloudflare.runner.deploy",
                        ],
                    },
                    pack_root=pack_root,
                )

        self.assertTrue(imported["success"], imported)
        self.assertEqual(
            imported["capabilities"],
            [
                "cloudflare.account.read",
            ],
        )
        self.assertEqual(
            imported["approval_required_capabilities"],
            [
                "cloudflare.pages.deployment.write",
                "cloudflare.pages.project.write",
            ],
        )
        self.assertEqual(imported["rejected_capabilities"], ["cloudflare.runner.deploy"])
        self.assertNotIn(raw_token, json.dumps(imported, ensure_ascii=False))

    def test_connection_registry_reuses_and_invalidates_manifest_cache(self):
        from core_runtime.connections.registry import ConnectionsRegistry
        from domain.ai_client import oauth_store

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            providers_dir = pack_root / "config" / "settings_control_center" / "providers"
            providers_dir.mkdir(parents=True)
            manifest_path = providers_dir / "google.connection.json"
            source_manifest = (
                DEFAULTSPACK_ROOT
                / "config"
                / "settings_control_center"
                / "providers"
                / "google.connection.json"
            )
            manifest_path.write_text(
                source_manifest.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            load_calls = 0
            original_load_manifest_dir = ConnectionsRegistry.load_manifest_dir

            def counted_load_manifest_dir(registry, root):
                nonlocal load_calls
                load_calls += 1
                return original_load_manifest_dir(registry, root)

            with patch.object(
                ConnectionsRegistry,
                "load_manifest_dir",
                counted_load_manifest_dir,
            ):
                first = oauth_store._connection_registry(pack_root)
                second = oauth_store._connection_registry(pack_root)
                manifest_path.write_text(
                    manifest_path.read_text(encoding="utf-8") + "\n",
                    encoding="utf-8",
                )
                changed = oauth_store._connection_registry(pack_root)
                manifest_path.unlink()
                deleted = oauth_store._connection_registry(pack_root)

        self.assertIs(first, second)
        self.assertIsNot(first, changed)
        self.assertIsNot(changed, deleted)
        self.assertEqual(load_calls, 2)
        with self.assertRaises(KeyError):
            deleted.get("google")

    def test_connection_provider_manifest_extensibility_with_dummy_provider(self):
        from domain.ai_client.oauth_store import provider_oauth_statuses
        from domain.connections.store import import_connection_bundle, resolve_capabilities_for_provider
        from domain.frontend.registry import FrontendRegistry

        manifest = {
            "schema": "rumi.connection.provider.v1",
            "provider_id": "dummy",
            "display_name": {"en": "Dummy Provider", "ja": "Dummy Provider"},
            "description": {"en": "Test provider for Settings Control Center extensibility."},
            "icon": "plug",
            "service_kind": "custom",
            "auth": {
                "type": "api_key",
                "template": "credential_bundle",
                "token_import_supported": True,
                "official_broker_supported": False,
                "self_host_client_supported": True,
            },
            "capabilities": [
                {
                    "id": "dummy.read",
                    "displayName": {"en": "Read dummy data"},
                    "description": {"en": "Read dummy data."},
                    "risk": "low",
                },
                {
                    "id": "dummy.write",
                    "displayName": {"en": "Write dummy data"},
                    "description": {"en": "Write dummy data."},
                    "risk": "high",
                },
            ],
            "scope_to_capability": [
                {
                    "credential_kind": "access_token",
                    "capabilities": ["dummy.read", "dummy.write"],
                }
            ],
            "settings": {"section": "accounts_connections", "priority": 70, "recommended": False},
        }
        raw_token = "dummy-secret-token"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            providers_dir = pack_root / "config" / "settings_control_center" / "providers"
            providers_dir.mkdir(parents=True)
            manifest_path = providers_dir / "dummy.connection.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True), patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                statuses = provider_oauth_statuses(pack_root=pack_root)
                settings = FrontendRegistry(pack_root=pack_root).get_settings()["values"]
                imported_read = import_connection_bundle(
                    {
                        "schema": "rumi.connection.credential_bundle.v1",
                        "provider_id": "dummy",
                        "connection_id": "dummy:test",
                        "account_label": "Dummy test",
                        "material_type": "access_token",
                        "credentials": {"access_token": raw_token},
                        "requested_capabilities": ["dummy.read"],
                    },
                    pack_root=pack_root,
                )
                imported_high = import_connection_bundle(
                    {
                        "schema": "rumi.connection.credential_bundle.v1",
                        "provider_id": "dummy",
                        "connection_id": "dummy:write",
                        "material_type": "access_token",
                        "credentials": {"access_token": "dummy-write-secret"},
                        "requested_capabilities": ["dummy.write"],
                    },
                    pack_root=pack_root,
                )
                imported_unknown = import_connection_bundle(
                    {
                        "schema": "rumi.connection.credential_bundle.v1",
                        "provider_id": "dummy",
                        "connection_id": "dummy:unknown",
                        "material_type": "access_token",
                        "credentials": {"access_token": "dummy-unknown-secret"},
                        "requested_capabilities": ["dummy.admin.everything"],
                    },
                    pack_root=pack_root,
                )
                manifest_path.unlink()
                statuses_after_delete = provider_oauth_statuses(pack_root=pack_root)
                settings_after_delete = FrontendRegistry(pack_root=pack_root).get_settings()["values"]
                resolved_after_delete = resolve_capabilities_for_provider(
                    "dummy",
                    {
                        "credential_kind": "access_token",
                        "requested_capabilities": ["dummy.read"],
                    },
                    pack_root=pack_root,
                )

        self.assertIn("dummy", statuses)
        self.assertEqual(settings["accounts_connections"]["providers"]["dummy"]["display_name"], "Dummy Provider")
        self.assertTrue(imported_read["success"], imported_read)
        self.assertEqual(imported_read["capabilities"], ["dummy.read"])
        self.assertEqual(imported_read["approval_required_capabilities"], [])
        self.assertEqual(imported_read["rejected_capabilities"], [])
        self.assertIn("credential_id", imported_read["credential_ref"])
        self.assertEqual(imported_high["capabilities"], [])
        self.assertEqual(imported_high["approval_required_capabilities"], ["dummy.write"])
        self.assertEqual(imported_unknown["capabilities"], [])
        self.assertEqual(imported_unknown["approval_required_capabilities"], [])
        self.assertEqual(imported_unknown["rejected_capabilities"], ["dummy.admin.everything"])
        self.assertNotIn(raw_token, json.dumps([imported_read, imported_high, imported_unknown], ensure_ascii=False))
        self.assertNotIn(raw_token, json.dumps(settings, ensure_ascii=False))
        self.assertNotIn("dummy", statuses_after_delete)
        self.assertNotIn("dummy", settings_after_delete["accounts_connections"]["providers"])
        self.assertEqual(resolved_after_delete["capabilities"], [])

    def test_credential_bundle_safe_metadata_drops_secret_fields(self):
        from core_runtime.connections.templates import CredentialBundle

        metadata = CredentialBundle.from_dict(
            {
                "schema": "rumi.connection.credential_bundle.v1",
                "provider_id": "cloudflare",
                "credentials": {"access_token": "stored-secret"},
                "token_metadata": {
                    "access_token": "metadata-access-secret",
                    "refresh_token": "metadata-refresh-secret",
                    "client_secret": "metadata-client-secret",
                    "ws_token": "metadata-ws-secret",
                    "account_label": "Cloudflare User",
                    "capabilities": ["cloudflare.runner.deploy"],
                },
            }
        ).safe_metadata()

        token_metadata = metadata["token_metadata"]
        self.assertEqual(token_metadata["account_label"], "Cloudflare User")
        self.assertNotIn("access_token", token_metadata)
        self.assertNotIn("refresh_token", token_metadata)
        self.assertNotIn("client_secret", token_metadata)
        self.assertNotIn("ws_token", token_metadata)
        self.assertNotIn("capabilities", token_metadata)

    def test_connection_import_route_accepts_env_token_without_returning_token(self):
        from blocks.connections import import_bundle as import_block

        raw_token = "cloudflare-route-secret-token"
        with patch.object(
            import_block,
            "import_connection_bundle",
            return_value={
                "success": True,
                "provider_id": "cloudflare",
                "connection_id": "default",
                "access_token": raw_token,
                "credential_ref": {"provider_id": "cloudflare"},
                "scopes": [],
                "capabilities": ["cloudflare.account.read"],
                "approval_required_capabilities": [],
                "rejected_capabilities": [],
                "status": "connected",
            },
        ) as imported:
            result = import_block.run(
                {
                    "_method": "POST",
                    "provider_id": "cloudflare",
                    "credential_bundle": f"CLOUDFLARE_API_TOKEN={raw_token}",
                },
                {},
            )

        imported.assert_called_once()
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(raw_token, json.dumps(result, ensure_ascii=False))

    def test_connection_import_from_env_token_keeps_settings_json_secret_free(self):
        from domain.connections.store import import_connection_bundle
        from domain.frontend.registry import FrontendRegistry

        raw_token = "cloudflare-settings-secret-token"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True), patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                imported = import_connection_bundle(
                    f"CLOUDFLARE_API_TOKEN={raw_token}\nCLOUDFLARE_ACCOUNT_ID=account-id",
                    provider_id="cloudflare",
                    pack_root=pack_root,
                )
                settings = FrontendRegistry(pack_root=pack_root).get_settings()

        self.assertTrue(imported["success"], imported)
        self.assertEqual(imported["capabilities"], ["cloudflare.account.read"])
        self.assertNotIn(raw_token, json.dumps(settings, ensure_ascii=False))

    def test_oauth_connection_persists_token_bundle_under_connection_credential_ref(self):
        from domain.ai_client.oauth_store import provider_oauth_status, save_provider_oauth_connection
        from domain.connections.store import connection_secret_key

        raw_token = "google-oauth-access-bundle"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                saved = save_provider_oauth_connection(
                    "google",
                    {
                        "access_token": raw_token,
                        "refresh_token": "google-refresh-bundle",
                        "expires_in": 3600,
                        "scope": "openid email profile https://www.googleapis.com/auth/drive.file",
                    },
                    userinfo={"email": "drive@example.test"},
                    pack_root=pack_root,
                )
                status = provider_oauth_status("google", pack_root=pack_root)

        expected_key = connection_secret_key("google", "default", "oauth2_token")
        self.assertTrue(saved["success"], saved)
        self.assertEqual(saved["credential_ref"]["credential_id"], expected_key)
        self.assertEqual(status["credential_ref"]["credential_id"], expected_key)
        self.assertIn("google.drive.file", status["capabilities"])
        self.assertFalse((secrets_dir / "RUMIOAUTH_GOOGLE_ACCESS_TOKEN.json").exists())
        self.assertNotIn(raw_token, str(saved))
        self.assertNotIn(raw_token, str(status))

    def test_finish_provider_oauth_exchanges_code_and_persists_connection(self):
        from domain.ai_client.oauth_store import (
            finish_provider_oauth,
            provider_oauth_status,
            save_provider_oauth_client_config,
            start_provider_oauth,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                save_provider_oauth_client_config("google", self._google_client_json(), pack_root=pack_root)
                started = start_provider_oauth(
                    "google",
                    request_headers={"Host": "127.0.0.1:8766"},
                    pack_root=pack_root,
                )
                with patch(
                    "domain.ai_client.oauth_store._exchange_code_for_tokens",
                    return_value={
                        "access_token": "oauth-access-token",
                        "refresh_token": "oauth-refresh-token",
                        "expires_in": 3600,
                        "scope": "openid email profile https://www.googleapis.com/auth/generative-language",
                        "token_type": "Bearer",
                    },
                ), patch(
                    "domain.ai_client.oauth_store._fetch_userinfo",
                    return_value={"email": "user@example.test", "name": "OAuth User"},
                ):
                    result = finish_provider_oauth(
                        "google",
                        {"code": "oauth-code", "state": started["state"]},
                        pack_root=pack_root,
                    )
                status = provider_oauth_status("google", pack_root=pack_root)

        self.assertTrue(result["success"])
        self.assertTrue(status["connected"])
        self.assertEqual(status["display_name"], "OAuth User")

    def test_oauth_block_callback_returns_static_postmessage_page(self):
        from blocks.ai import oauth as oauth_block

        with patch.object(
            oauth_block,
            "finish_provider_oauth",
            return_value={
                "success": True,
                "provider_id": "google",
                "email": "user@example.test",
                "display_name": "OAuth User",
            },
        ):
            result = oauth_block.run(
                {
                    "_method": "GET",
                    "provider_id": "google",
                    "code": "oauth-code",
                    "state": "oauth-state",
                },
                {},
            )

        self.assertTrue(result["_static"])
        self.assertIn("window.opener.postMessage", str(result["body"]))
        self.assertIn("Browser login connected", str(result["body"]))


if __name__ == "__main__":
    unittest.main()
