"""Independent child history with parent context and ordinary Host authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.conversation_context import (
    LINK_VERSION,
    context_binding,
    context_link,
    inherited_context,
    inherited_turn_options,
    resolve_linked_conversation,
)
from tobkiri_protocol.saved_conversation import (
    validate_saved_conversation_context,
    validate_saved_conversation_input,
)

CONVERSATION = (
    "tobkiri.resource.conversation.v1",
    "rumi_conversation_store_pack.conversation-resource",
)
MANAGE = (
    "tobkiri.action.conversation.manage.v1",
    "rumi_conversation_store_pack.conversation-manage",
)
TURN = ("tobkiri.resource.turn.v1", "rumi_turn_runtime_pack.turn-resource")
EVENTS = ("tobkiri.event.turn.v1", "rumi_turn_runtime_pack.turn-events")
SAVED = ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
STOP = ("tobkiri.action.turn.stop.v1", "rumi_turn_runtime_pack.turn-stop")
RECONCILE = (
    "tobkiri.action.turn.reconcile.v1",
    "rumi_turn_runtime_pack.turn-reconcile",
)
SLOT = "side-chat"
DEPENDENCIES = (CONVERSATION, MANAGE, TURN, EVENTS, SAVED, STOP, RECONCILE)


class SideChat:
    """Consume exact selected public contracts; never another Pack's modules."""

    def __init__(self, client: Any, profile_id: str) -> None:
        """Use only the Host-captured Profile and restricted contract client."""
        self.client = client
        self.profile_id = profile_id

    def _read(self, conversation_id: str) -> Mapping[str, Any]:
        response = self.client.invoke(
            *CONVERSATION,
            {
                "profile_id": self.profile_id,
                "operation": "get",
                "conversation_id": conversation_id,
            },
        )
        value = response.get("conversation") if isinstance(response, Mapping) else None
        if not isinstance(value, Mapping) or value.get("id") != conversation_id:
            raise LookupError("conversation is unavailable")
        return value

    def _snapshot(self) -> Mapping[str, Any]:
        response = self.client.invoke(
            *CONVERSATION,
            {
                "profile_id": self.profile_id,
                "operation": "list",
            },
        )
        if (
            not isinstance(response, Mapping)
            or not isinstance(response.get("conversations"), list)
            or type(response.get("revision")) is not int
        ):
            raise LookupError("conversation owner is unavailable")
        return response

    @staticmethod
    def _child(snapshot: Mapping[str, Any], parent_id: str) -> Mapping[str, Any] | None:
        matches = []
        for value in snapshot["conversations"]:
            if not isinstance(value, Mapping):
                raise ValueError("conversation owner projection is invalid")
            link = context_link(value)
            if (
                link
                and link["parent_conversation_id"] == parent_id
                and link["slot"] == SLOT
            ):
                matches.append(value)
        if len(matches) > 1:
            raise ValueError("conversation child slot is ambiguous")
        return matches[0] if matches else None

    def get(self, parent_id: str) -> dict[str, Any]:
        """Read separate history and fresh context; never manufacture a child."""
        try:
            parent = self._read(parent_id)
            binding = context_binding(parent, self.profile_id)
            validate_saved_conversation_context(parent)
            child = self._child(self._snapshot(), parent_id)
            if child is None:
                return {
                    "status": "missing",
                    "parent_conversation_id": parent_id,
                    "parent_revision": binding["parent_revision"],
                }
            resolved = resolve_linked_conversation(
                child,
                parent,
                binding,
                self.profile_id,
            )
            validate_saved_conversation_context(resolved)
            turns = self.client.invoke(
                *TURN,
                {
                    "profile_id": self.profile_id,
                    "operation": "list",
                    "conversation_id": child["id"],
                    "limit": 1,
                },
            )
            if not isinstance(turns, Mapping) or not isinstance(
                turns.get("turns"), list
            ):
                raise LookupError("turn owner is unavailable")
            if any(
                turn.get("conversation_id") != child["id"] for turn in turns["turns"]
            ):
                raise ValueError("turn projection differs from the child")
            return {
                "status": "available",
                "parent_conversation_id": parent_id,
                "parent_revision": binding["parent_revision"],
                "conversation_id": child["id"],
                "revision": child["conversation_revision"],
                "context_binding": binding,
                "thread": {
                    "conversation": resolved,
                    "messages": child["messages"],
                    "context": {
                        "profile_id": self.profile_id,
                        "parent_conversation_id": parent_id,
                        "parent_revision": binding["parent_revision"],
                        **inherited_context(parent),
                        "approval_policy": "current_host_policy",
                    },
                    "pending_turn": turns["turns"][0] if turns["turns"] else None,
                },
            }
        except Exception:
            return {"status": "unavailable", "reason": "parent_context_unavailable"}

    def ensure(self, parent_id: str, expected_parent_revision: int) -> dict[str, Any]:
        """Create a single owner-guarded child slot under ordinary write policy."""
        parent = self._read(parent_id)
        if parent.get("conversation_revision") != expected_parent_revision:
            raise ValueError("parent conversation revision changed")
        binding = context_binding(parent, self.profile_id)
        validate_saved_conversation_context(parent)
        snapshot = self._snapshot()
        if self._child(snapshot, parent_id) is None:
            identity = canonical_digest([self.profile_id, parent_id, SLOT])
            child = {
                "id": "conversation:" + identity.removeprefix("sha256:"),
                "title": "Side chat",
                "parent_conversation_id": parent_id,
                "context_link": {
                    "version": LINK_VERSION,
                    "parent_conversation_id": parent_id,
                    "slot": SLOT,
                    "created_from_parent_revision": expected_parent_revision,
                },
                "metadata": {"conversation_channel": "side", "is_hidden": True},
            }
            try:
                result = self.client.invoke(
                    *MANAGE,
                    {
                        "profile_id": self.profile_id,
                        "operation": "create",
                        "conversation": child,
                        "expected_revision": snapshot["revision"],
                    },
                )
                if (
                    not isinstance(result, Mapping)
                    or not isinstance(result.get("conversation"), Mapping)
                    or result["conversation"].get("context_link")
                    != child["context_link"]
                ):
                    raise LookupError("child creation is unconfirmed")
            except Exception:
                # A competing create may have won; only an exact fresh owner
                # record with the same parent context can confirm that result.
                fresh_parent = self._read(parent_id)
                fresh_child = self._child(self._snapshot(), parent_id)
                if fresh_child is None or inherited_context(
                    fresh_parent
                ) != inherited_context(parent):
                    raise LookupError("child creation is unavailable") from None
                fresh_link = context_link(fresh_child)
                if (
                    fresh_link is None
                    or fresh_link["created_from_parent_revision"]
                    != binding["parent_revision"]
                ):
                    raise LookupError("child creation is unavailable") from None
        result = self.get(parent_id)
        if result["status"] != "available":
            raise LookupError("child context is unavailable")
        return result

    def _owned_child(self, conversation_id: str) -> Mapping[str, Any]:
        child = self._read(conversation_id)
        link = context_link(child)
        if link is None or link["slot"] != SLOT:
            raise ValueError("turn does not target the selected child slot")
        return child

    def send(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Bind context before invoking the existing durable saved-turn contract."""
        child = self._owned_child(payload["conversation_id"])
        if child.get("conversation_revision") != payload["expected_child_revision"]:
            raise ValueError("child conversation revision changed")
        link = context_link(child)
        if link is None:
            raise ValueError("child context link is unavailable")
        parent = self._read(link["parent_conversation_id"])
        binding = context_binding(parent, self.profile_id)
        if binding["parent_revision"] != payload["expected_parent_revision"]:
            raise ValueError("parent conversation revision changed")
        resolved = resolve_linked_conversation(child, parent, binding, self.profile_id)
        validate_saved_conversation_context(resolved)
        initial = validate_saved_conversation_input(
            {
                "request": {
                    "turn_id": payload["turn_id"],
                    "conversation_id": child["id"],
                    "conversation_revision": child["conversation_revision"],
                    "content": payload["content"],
                    "context_binding": binding,
                    **inherited_turn_options(parent),
                }
            }
        )
        result = self.client.invoke(*SAVED, initial)
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("turn"), Mapping)
            or (
                result["turn"].get("id") != payload["turn_id"]
                or result["turn"].get("conversation_id") != child["id"]
            )
        ):
            raise LookupError("saved turn outcome is unconfirmed")
        return result

    def turn(
        self, action: str, conversation_id: str, turn_id: str
    ) -> Mapping[str, Any]:
        """Read, reconcile or stop only an existing turn owned by this child."""
        self._owned_child(conversation_id)
        record = self.client.invoke(
            *TURN,
            {
                "profile_id": self.profile_id,
                "operation": "get",
                "turn_id": turn_id,
            },
        )
        if (
            not isinstance(record, Mapping)
            or record.get("id") != turn_id
            or (record.get("conversation_id") != conversation_id)
        ):
            raise ValueError("turn does not belong to the child conversation")
        if action == "events":
            return self.client.invoke(
                *EVENTS,
                {
                    "profile_id": self.profile_id,
                    "operation": "get",
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                },
            )
        if action not in {"stop", "reconcile"}:
            raise ValueError("side turn operation is invalid")
        return self.client.invoke(
            *(STOP if action == "stop" else RECONCILE),
            {
                "turn_id": turn_id,
            },
        )
