"""Codex connection catalog owned by the provider registry Pack."""

from __future__ import annotations

from core_runtime.connections.models import ConnectionProvider, ProviderCapability

CODEX_PROVIDER = ConnectionProvider(
    provider_id="codex",
    display_name="Codex",
    description="Connect a Codex local/programmatic workflow access token and optional Codex App Server endpoint.",
    icon="terminal",
    service_kind="dev",
    auth_type="codex",
    official_broker_supported=False,
    self_host_client_supported=False,
    auth_template="credential_bundle",
    auth_methods=[
        {
            "id": "chatgpt_account",
            "displayName": "ChatGPT account via Codex App Server",
            "description": "Read the signed-in Codex App Server account without storing a Rumi Platform API key.",
            "credential_kind": "chatgpt_account",
            "secret_material": False,
        },
        {
            "id": "codex_access_token",
            "displayName": "Codex access token",
            "description": "Optional local/programmatic workflow token stored separately from Codex App Server auth.",
            "credential_kind": "codex_access_token",
            "secret_material": True,
        },
        {
            "id": "app_server_secret",
            "displayName": "Codex App Server secret",
            "description": "Optional WS token or shared secret for non-loopback App Server endpoints.",
            "credential_kind": "codex_app_server_secret",
            "secret_material": True,
        },
    ],
    token_import_supported=True,
    priority=50,
    capabilities=[
        ProviderCapability(
            id="codex.access_token.configure",
            display_name="Configure Codex access token",
            description="Store a Codex access token for local and programmatic workflow integrations. The token is not used for Codex App Server auth.",
            risk="high",
        ),
        ProviderCapability(
            id="codex.app_server.connect",
            display_name="Connect Codex App Server",
            description="Use Codex App Server as a Tools & MCP tool source and automation endpoint with separate App Server auth.",
            risk="high",
        ),
        ProviderCapability(
            id="codex.thread.start",
            display_name="Start Codex thread",
            description="Start a Codex thread through a local or App Server-backed transport.",
            risk="medium",
        ),
        ProviderCapability(
            id="codex.turn.run",
            display_name="Run Codex turn",
            description="Run a Codex turn against a configured thread.",
            risk="medium",
        ),
        ProviderCapability(
            id="codex.events.stream",
            display_name="Stream Codex events",
            description="Read event streams from a Codex session without granting approvals by itself.",
            risk="medium",
        ),
        ProviderCapability(
            id="codex.approval.respond",
            display_name="Respond to Codex approvals",
            description="Approve or deny high-impact Codex actions such as writes, terminal commands, and git operations.",
            risk="high",
        ),
        ProviderCapability(
            id="codex.exec.run",
            display_name="Run Codex execution",
            description="Run Codex-backed execution that can reach workspace, terminal, or git operations under approval policy.",
            risk="high",
        ),
    ],
    scope_presets=[],
    scope_to_capability=[
        {"credential_kind": "codex_access_token", "capabilities": ["codex.access_token.configure"]},
        {"credential_kind": "codex_app_server_secret", "capabilities": ["codex.app_server.connect"]},
    ],
    adapter={
        "python": "core_runtime.connections.adapter:GenericConnectionAdapter",
        "sdk_optional": True,
    },
    metadata={
        "credential_kind": "codex_access_token",
        "app_server_auth_kind": "codex_app_server_secret",
        "provider_kind": "codex",
        "platform_api_key_required": False,
        "not_platform_api_key": True,
        "not_workspace_agent_token": True,
        "secret_handling": "Never expose the raw Codex token or Codex App Server secret in Settings payloads, logs, snapshots, repository files, or CLI arguments.",
    },
)
