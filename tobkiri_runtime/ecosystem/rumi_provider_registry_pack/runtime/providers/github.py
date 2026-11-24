"""GitHub connection catalog owned by the provider registry Pack."""

from __future__ import annotations

from core_runtime.connections.models import (
    ConnectionProvider,
    OAuthConfig,
    ProviderCapability,
)

GITHUB_PROVIDER = ConnectionProvider(
    provider_id="github",
    display_name="GitHub",
    description="Connect GitHub identity, repositories, and workflow capabilities through OAuth or token import.",
    icon="github",
    service_kind="dev",
    auth_type="oauth2",
    official_broker_supported=False,
    self_host_client_supported=True,
    auth_template="generic_oauth2_pkce",
    token_import_supported=True,
    priority=45,
    oauth=OAuthConfig(
        authorization_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        default_scopes=["read:user", "user:email"],
        pkce_supported=False,
        token_endpoint_auth_method="client_secret_post",
    ),
    scope_presets=[
        {"id": "identity", "label": "GitHub identity", "scopes": ["read:user", "user:email"]},
        {"id": "public_repo", "label": "Public repositories", "scopes": ["read:user", "user:email", "public_repo"]},
    ],
    capabilities=[
        ProviderCapability(
            id="github.user.read",
            display_name="Read GitHub identity",
            description="Read the connected GitHub account profile.",
            risk="low",
        ),
        ProviderCapability(
            id="github.repo.read",
            display_name="Read repositories",
            description="Read repository metadata or contents allowed by the granted token scopes.",
            risk="medium",
        ),
        ProviderCapability(
            id="github.repo.write",
            display_name="Write repositories",
            description="Write repository contents or pull request state allowed by the granted token scopes.",
            risk="high",
        ),
        ProviderCapability(
            id="github.actions.workflow",
            display_name="Manage workflows",
            description="Read or trigger GitHub Actions workflow state when workflow scope is granted.",
            risk="high",
        ),
    ],
    scope_to_capability=[
        {"scopes": ["read:user", "user:email"], "capabilities": ["github.user.read"]},
        {"scopes": ["public_repo"], "capabilities": ["github.repo.read"]},
        {"scopes": ["repo"], "capabilities": ["github.repo.read", "github.repo.write"]},
        {"scopes": ["workflow"], "capabilities": ["github.actions.workflow"]},
    ],
    adapter={
        "python": "core_runtime.connections.adapter:GenericConnectionAdapter",
        "sdk_optional": True,
    },
)
