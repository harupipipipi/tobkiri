import stat

from core_runtime.connections.credential_store import LocalEncryptedCredentialStore
from core_runtime.connections.registry import (
    ConnectionsRegistry,
    discover_connection_manifests,
)
from core_runtime.connections.oauth_service import InMemoryOAuthStateStore
from core_runtime.connections.permission_resolver import resolve_connection_permissions
from ecosystem.rumi_provider_registry_pack.runtime.connection_catalog import (
    CLOUDFLARE_PROVIDER,
    CODEX_PROVIDER,
    GITHUB_PROVIDER,
    GOOGLE_PROVIDER,
)
from core_runtime.connections.templates import CredentialBundle
from ecosystem.rumi_provider_registry_pack.runtime.connection_adapters import (
    CloudflareConnectionAdapter,
)


def test_connections_registry_orders_providers():
    registry = ConnectionsRegistry()
    registry.register(GOOGLE_PROVIDER)
    registry.register(CLOUDFLARE_PROVIDER)
    registry.register(GITHUB_PROVIDER)
    registry.register(CODEX_PROVIDER)
    providers = registry.list_providers()
    assert [provider["providerId"] for provider in providers][:4] == ["cloudflare", "google", "github", "codex"]


def test_legacy_core_provider_catalog_is_pack_independent():
    """Host compatibility imports expose provider identity without Pack code."""

    from core_runtime.cloudflare import cloudflare_environment_status
    from core_runtime.connections.providers import (
        CLOUDFLARE_PROVIDER as legacy_cloudflare,
    )
    from core_runtime.connections.providers import CODEX_PROVIDER as legacy_codex
    from core_runtime.connections.providers import GITHUB_PROVIDER as legacy_github
    from core_runtime.connections.providers import GOOGLE_PROVIDER as legacy_google

    assert legacy_cloudflare.provider_id == CLOUDFLARE_PROVIDER.provider_id
    assert legacy_codex.provider_id == CODEX_PROVIDER.provider_id
    assert legacy_github.provider_id == GITHUB_PROVIDER.provider_id
    assert legacy_google.provider_id == GOOGLE_PROVIDER.provider_id
    assert all(
        not str(provider.adapter.get("python") or "").startswith("ecosystem.")
        for provider in (
            legacy_cloudflare,
            legacy_codex,
            legacy_github,
            legacy_google,
        )
    )
    assert cloudflare_environment_status()["schema"] == "rumi.cloudflare.environment.v1"


def test_connection_manifest_discovery_is_bounded_and_skips_symlinks(tmp_path):
    root = tmp_path / "providers"
    root.mkdir()
    direct = root / "direct.connection.json"
    direct.write_text("{}", encoding="utf-8")
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    included = nested / "nested.connection.json"
    included.write_text("{}", encoding="utf-8")
    too_deep = nested / "three" / "four"
    too_deep.mkdir(parents=True)
    (too_deep / "deep.connection.json").write_text("{}", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "escaped.connection.json").write_text("{}", encoding="utf-8")
    (root / "linked").symlink_to(external, target_is_directory=True)

    discovered = discover_connection_manifests(root, max_depth=2)

    assert discovered == (direct, included)


def test_provider_safe_payload_has_no_secret():
    payload = CLOUDFLARE_PROVIDER.to_dict()
    payload_text = str(payload).lower()
    assert "rumi_cloudflare_oauth_client_secret" not in payload_text
    assert "secret_value" not in payload_text
    assert "token_value" not in payload_text
    assert payload["officialBrokerSupported"] is True
    assert payload["selfHostClientSupported"] is True
    assert payload["pkceSupported"] is True
    assert payload["authTemplate"] == "generic_oauth2_pkce"
    assert payload["tokenImportSupported"] is True
    assert payload["scopeToCapability"][0]["capabilities"] == ["cloudflare.account.read"]
    assert payload["capabilities"][0]["displayName"] == "Read account metadata"
    assert payload["metadata"]["connector_resources"] == {
        "sandbox_bridge": "connector://cloudflare/sandbox_bridge",
        "pc_tunnel": "connector://cloudflare/pc_tunnel",
        "pc_tool_bridge": "connector://cloudflare/pc_tool_bridge",
    }
    assert "not uploaded to Cloudflare" in payload["metadata"]["pc_tool_bridge_note"]
    assert payload["metadata"]["tool_coverage_surface"] == "/api/tools/catalog"
    assert payload["metadata"]["all_tools_cloudflare_native_supported"] is False
    assert payload["metadata"]["pc_bridge_required_for_host_tools"] is True
    assert "pages.dev" in payload["metadata"]["stable_pc_tunnel_note"]
    assert "RUMI_CLOUDFLARE_PC_TUNNEL_HOSTNAME" in payload["metadata"]["self_host_env"]
    assert "RUMI_CLOUDFLARE_PC_TOOL_BRIDGE_URL" in payload["metadata"]["self_host_env"]
    assert "RUMI_PC_TOOL_BRIDGE_TOKEN" in payload["metadata"]["self_host_env"]


