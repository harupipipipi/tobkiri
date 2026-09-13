from __future__ import annotations

# Reference implementation. Adapt to the repo's actual HTTP framework.
from fastapi import APIRouter, HTTPException

from core_runtime.connections.oauth_service import OAuthClientConfig, OAuthService
from core_runtime.connections.registry import ConnectionsRegistry


def build_connections_router(registry: ConnectionsRegistry, oauth_service: OAuthService) -> APIRouter:
    router = APIRouter(prefix="/api/connections", tags=["connections"])

    @router.get("/providers")
    def list_providers() -> list[dict]:
        return registry.list_providers()

    @router.post("/{provider_id}/start")
    def start_connection(provider_id: str, body: dict) -> dict:
        provider = registry.get(provider_id)
        mode = body.get("mode", "self_host")
        config = OAuthClientConfig(
            client_id=body.get("client_id") or _host_contract_required(provider_id, "CLIENT_ID"),
            client_secret=body.get("client_secret") or _host_contract_optional(provider_id, "CLIENT_SECRET"),
            redirect_uri=body["redirect_uri"],
            scopes=body.get("scopes") or (provider.oauth.default_scopes if provider.oauth else []),
            mode=mode,
            official_broker_base_url=body.get("official_broker_base_url"),
        )
        result = oauth_service.start(provider, config, profile_id=body.get("profile_id"))
        return {"authorizationUrl": result.authorization_url, "state": result.state}

    @router.get("/oauth/callback/{provider_id}")
    def oauth_callback(provider_id: str, code: str, state: str, error: str | None = None) -> dict:
        if error:
            raise HTTPException(status_code=400, detail=error)
        callback_state = oauth_service.validate_callback_state(state)
        if callback_state["provider_id"] != provider_id:
            raise HTTPException(status_code=400, detail="Provider/state mismatch")
        # Exchange code for token in provider adapter, store via CredentialStore, create Connection.
        # Keep raw token out of response.
        return {"status": "ok", "providerId": provider_id, "codeReceived": bool(code)}

    return router


def _host_contract_required(provider_id: str, key: str) -> str:
    value = _host_contract_optional(provider_id, key)
    if not value:
        raise HTTPException(status_code=400, detail=f"Missing self-host OAuth config for {provider_id}: {key}")
    return value


def _host_contract_optional(provider_id: str, key: str) -> str | None:
    from core_runtime.host_contract import host_contract_value

    value = host_contract_value(
        f"oauth_{provider_id.strip().lower()}_{key.strip().lower()}",
        provider_id=provider_id,
    )
    return value or None
