from __future__ import annotations

from ..models import ConnectionProvider, OAuthConfig, ProviderCapability

GOOGLE_PROVIDER = ConnectionProvider(
    provider_id="google",
    display_name="Google",
    description="Connect Gmail and Google Drive capabilities.",
    icon="google",
    service_kind="google",
    auth_type="oauth2",
    official_broker_supported=True,
    self_host_client_supported=True,
    auth_template="generic_oauth2_pkce",
    token_import_supported=True,
    priority=40,
    oauth=OAuthConfig(
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        revoke_url="https://oauth2.googleapis.com/revoke",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        default_scopes=["openid", "email", "profile"],
        pkce_supported=True,
        token_endpoint_auth_method="client_secret_post",
    ),
    capabilities=[
        ProviderCapability(
            id="google.drive.file",
            display_name="Google Drive selected files",
            description="Access files created/opened/shared with Rumi through Drive file scope and picker-style UX.",
            risk="low",
        ),
        ProviderCapability(
            id="google.gmail.labels",
            display_name="Gmail labels",
            description="Read Gmail labels in the low-friction mode.",
            risk="low",
        ),
        ProviderCapability(
            id="google.gmail.metadata_restricted",
            display_name="Gmail metadata/search",
            description="Search/inspect Gmail metadata only when restricted-scope mode is explicitly enabled.",
            risk="high",
        ),
    ],
    services=[
        {
            "service_id": "drive",
            "display_name": "Google Drive",
            "recommended_scopes": ["https://www.googleapis.com/auth/drive.file"],
            "restricted_scopes": ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/drive.readonly"],
        },
        {
            "service_id": "gmail",
            "display_name": "Gmail",
            "scope_modes": [
                {"mode": "labels_only", "classification": "non_sensitive", "scopes": ["https://www.googleapis.com/auth/gmail.labels"]},
                {"mode": "metadata_search", "classification": "restricted", "scopes": ["https://www.googleapis.com/auth/gmail.metadata"], "requires": "restricted_scope_review_or_self_host_ack"},
                {"mode": "readonly_body", "classification": "restricted", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"], "requires": "restricted_scope_review_or_self_host_ack"},
            ],
            "restricted_scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.metadata", "https://www.googleapis.com/auth/gmail.modify", "https://mail.google.com/"],
        },
    ],
    scope_presets=[
        {"id": "google_identity", "label": "Google identity", "scopes": ["openid", "email", "profile"]},
        {"id": "google_drive", "label": "Google Drive selected files", "scopes": ["openid", "email", "profile", "https://www.googleapis.com/auth/drive.file"]},
        {"id": "google_gmail_labels", "label": "Gmail labels", "scopes": ["openid", "email", "profile", "https://www.googleapis.com/auth/gmail.labels"]},
    ],
    scope_to_capability=[
        {"scopes": ["https://www.googleapis.com/auth/drive.file"], "capabilities": ["google.drive.file"]},
        {"scopes": ["https://www.googleapis.com/auth/gmail.labels"], "capabilities": ["google.gmail.labels"]},
        {
            "scopes": [
                "https://www.googleapis.com/auth/gmail.metadata",
                "https://www.googleapis.com/auth/gmail.readonly",
            ],
            "capabilities": ["google.gmail.metadata_restricted"],
        },
    ],
    adapter={
        "python": "core_runtime.connections.adapter:GenericConnectionAdapter",
        "sdk_optional": True,
    },
    metadata={
        "scope_warning": "Show exact scopes before authorization. Broad Gmail/Drive scopes can trigger verification/security review.",
    },
)
