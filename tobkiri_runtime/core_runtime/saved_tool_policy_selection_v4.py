"""Host-private first-tool native ceremony over an actual retained Broker route."""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
import time
from typing import Any, Callable, Iterator

from tobkiri_host.ports import (
    AuthorityApprovalWindowOpenCommand,
    InteractiveApprovalGetQuery,
)


class RetainedSelectionDispatchV4:
    """Expose only a live Host-created exact native-selection dispatch port."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._ports: dict[str, Any] = {}

    @contextmanager
    def register(self, port: Any) -> Iterator[None]:
        """Retain one exact controller only while its Broker call is live."""
        key = port.selection_id
        with self._lock:
            if key in self._ports:
                raise PermissionError("native policy selection already live")
            self._ports[key] = port
        try:
            yield
        finally:
            with self._lock:
                if self._ports.get(key) is port:
                    del self._ports[key]

    def select_prepared(self, selection_id: str, invocation: Any) -> None:
        """Delegate identity authentication to the actual retained selection port."""
        invocation.assert_current()
        with self._lock:
            port = self._ports.get(selection_id)
        if port is None:
            raise PermissionError("native policy selection is not live")
        port.select_prepared(selection_id, invocation)


def commit_native_policy_selection(
    *,
    invocation: Any,
    policy: Any,
    command: Any,
    retained_port: Any,
    dispatch_port: RetainedSelectionDispatchV4,
    broker: Any,
    authority: Any,
    window: Any,
    assert_current: Callable[[], None],
    cancellation_proof: Any,
    presentation_context: Any,
    clock: Callable[[], float] = time.time,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    """Present an already captured native command, then consume its exact lease.

    The policy command was created by ``policy.request`` from an actual Broker
    prepared route. No browser selection IDs or approval flags enter this port.
    """
    assert_current()
    invocation.assert_current()
    presentation = presentation_context
    opened = window.open_authority_approval_window(
        AuthorityApprovalWindowOpenCommand(
            context=presentation,
            request_id=command.context.request_id,
            presentation_owner_principal_id=command.presentation_owner_principal_id,
            presentation_owner_session_id=command.presentation_owner_session_id,
        )
    )
    if opened != {"opened": True, "request_id": command.context.request_id}:
        raise PermissionError("native policy selection window unavailable")
    deadline = min(command.expires_at, clock() + 45)
    while clock() < deadline:
        assert_current()
        invocation.assert_current()
        status = authority.get_interactive_approval(
            InteractiveApprovalGetQuery(
                context=presentation,
                request_id=command.context.request_id,
            )
        )
        if (
            status.request_id != command.context.request_id
            or status.expires_at != command.expires_at
        ):
            raise PermissionError("native policy selection changed")
        if status.state == "approved":
            policy.record_native_approved_selection(command)
            assert_current()
            with dispatch_port.register(retained_port):
                broker.invoke_prepared(
                    policy.prepared.to_snapshot(),
                    command.context,
                    command.base_scope,
                    execution_guard=assert_current,
                    execute_not_after_wall=deadline,
                    wall_clock=clock,
                    nested_cancellation_proof=cancellation_proof,
                    cancellation_requested=invocation.envelope.cancellation_requested,
                    parent_deadline_monotonic=invocation.envelope.deadline_monotonic,
                )
            assert_current()
            retained_port.complete_after_broker()
            return
        if status.state != "pending":
            raise PermissionError("native policy selection stopped: " + status.state)
        pause(min(0.05, max(0, deadline - clock())))
    raise PermissionError("native policy selection expired")
