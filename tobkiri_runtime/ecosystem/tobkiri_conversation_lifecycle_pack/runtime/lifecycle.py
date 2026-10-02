"""Optional archive policy using public conversation contracts and owner CAS."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from tobkiri_protocol.conversation_lifecycle import (
    ARCHIVE_MODE,
    LIFECYCLE_VERSION,
    completion_source,
)

CONVERSATION = "tobkiri.resource.conversation.v1"
CONVERSATION_OPERATION = "rumi_conversation_store_pack.conversation-resource"
CONVERSATION_ACTION = "tobkiri.action.conversation.manage.v1"
CONVERSATION_ACTION_OPERATION = "rumi_conversation_store_pack.conversation-manage"
ARCHIVE_ACTION_ID = "conversation-lifecycle.archive-due"


class ContractClient(Protocol):
    """Captured broker client; writes require ordinary contract authorization."""

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
    ) -> Any:
        """Invoke one exact admitted public contract edge."""


class ConversationLifecycle:
    """Reconstruct deadlines from the owner on every scan and after restart."""

    def __init__(
        self,
        client: ContractClient,
        profile_id: str,
        *,
        clock_ms: Callable[[], int] | None = None,
        enabled: bool = True,
    ) -> None:
        self.client = client
        self.profile_id = profile_id
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.enabled = enabled

    def status(self, conversation_id: str) -> dict[str, Any]:
        """Observe current policy without manufacturing a successful connection."""
        if not self.enabled:
            return {"status": "disabled", "version": LIFECYCLE_VERSION}
        try:
            conversation = self._get(conversation_id)
        except Exception:
            return {"status": "unavailable", "version": LIFECYCLE_VERSION}
        return {
            "status": "available",
            "profile_id": self.profile_id,
            **completion_source(conversation),
        }

    def configure(self, conversation_id: str, mode: str) -> dict[str, Any]:
        """Save the explicit mode through the ordinary conversation CAS action."""
        if not self.enabled:
            raise PermissionError("conversation lifecycle Pack is disabled")
        if mode not in {"manual", ARCHIVE_MODE}:
            raise ValueError("unknown conversation lifecycle mode")
        conversation = self._get(conversation_id)
        metadata = dict(conversation.get("metadata") or {})
        metadata["conversation_lifecycle"] = {"version": 1, "mode": mode}
        result = self._update(conversation, {"metadata": metadata})
        return {"status": "configured", "mode": mode, "result": result}

    def tick(self) -> dict[str, Any]:
        """Archive due completed conversations after a fresh owner read and CAS."""
        if not self.enabled:
            return {"status": "disabled", "archived": [], "count": 0}
        now_ms = self.clock_ms()
        try:
            snapshot = self._read({"operation": "list"})
            records = snapshot["conversations"]
            if not isinstance(records, list):
                raise ValueError("conversation list projection is invalid")
        except Exception:
            return {"status": "unavailable", "archived": [], "count": 0}
        archived: list[str] = []
        conflicts: list[str] = []
        unavailable: list[str] = []
        for record in records:
            if not isinstance(record, Mapping) or not self._due(record, now_ms):
                continue
            conversation_id = str(record["id"])
            try:
                current = self._get(conversation_id)
                if not self._due(current, now_ms):
                    continue
                # A resume/new completion between read and write invalidates CAS.
                result = self._update(current, {"is_archived": True})
                saved = result.get("conversation")
                if (
                    not isinstance(saved, Mapping)
                    or saved.get("is_archived") is not True
                ):
                    raise RuntimeError("archive result is unconfirmed")
                archived.append(conversation_id)
            except Exception as exc:
                if type(exc).__name__ == "ConversationConflict":
                    conflicts.append(conversation_id)
                else:
                    unavailable.append(conversation_id)
        return {
            "status": "unavailable" if unavailable else "ok",
            "archived": archived,
            "count": len(archived),
            "conflicts": conflicts,
            "unavailable": unavailable,
        }

    def _read(self, payload: Mapping[str, Any]) -> Any:
        return self.client.invoke(
            CONVERSATION,
            CONVERSATION_OPERATION,
            {
                **payload,
                "profile_id": self.profile_id,
            },
        )

    def _get(self, conversation_id: str) -> Mapping[str, Any]:
        result = self._read(
            {
                "operation": "get",
                "conversation_id": conversation_id,
            }
        )
        conversation = (
            result.get("conversation") if isinstance(result, Mapping) else None
        )
        if (
            not isinstance(conversation, Mapping)
            or conversation.get("id") != conversation_id
        ):
            raise LookupError("conversation source is unavailable")
        return conversation

    def _update(self, conversation: Mapping[str, Any], patch: Mapping[str, Any]) -> Any:
        return self.client.invoke(
            CONVERSATION_ACTION,
            CONVERSATION_ACTION_OPERATION,
            {
                "operation": "update",
                "profile_id": self.profile_id,
                "conversation_id": conversation["id"],
                "patch": dict(patch),
                "expected_conversation_revision": conversation["conversation_revision"],
            },
        )

    @staticmethod
    def _due(conversation: Mapping[str, Any], now_ms: int) -> bool:
        source = completion_source(conversation)
        due = source["archive_due_at_ms"]
        return due is not None and now_ms >= due and not source["is_archived"]


def create_lifecycle_job_adapter(
    client: ContractClient,
    profile_id: str,
    *,
    clock_ms: Callable[[], int] | None = None,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Bind the scheduled action to an admitted Profile-scoped public client."""
    runtime = ConversationLifecycle(client, profile_id, clock_ms=clock_ms)

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (
            name != "dispatch"
            or payload.get("action_id") != ARCHIVE_ACTION_ID
            or payload.get("profile_id") != profile_id
        ):
            raise PermissionError("lifecycle job binding is invalid")
        return runtime.tick()

    return operation