def test_cloudflare_pages_write_requires_approval():
    resolved = resolve_connection_permissions(
        CLOUDFLARE_PROVIDER,
        {
            "scopes": ["pages:write"],
            "requested_capabilities": [
                "cloudflare.pages.project.write",
                "cloudflare.pages.deployment.write",
            ],
        },
    )

    assert resolved.capabilities == []
    assert resolved.approval_required_capabilities == [
        "cloudflare.pages.deployment.write",
        "cloudflare.pages.project.write",
    ]
    assert resolved.rejected_capabilities == []


def test_cloudflare_pages_write_does_not_grant_runner_deploy():
    resolved = resolve_connection_permissions(
        CLOUDFLARE_PROVIDER,
        {
            "scopes": ["pages:write"],
            "requested_capabilities": ["cloudflare.runner.deploy"],
        },
    )

    assert "cloudflare.runner.deploy" not in resolved.capabilities
    assert "cloudflare.runner.deploy" not in resolved.approval_required_capabilities
    assert "cloudflare.runner.deploy" in resolved.rejected_capabilities


def test_cloudflare_full_runner_scope_requires_approval_for_runner_deploy():
    resolved = resolve_connection_permissions(
        CLOUDFLARE_PROVIDER,
        {
            "scopes": [
                "workers:write",
                "workers_scripts:edit",
                "pages:write",
                "d1:write",
                "r2:write",
                "queues:write",
                "workflows:write",
            ],
            "requested_capabilities": ["cloudflare.runner.deploy"],
        },
    )

    assert resolved.capabilities == []
    assert resolved.approval_required_capabilities == ["cloudflare.runner.deploy"]
    assert resolved.rejected_capabilities == []


def test_cloudflare_connection_adapter_normalizes_context_without_secret_leak(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "env-account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN_REQUESTED_CAPABILITIES", "cloudflare.worker.read,cloudflare.d1.read")
    bundle = CredentialBundle.from_dict(
        {
            "provider_id": "cloudflare",
            "connection_id": "default",
            "api_key": "cf-secret-token",
            "token_metadata": {
                "account_id": "metadata-account",
                "account_name": "Rumi Ops",
                "api_key": "cf-secret-token",
            },
        }
    )
    secret_material = bundle.secret_material()
    secret_material["context"] = {"account_id": "context-account", "zone_id": "context-zone"}

    metadata = CloudflareConnectionAdapter().normalize_token_metadata(
        provider=CLOUDFLARE_PROVIDER,
        credential_bundle=bundle,
        secret_material=secret_material,
    )

    assert metadata["provider_id"] == "cloudflare"
    assert metadata["account_id"] == "metadata-account"
    assert metadata["zone_id"] == "context-zone"
    assert metadata["account_id_configured"] is True
    assert metadata["zone_id_configured"] is True
    assert metadata["cloudflare_account_status"] == "configured"
    assert metadata["account_label"] == "Cloudflare: Rumi Ops"
    assert metadata["requested_capabilities"] == ["cloudflare.worker.read", "cloudflare.d1.read"]
    assert metadata["status"] == "connected"
    assert "cf-secret-token" not in str(metadata)


