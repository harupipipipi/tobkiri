"""Fail-closed defaultspack facade for durable deferred steers."""

from __future__ import annotations

import uuid
from typing import Any, List, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
)

AUTHORITY = "rumi.service.host.authorize.v1"
RESOURCE = "rumi.resource.agent.state.v1"
ACTION = "rumi.action.agent.state.v1"
STATE_PACK_ID = "rumi_agent_state_store_pack"
_ACTIVE_STATUSES = ["queued", "ready", "applied", "failed"]


class DeferredSteerFacadeError(RuntimeError):
    """Expose stable deferred-steer diagnostics without local fallback."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = status


class DeferredSteerFacade:
    """Route durable deferred steers through the selected canonical owner."""

    def __init__(self, context: Mapping[str, Any] | None = None) -> None:
        self.context = dict(context or {})
        self.profile_id = _profile_id()

    def list(
        self,
        *,
        scope_type: str = "",
        scope_id: str = "",
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        """List active steers or bounded history for an exact scope."""

        result = _invoke(
            RESOURCE,
            "deferred.list",
            {
                "profile_id": self.profile_id,
                "scope_type": scope_type,
                "scope_id": scope_id,
                **({} if include_history else {"statuses": _ACTIVE_STATUSES}),
            },
        )
        values = result.get("deferred_steers") if isinstance(result, Mapping) else []
        return [_project(item) for item in values or [] if isinstance(item, Mapping)]

    def register(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Register one visible, non-auto-executing deferred steer."""

        scope_type = str(payload.get("scope_type") or "conversation")
        scope_id = str(
            payload.get("scope_id")
            or payload.get("conversation_id")
            or payload.get("execution_id")
            or ""
        )
        result = self._mutate(
            "deferred.register",
            {
                "deferred_steer_id": str(payload.get("deferred_steer_id") or uuid.uuid4()),
                "idempotency_key": str(payload.get("idempotency_key") or ""),
                "title": payload.get("title"),
                "instruction": payload.get("instruction") or payload.get("prompt"),
                "reason": payload.get("reason"),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "checkpoint": payload.get("checkpoint") or "manual_only",
                "source": payload.get("source") or "ai",
                "source_id": payload.get("source_id")
                or self.context.get("run_id")
                or self.context.get("execution_id")
                or "",
                "actor_id": payload.get("actor_id")
                or self.context.get("principal_id")
                or self.context.get("user_id")
                or "defaultspack.agent",
                "related_references": payload.get("related_references") or [],
                "dedupe_key": payload.get("dedupe_key") or "",
            },
        )
        return {
            **_project(result["deferred_steer"]),
            "deduplicated": bool(result.get("deduplicated")),
            "confirmation": "Deferred steer registered",
        }

    def update(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Edit allowed deferred-steer fields at an exact record revision."""

        return self._record_mutation(
            "deferred.update", payload, {"updates": payload.get("updates") or {}}
        )

    def checkpoint(self, payload: Mapping[str, Any]) -> List[dict[str, Any]]:
        """Make matching queued steers ready at an explicit safe checkpoint."""

        result = self._mutate(
            "deferred.checkpoint",
            {
                "checkpoint": payload.get("checkpoint"),
                "scope_type": payload.get("scope_type"),
                "scope_id": payload.get("scope_id"),
            },
        )
        return [
            _project(item)
            for item in result.get("deferred_steers") or []
            if isinstance(item, Mapping)
        ]

    def defer(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return one ready or failed steer to the queued lifecycle."""

        return self._record_mutation(
            "deferred.defer",
            payload,
            {"checkpoint": payload.get("checkpoint") or "manual_only"},
        )

    def apply(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Bind an already-created normal instruction/message to its origin steer."""

        return self._record_mutation(
            "deferred.apply",
            payload,
            {"application_reference": payload.get("application_reference") or {}},
        )

    def complete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Mark an applied steer completed without authorizing new effects."""

        return self._record_mutation("deferred.complete", payload, {})

    def dismiss(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dismiss an active steer with bounded rationale."""

        return self._record_mutation(
            "deferred.dismiss", payload, {"reason": payload.get("reason") or ""}
        )

    def fail(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record a failed normal-path application without retrying effects."""

        return self._record_mutation(
            "deferred.fail", payload, {"error": payload.get("error") or ""}
        )

    def _record_mutation(
        self,
        name: str,
        payload: Mapping[str, Any],
        extra: Mapping[str, Any],
    ) -> dict[str, Any]:
        steer_id = str(payload.get("deferred_steer_id") or payload.get("steer_id") or "")
        if not steer_id:
            raise DeferredSteerFacadeError("INVALID_INPUT", "deferred_steer_id is required")
        result = self._mutate(
            name,
            {
                "deferred_steer_id": steer_id,
                "expected_steer_revision": int(
                    payload.get("expected_steer_revision") or payload.get("revision") or 0
                ),
                **dict(extra),
            },
        )
        return _project(result["deferred_steer"])

    def _mutate(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = _invoke(
            RESOURCE,
            "deferred.list",
            {"profile_id": self.profile_id, "statuses": _ACTIVE_STATUSES},
        )
        exact = {
            "expected_revision": int(snapshot.get("revision") or 0),
            **dict(arguments),
        }
        receipt = _receipt(self.context, self.profile_id, name, exact)
        result = _invoke(
            ACTION,
            name,
            {"profile_id": self.profile_id, **exact, **receipt},
        )
        if not isinstance(result, Mapping):
            raise DeferredSteerFacadeError(
                "DEFERRED_STEER_OWNER_INVALID",
                "Deferred steer owner returned invalid data",
                503,
            )
        return dict(result)


def _receipt(
    context: Mapping[str, Any],
    profile_id: str,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    caller_id = str(
        context.get("principal_id") or context.get("user_id") or "defaultspack.local_user"
    )
    scope = {
        "service_pack_id": STATE_PACK_ID,
        "operation": f"agent.state.{name}",
        "authority": "agent.state.manage",
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": f"domain.chat.deferred_steer.{name}",
        "profile_id": profile_id,
        "workspace_id": "",
        "session_id": str(context.get("session_id") or ""),
        "arguments": dict(arguments),
        "approval_required": False,
    }
    issued = _invoke(AUTHORITY, "authorize", scope)
    if not isinstance(issued, Mapping) or not issued.get("authorized"):
        raise DeferredSteerFacadeError(
            "DEFERRED_STEER_AUTHORITY_DENIED",
            str((issued or {}).get("reason") or "Deferred steer denied"),
            403,
        )
    return {
        "authority_receipt": str(issued.get("receipt") or ""),
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": scope["caller_function_id"],
        "session_id": scope["session_id"],
    }


def _project(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_scope = value.get("scope")
    scope: Mapping[str, Any] = raw_scope if isinstance(raw_scope, Mapping) else {}
    return {
        **dict(value),
        "deferred": True,
        "prompt": str(value.get("instruction") or ""),
        "target_type": str(scope.get("type") or ""),
        "target_id": str(scope.get("id") or ""),
        "conversation_id": (
            str(scope.get("id") or "") if scope.get("type") == "conversation" else ""
        ),
        "visible": True,
        "auto_send": False,
    }


def _invoke(contract: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise DeferredSteerFacadeError(
            "DEFERRED_STEER_OWNER_UNAVAILABLE",
            "Deferred steer owner is unavailable",
            503,
        )
    return invoke_global_contract(registry, contract, operation, payload)


def _profile_id() -> str:
    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        raise DeferredSteerFacadeError(
            "DEFERRED_STEER_OWNER_UNAVAILABLE",
            "Resolved profile is unavailable",
            503,
        )
    return captured_profile_id(session)
