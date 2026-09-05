from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain.external.redaction import redact_sensitive


@dataclass
class WebhookEndpoint:
    id: str
    kind: str
    input_profile_id: str
    audience_policy_id: str = ""
    response_profile_id: str = ""
    security: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    default_delivery: dict[str, Any] = field(default_factory=dict)
    allowed_delivery_actions: list[str] = field(default_factory=list)
    ttl_seconds: int | None = None
    expires_at: int | None = None
    conversation: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    public_url: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, redact: bool = True) -> dict[str, Any]:
        security = dict(self.security)
        metadata = dict(self.metadata)
        if redact:
            security = redact_sensitive(security)
            metadata = redact_sensitive(metadata)
        return {
            "id": self.id,
            "kind": self.kind,
            "input_profile_id": self.input_profile_id,
            "audience_policy_id": self.audience_policy_id,
            "response_profile_id": self.response_profile_id,
            "security": security,
            "target": dict(self.target),
            "default_delivery": dict(self.default_delivery),
            "allowed_delivery_actions": list(self.allowed_delivery_actions),
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "conversation": dict(self.conversation),
            "response": dict(self.response),
            "enabled": bool(self.enabled),
            "public_url": dict(self.public_url),
            "metadata": metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WebhookEndpoint":
        security_value = value.get("security")
        target_value = value.get("target")
        default_delivery_value = value.get("default_delivery")
        conversation_value = value.get("conversation")
        response_value = value.get("response")
        public_url_value = value.get("public_url")
        metadata_value = value.get("metadata")
        security = (
            {str(key): item for key, item in security_value.items()}
            if isinstance(security_value, dict)
            else {}
        )
        target = (
            {str(key): item for key, item in target_value.items()}
            if isinstance(target_value, dict)
            else {}
        )
        default_delivery = (
            {str(key): item for key, item in default_delivery_value.items()}
            if isinstance(default_delivery_value, dict)
            else {}
        )
        conversation = (
            {str(key): item for key, item in conversation_value.items()}
            if isinstance(conversation_value, dict)
            else {}
        )
        response = (
            {str(key): item for key, item in response_value.items()}
            if isinstance(response_value, dict)
            else {}
        )
        public_url = (
            {str(key): item for key, item in public_url_value.items()}
            if isinstance(public_url_value, dict)
            else {}
        )
        metadata = (
            {str(key): item for key, item in metadata_value.items()}
            if isinstance(metadata_value, dict)
            else {}
        )
        allowed_actions_value = value.get("allowed_delivery_actions")
        if isinstance(allowed_actions_value, list):
            allowed_actions = [
                str(item).strip()
                for item in allowed_actions_value
                if str(item or "").strip()
            ]
        else:
            allowed_actions = []
        ttl_value = value.get("ttl_seconds")
        ttl_seconds = ttl_value if isinstance(ttl_value, int) else None
        expires_value = value.get("expires_at")
        expires_at = expires_value if isinstance(expires_value, int) else None
        return cls(
            id=str(value.get("id") or ""),
            kind=str(value.get("kind") or "generic"),
            input_profile_id=str(value.get("input_profile_id") or "generic.webhook.default"),
            audience_policy_id=str(value.get("audience_policy_id") or ""),
            response_profile_id=str(value.get("response_profile_id") or ""),
            security=security,
            target=target,
            default_delivery=default_delivery,
            allowed_delivery_actions=allowed_actions,
            ttl_seconds=ttl_seconds,
            expires_at=expires_at,
            conversation=conversation,
            response=response,
            enabled=bool(value.get("enabled", True)),
            public_url=public_url,
            metadata=metadata,
        )
