"""Project normalized connector events into conversation and agent contracts."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping

CONVERSATION_RESOURCE = "rumi.resource.conversation.v1"
CONVERSATION_ACTION = "rumi.action.conversation.manage.v1"
MESSAGE_ACTION = "rumi.action.message.manage.v1"
JOB_ACTION = "rumi.action.job.v1"


class ConnectorTurnAdapter:
    """Create one connector conversation and dispatch one global agent job."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def route(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Project an event only when public connector routes include `turn`."""

        profile_id = str(payload.get("profile_id") or "default")
        connector = _mapping(payload.get("connector"))
        config = _mapping(connector.get("config"))
        routes = config.get("routes")
        routes = (
            [str(item) for item in routes]
            if isinstance(routes, list)
            else ["turn"]
        )
        if "turn" not in routes:
            return {"status": "skipped", "reason": "turn route is not enabled"}
        event = _mapping(payload.get("event"))
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("connector event_id is required")
        connector_id = str(payload.get("connector_id") or connector.get("id") or "")
        channel_id = str(event.get("channel_id") or event.get("actor_id") or "default")
        conversation_id = "connector-" + _hash(f"{connector_id}\0{channel_id}")[:40]
        conversation = self._conversation(profile_id, conversation_id)
        if conversation is None:
            conversation = self._create_conversation(
                profile_id,
                conversation_id,
                connector_id,
                channel_id,
                event,
                config,
            )
        message_id = "connector-" + _hash(f"{connector_id}\0{event_id}")[:40]
        existing = next(
            (
                item
                for item in conversation.get("messages") or []
                if isinstance(item, Mapping) and item.get("id") == message_id
            ),
            None,
        )
        if existing is not None:
            return {
                "status": "accepted",
                "deduplicated": True,
                "conversation_id": conversation_id,
                "message_id": message_id,
            }
        appended = self.client.invoke(
            MESSAGE_ACTION,
            "append",
            {
                "profile_id": profile_id,
                "conversation_id": conversation_id,
                "expected_conversation_revision": conversation["conversation_revision"],
                "message": {
                    "id": message_id,
                    "role": "user",
                    "content": str(event.get("text") or ""),
                    "metadata": {
                        "source": "connector",
                        "connector_id": connector_id,
                        "adapter_id": str(event.get("adapter_id") or ""),
                        "event_id": event_id,
                        "actor_id": str(event.get("actor_id") or ""),
                        "channel_id": channel_id,
                        "event": _safe_event(event),
                    },
                },
            },
        )
        agent = self.client.invoke(
            JOB_ACTION,
            "dispatch",
            {
                "action_id": "agent.turn",
                "idempotency_key": f"connector:{_hash(event_id)[:40]}:turn",
                "profile_id": profile_id,
                "payload": {
                    "agent_profile_id": str(
                        config.get("agent_profile_id") or "default"
                    ),
                    "conversation_id": conversation_id,
                    "conversation_revision": appended["conversation_revision"],
                    "connector_id": connector_id,
                    "event_id": event_id,
                },
            },
        )
        return {
            "status": "accepted",
            "deduplicated": False,
            "conversation_id": conversation_id,
            "message_id": appended["message"]["id"],
            "agent": agent,
        }

    def _conversation(
        self,
        profile_id: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        value = self.client.invoke(
            CONVERSATION_RESOURCE,
            "get",
            {"profile_id": profile_id, "conversation_id": conversation_id},
        )
        return dict(value) if isinstance(value, Mapping) else None

    def _create_conversation(
        self,
        profile_id: str,
        conversation_id: str,
        connector_id: str,
        channel_id: str,
        event: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = self.client.invoke(
            CONVERSATION_RESOURCE,
            "list",
            {"profile_id": profile_id},
        )
        try:
            created = self.client.invoke(
                CONVERSATION_ACTION,
                "create",
                {
                    "profile_id": profile_id,
                    "expected_revision": int(snapshot.get("revision") or 0),
                    "conversation": {
                        "id": conversation_id,
                        "title": str(event.get("channel_id") or "Connector")[:500],
                        "agent_id": str(config.get("agent_profile_id") or "default"),
                        "tags": ["connector", str(event.get("adapter_id") or "")],
                        "conversation_kind": "connector",
                        "metadata": {
                            "connector_id": connector_id,
                            "channel_id": channel_id,
                        },
                    },
                },
            )
            return dict(created["conversation"])
        except Exception:
            current = self._conversation(profile_id, conversation_id)
            if current is None:
                raise
            return current


def create_connector_route(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the connector-to-turn route projection."""

    adapter = ConnectorTurnAdapter(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "route":
            raise ValueError(f"unknown connector turn operation: {name}")
        return adapter.route(payload)

    return operation


def _safe_event(value: Mapping[str, Any]) -> dict[str, Any]:
    secret_parts = ("credential", "oauth", "password", "secret", "signature", "token")
    return {
        str(key): item
        for key, item in value.items()
        if not any(part in str(key).casefold() for part in secret_parts)
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

