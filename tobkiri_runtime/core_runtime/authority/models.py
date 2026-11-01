"""Authority service data models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..host_permissions.models import HOST_PERMISSION_IDS


AUTHORITY_PERMISSION_IDS = frozenset(
    {
        "model.invoke",
        "api_key.manage",
        "api_key.use",
        "function.call",
        "secret.read",
        "secret.manage",
        "network.egress",
        "network.manage",
        "host.execute",
        "tool.execute",
        "file.read",
        "file.write",
        "pack.read",
        "pack.manage",
        "pack.approve",
        "provider.read",
        "provider.manage",
        "authority.request.read",
        "authority.request.list",
        "authority.request.approve",
        "authority.request.deny",
        "authority.grant.read",
        "authority.grant.manage",
        "authority.host_intent.approve",
        "authority.host_intent.deny",
        "auth.token.issue",
        "auth.token.list",
        "auth.token.revoke",
    }
) | HOST_PERMISSION_IDS


@dataclass(frozen=True)
class AuthorityResource:
    kind: str
    provider_id: str | None = None
    api_id: str | None = None
    model_id: str | None = None
    function_id: str | None = None
    pack_id: str | None = None
    host_action: str | None = None
    domain: str | None = None
    port: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthorityDecision:
    allowed: bool
    permission_id: str
    principal_id: str
    reason: str = ""
    request_id: str | None = None
    approval_required: bool = False
    risk_level: str = "low"
    grant_config: dict[str, Any] = field(default_factory=dict)
    resource: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_approval_event(self) -> dict[str, Any]:
        summary = self.reason or f"{self.permission_id} requires approval"
        return {
            "approval_kind": "authority",
            "authority": True,
            "requires_approval": True,
            "approval_required": True,
            "approval_request_id": self.request_id,
            "request_id": self.request_id,
            "principal_id": self.principal_id,
            "permission_id": self.permission_id,
            "resource": dict(self.resource or {}),
            "risk_level": self.risk_level,
            "display_summary": summary,
            "reason": self.reason,
            "message": summary,
        }


@dataclass(frozen=True)
class AuthorityRequest:
    request_id: str
    status: Literal["pending", "approved", "denied", "expired"]
    principal_id: str
    permission_id: str
    resource: dict[str, Any]
    reason: str
    risk_level: str
    created_at: str
    expires_at: str | None = None
    conversation_id: str | None = None
    profile_id: str | None = None
    node_id: str | None = None
    graph_id: str | None = None
    debug_session_id: str = ""
    lease_epoch: int = 0
    debug_run_id: str = ""
    workspace_identity_digest: str = ""
    pack_id: str = ""
    debug_profile_id: str = ""
    operation_owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthorityRequest":
        raw_status = data.get("status")
        status: Literal["pending", "approved", "denied", "expired"] = "pending"
        if raw_status == "approved":
            status = "approved"
        elif raw_status == "denied":
            status = "denied"
        elif raw_status == "expired":
            status = "expired"
        raw_resource = data.get("resource")
        resource = dict(raw_resource) if isinstance(raw_resource, dict) else {}
        return cls(
            request_id=str(data.get("request_id") or ""),
            status=status,
            principal_id=str(data.get("principal_id") or ""),
            permission_id=str(data.get("permission_id") or ""),
            resource=resource,
            reason=str(data.get("reason") or ""),
            risk_level=str(data.get("risk_level") or "low"),
            created_at=str(data.get("created_at") or ""),
            expires_at=str(data.get("expires_at") or "") or None,
            conversation_id=str(data.get("conversation_id") or "") or None,
            profile_id=str(data.get("profile_id") or "") or None,
            node_id=str(data.get("node_id") or "") or None,
            graph_id=str(data.get("graph_id") or "") or None,
            debug_session_id=str(data.get("debug_session_id") or ""),
            lease_epoch=int(data.get("lease_epoch") or 0),
            debug_run_id=str(data.get("debug_run_id") or ""),
            workspace_identity_digest=str(data.get("workspace_identity_digest") or ""),
            pack_id=str(data.get("pack_id") or ""),
            debug_profile_id=str(data.get("debug_profile_id") or ""),
            operation_owner=str(data.get("operation_owner") or ""),
        )
