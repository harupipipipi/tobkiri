"""Host-private scheduling binding for one selected Calendar occurrence."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
from typing import Any, Iterator

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4


def is_calendar_effect_dispatch(
    source: Any, contract_id: str, operation_id: str, payload: Any,
) -> bool:
    """Select occurrence scheduling only; discovery retains ordinary dispatch.

    This predicate grants no authority. The selected binding, finite occurrence
    identity, capture, cancellation and deadline checks remain mandatory.
    """
    from collections.abc import Mapping
    return (
        (source.contract_id, source.operation_id) == (
            "tobkiri.action.job.v1", "rumi_job_action_broker_pack.job-action-broker",
        )
        and (contract_id, operation_id) == (
            "tobkiri.action.job.adapter.v2", "rumi_turn_runtime_pack.chat-saved-job-adapter",
        )
        and isinstance(payload, Mapping)
        and payload.get("operation") == "dispatch"
    )


@dataclass(frozen=True)
class CalendarDispatchBranch:
    """Retain actual captured source and selected signed binding, never JSON."""

    scope: CapturedInvocationScopeV4
    registry: Any
    owner: tuple[str, str]
    selected_binding: Any
    cancellation: threading.Event = field(default_factory=threading.Event)

    def assert_selected(self, scope: Any, envelope: Any) -> None:
        """Recheck the exact live source, selected target and parent ceiling."""
        if scope is not self.scope:
            raise PermissionError("Calendar source scope changed")
        scope.assert_current()
        binding = self.selected_binding
        if (
            (scope.envelope.contract_id, scope.envelope.operation_id)
            != ("tobkiri.action.job.v1", "rumi_job_action_broker_pack.job-action-broker")
            or (envelope.contract_id, envelope.operation_id)
            != ("tobkiri.action.job.adapter.v2", "rumi_turn_runtime_pack.chat-saved-job-adapter")
            or envelope.target_principal != binding.principal_ref
            or envelope.contract_version != binding.operation.contract_version
            or envelope.cancellation_requested is not self.cancellation
            or envelope.deadline_monotonic > scope.envelope.deadline_monotonic
        ):
            raise PermissionError("Calendar selected adapter changed")

    @contextmanager
    def parent_scope(self, envelope: Any, binding: Any, proof: Any) -> Iterator[Any]:
        """Enroll the actual adapter only after ordinary materialization."""
        if binding is not self.selected_binding:
            raise PermissionError("Calendar signed selected binding changed")
        self.assert_selected(self.scope, envelope)
        if proof is not None:
            with self.registry.inherited_calendar_dispatch_scope(
                proof, scope=self.scope, target_envelope=envelope,
                owner_principal=self.owner[0], owner_session=self.owner[1],
                selected_adapter_guard=self.assert_selected,
            ) as parent:
                yield parent
            return
        with self.registry.private_calendar_dispatch_scope(
            scope=self.scope, target_envelope=envelope,
            owner_principal=self.owner[0], owner_session=self.owner[1],
            selected_adapter_guard=self.assert_selected,
        ) as parent:
            yield parent