def test_cloudflare_connection_adapter_prefers_context_then_env(monkeypatch):
    monkeypatch.setenv("RUMI_CLOUDFLARE_ACCOUNT_ID", "env-account")
    monkeypatch.setenv("RUMI_CLOUDFLARE_ZONE_ID", "env-zone")
    bundle = CredentialBundle.from_dict(
        {
            "provider_id": "cloudflare",
            "connection_id": "default",
            "api_key": "cf-secret-token",
            "scopes": ["account:read"],
        }
    )
    secret_material = bundle.secret_material()
    secret_material["context"] = {"account_id": "context-account"}

    metadata = CloudflareConnectionAdapter().normalize_token_metadata(
        provider=CLOUDFLARE_PROVIDER,
        credential_bundle=bundle,
        secret_material=secret_material,
    )

    assert metadata["account_id"] == "context-account"
    assert metadata["zone_id"] == "env-zone"
    assert metadata["requested_capabilities"] == []
    assert metadata["account_id_configured"] is True
    assert metadata["zone_id_configured"] is True


def test_codex_provider_safe_payload_has_no_token_material():
    payload = CODEX_PROVIDER.to_dict()
    payload_text = str(payload).lower()
    assert "client_secret" not in payload_text
    assert "secret_value" not in payload_text
    assert "token_value" not in payload_text
    assert payload["providerId"] == "codex"
    assert payload["authType"] == "codex"
    assert [method["id"] for method in payload["authMethods"]] == [
        "chatgpt_account",
        "codex_access_token",
        "app_server_secret",
    ]
    assert payload["officialBrokerSupported"] is False
    assert payload["selfHostClientSupported"] is False
    assert payload["metadata"]["credential_kind"] == "codex_access_token"
    assert payload["metadata"]["provider_kind"] == "codex"
    assert payload["metadata"]["platform_api_key_required"] is False
    assert payload["metadata"]["not_platform_api_key"] is True
    assert payload["metadata"]["not_workspace_agent_token"] is True
    assert payload["authTemplate"] == "credential_bundle"
    assert payload["tokenImportSupported"] is True
    assert payload["scopeToCapability"][0]["credential_kind"] == "codex_access_token"
    assert payload["scopeToCapability"][1]["credential_kind"] == "codex_app_server_secret"


def test_codex_app_server_secret_requires_approval_for_connect_capability():
    resolved = resolve_connection_permissions(
        CODEX_PROVIDER,
        {
            "credential_kind": "codex_app_server_secret",
            "requested_capabilities": ["codex.app_server.connect"],
        },
    )

    assert resolved.capabilities == []
    assert resolved.approval_required_capabilities == ["codex.app_server.connect"]
    assert resolved.rejected_capabilities == []


def test_local_encrypted_credential_store_writes_private_file(tmp_path):
    path = tmp_path / "credentials.json"
    store = LocalEncryptedCredentialStore(path, key=LocalEncryptedCredentialStore.generate_key())

    envelope = store.put("codex", "default", "access_token", {"token": "secret-value"})

    assert store.get(envelope.credential_id) == {"token": "secret-value"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_github_provider_template_supports_manifest_driven_import():
    payload = GITHUB_PROVIDER.to_dict()
    payload_text = str(payload).lower()
    assert "access_token_value" not in payload_text
    assert "refresh_token" not in payload_text
    assert "secret_value" not in payload_text
    assert payload["providerId"] == "github"
    assert payload["authTemplate"] == "generic_oauth2_pkce"
    assert payload["tokenImportSupported"] is True
    assert payload["scopeToCapability"][0]["capabilities"] == ["github.user.read"]


def test_codex_core_provider_exposes_high_risk_execution_capabilities():
    capabilities = {capability.id: capability.risk for capability in CODEX_PROVIDER.capabilities}
    assert capabilities["codex.access_token.configure"] == "high"
    assert capabilities["codex.app_server.connect"] == "high"
    assert capabilities["codex.thread.start"] == "medium"
    assert capabilities["codex.turn.run"] == "medium"
    assert capabilities["codex.events.stream"] == "medium"
    assert capabilities["codex.approval.respond"] == "high"
    assert capabilities["codex.exec.run"] == "high"


def test_oauth_state_store_expires_state():
    now = 1_000.0
    store = InMemoryOAuthStateStore(now=lambda: now)
    store.put("state", {"provider_id": "google"}, ttl_seconds=10)

    now = 1_011.0

    try:
        store.pop("state")
    except ValueError as exc:
        assert str(exc) == "Invalid or expired OAuth state"
    else:
        raise AssertionError("expired OAuth state should fail closed")
