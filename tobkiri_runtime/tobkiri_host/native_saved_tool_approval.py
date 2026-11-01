"""Native confirmation for exact Host-prepared saved tool invocations."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
import time
from typing import Any, Callable, Mapping

from tobkiri_host.ports import (
    AuthorityApprovalWindowOpenCommand,
    InteractiveApprovalGetQuery,
    InteractiveApprovalGrantAttestation,
    InteractiveApprovalRequestCommand,
)
from tobkiri_protocol.canonical import canonical_digest


@dataclass(frozen=True)
class PreparedNativeToolApproval:
    """Host Broker-prepared exact command and no-material Grant assertion.

    Preparation must occur in Host TCB against the original invocation and
    actual resolved execution binding; never manufacture scope from wire data.
    """

    command: InteractiveApprovalRequestCommand
    attestation: InteractiveApprovalGrantAttestation
    operation_digest: str
    presentation_context: Any = None


class NativeSavedToolApprovalGate:
    """Request, display and consume one actual native decision before dispatch."""

    def __init__(
        self,
        *,
        authority: Any,
        window: Any,
        prepare: Callable[[Any, Mapping[str, Any], Mapping[str, Any]], PreparedNativeToolApproval],
        timeout_seconds: float = 45,
        clock: Callable[[], float] = time.time,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        if authority is None or window is None or not 0 < timeout_seconds <= 45:
            raise PermissionError("native saved tool approval is unavailable")
        self._authority, self._window, self._prepare = authority, window, prepare
        self._timeout, self._clock, self._pause = timeout_seconds, clock, pause
        self._lock = RLock()
        self._claimed: set[str] = set()

    def __call__(
        self,
        invocation: Any,
        execution: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        """Require a fresh exact durable decision; cancellation fails closed."""
        invocation.assert_current()
        digest = canonical_digest({"execution": dict(execution), "payload": dict(payload)})
        prepared = self._prepare(invocation, execution, payload)
        if (
            not isinstance(prepared, PreparedNativeToolApproval)
            or prepared.operation_digest != digest
        ):
            raise PermissionError("native saved tool preparation changed")
        command, attestation = prepared.command, prepared.attestation
        presentation = prepared.presentation_context or command.context
        if (
            command.context.request_id != attestation.request_id
            or any(
                getattr(command.context, key) != getattr(attestation.context, key)
                for key in (
                    "profile_id",
                    "profile_revision",
                    "activation_id",
                    "activation_digest",
                    "plan_digest",
                    "profile_authority_digest",
                    "security_epoch",
                    "fencing_token",
                    "caller_principal",
                    "caller_session_id",
                    "caller_domain_id",
                    "caller_boot_epoch",
                    "target_domain_id",
                    "target_boot_epoch",
                    "target_backend_digest",
                    "handle_namespace",
                )
            )
            or command.expires_at <= self._clock()
            or command.caller_publisher_lineage != attestation.caller_publisher_lineage
            or command.target_publisher_lineage != attestation.target_publisher_lineage
            or command.request_digest != attestation.request_digest
            or command.target_principal != attestation.target_principal
            or command.base_scope != attestation.base_scope
            or command.invocation_owner_id != attestation.invocation_owner_id
            or command.expires_at != attestation.expires_at
            or command.presentation_owner_principal_id != invocation.presentation_owner_principal_id
            or command.presentation_owner_session_id != invocation.presentation_owner_session_id
            or command.redacted_metadata.get("operation_digest") != digest
        ):
            raise PermissionError("native saved tool approval binding changed")
        with self._lock:
            if attestation.request_id in self._claimed:
                raise PermissionError("native saved tool approval was already claimed")
            # Reserve before opening: concurrent retries cannot create or claim
            # another decision for this prepared request. Failure stays spent.
            self._claimed.add(attestation.request_id)
        invocation.assert_current()
        status = self._authority.request_interactive_approval(command)
        if status.request_id != attestation.request_id or status.state != "pending":
            raise PermissionError("native saved tool approval is unavailable")
        opened = self._window.open_authority_approval_window(
            AuthorityApprovalWindowOpenCommand(
                context=presentation,
                request_id=attestation.request_id,
                presentation_owner_principal_id=command.presentation_owner_principal_id,
                presentation_owner_session_id=command.presentation_owner_session_id,
            )
        )
        if opened != {"opened": True, "request_id": attestation.request_id}:
            raise PermissionError("native saved tool approval window is unavailable")
        end = min(command.expires_at, self._clock() + self._timeout)
        while end - self._clock() > 0.000001:
            invocation.assert_current()
            status = self._authority.get_interactive_approval(
                InteractiveApprovalGetQuery(context=presentation, request_id=attestation.request_id)
            )
            if (
                status.request_id != attestation.request_id
                or status.expires_at != command.expires_at
            ):
                raise PermissionError("native saved tool approval changed")
            if status.state == "approved":
                self._authority.assert_interactive_approval_grant(attestation)
                invocation.assert_current()
                if (
                    self._clock() >= end
                    or canonical_digest({"execution": dict(execution), "payload": dict(payload)})
                    != digest
                ):
                    raise PermissionError("native saved tool approval changed")
                return
            if status.state != "pending":
                raise PermissionError(f"native saved tool approval stopped: {status.state}")
            self._pause(min(0.1, max(0, end - self._clock())))
        raise PermissionError("native saved tool approval expired")
