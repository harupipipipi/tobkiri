"""Host-private finite scheduling scope for one captured saved message tool.

This scope changes scheduling only. It carries no authority, session, Grant or
Provider call and is never selected by an application payload flag.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4

BROKER = ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
SAVED = ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
GUEST = ("conversation.saved-turn.v1", "saved_complete")
CAPTURE = (
    "profile_id",
    "profile_revision",
    "activation_id",
    "activation_digest",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
)
# Signed Host operations in the finite chat tool chain. Selection here does not
# bypass the normal Contract binding, local policy, lease or approval checks.
CONTRACTS = frozenset(
    {
        "tobkiri.service.tool.invoke.v1",
        "tobkiri.resource.tool.definition.v1",
        "tobkiri.service.tool.arguments.validate.v1",
        "tobkiri.service.tool.execute.v1",
        "tobkiri.service.tool.result.normalize.v1",
        "tobkiri.service.tool.local.operation.v1",
        "tobkiri.service.interactive-effect.v1",
        "tobkiri.service.chat.message.send.v1",
        "tobkiri.resource.chat.reference.v1",
        "tobkiri.resource.conversation.v1",
        "tobkiri.resource.turn.v1",
        "tobkiri.action.chat.message.delivery.v1",
        "tobkiri.action.turn.saved.v1",
        "tobkiri.resource.workspace.mount.v1",
        "tobkiri.service.file.create.v1",
        "tobkiri.action.turn.progress.v1",
        "tobkiri.resource.turn.progress.v1",
    }
)


@dataclass
class _FiniteChatDispatch:
    scope: CapturedInvocationScopeV4
    tool_call_id: str
    owner: tuple[str, str]
    source_proof: Any
    root: Any = None
    active: bool = True
    candidate_witness: Any = None
    cleanup: list[Any] = field(default_factory=list)
    steps: int = 0
    lock: Any = field(default_factory=threading.Lock)

    def bind_candidate(self, witness: Any) -> None:
        """Retain an inert admission witness for this exact live source."""
        with self.lock:
            if not self.active or witness.source_broker_envelope is not self.root:
                raise PermissionError("finite candidate source changed")
            if self.candidate_witness is not None:
                raise PermissionError("finite candidate is already bound")
            self.candidate_witness = witness

    def permits(self, envelope: Any, proof: Any, parent_scope: Any) -> bool:
        """Select only this source tree after normal Broker authorization."""
        with self.lock:
            return self._permits_locked(envelope, proof, parent_scope)

    def _permits_locked(self, envelope: Any, proof: Any, parent_scope: Any) -> bool:
        if not self.active:
            raise PermissionError("finite message source has ended")
        self.scope.assert_current()
        if proof is None or envelope.contract_id not in CONTRACTS:
            return False
        if not all(
            getattr(envelope.context, key) == getattr(self.scope.envelope.context, key)
            for key in CAPTURE
        ):
            raise PermissionError("finite message scheduling capture changed")
        # Proof is an actual Host registry object, validated by Broker before
        # reaching this hook. Its parent must be in this live registered tree.
        if not isinstance(
            parent_scope, CapturedInvocationScopeV4
        ) or not proof.matches_invocation(parent_scope.envelope, *self.owner):
            raise PermissionError("finite message scheduling parent is unavailable")
        parent_scope.assert_current()
        if self.root is None:
            if (
                not proof.matches_invocation(self.scope.envelope, *self.owner)
                or (envelope.contract_id, envelope.operation_id) != BROKER
                or envelope.payload.get("tool_id") != "chat_send_message"
                or envelope.payload.get("tool_call_id") != self.tool_call_id
            ):
                return False
            self.root = envelope
        else:
            scope: CapturedInvocationScopeV4 | None = parent_scope
            seen: set[int] = set()
            found = False
            while scope is not None:
                if (
                    not isinstance(scope, CapturedInvocationScopeV4)
                    or id(scope) in seen
                    or len(seen) >= 16
                ):
                    raise PermissionError(
                        "finite message scheduling ancestry is invalid"
                    )
                seen.add(id(scope))
                scope.assert_current()
                if not all(
                    getattr(scope.envelope.context, key)
                    == getattr(self.scope.envelope.context, key)
                    for key in CAPTURE
                ):
                    raise PermissionError(
                        "finite message scheduling ancestor capture changed"
                    )
                found |= scope.envelope is self.root
                scope = scope.parent
            if not found:
                raise PermissionError("finite message scheduling source root changed")
        self.steps += 1
        if self.steps > 16384:
            raise PermissionError("finite message scheduling budget exhausted")
        return True


_current: contextvars.ContextVar[_FiniteChatDispatch | None] = contextvars.ContextVar(
    "finite_chat_dispatch",
    default=None,
)


def select_inline_chat_dispatch(envelope: Any, proof: Any, parent_scope: Any) -> bool:
    """Read the current Host-only scheduling scope after all ordinary guards."""
    token = _current.get()
    return token is not None and token.permits(envelope, proof, parent_scope)


def _assert_saved_source_ancestry(
    scope: CapturedInvocationScopeV4, proof: Any, owner: tuple[str, str],
) -> tuple[CapturedInvocationScopeV4, bool]:
    """Validate actual ordinary or selected Calendar source; mint no authority."""
    scope.assert_current()
    if (scope.envelope.contract_id, scope.envelope.operation_id) != GUEST:
        raise PermissionError("finite message source is not a saved guest")
    parent = scope.parent
    calendar = False
    direct_calendar = (
        isinstance(parent, CapturedInvocationScopeV4)
        and (parent.envelope.contract_id, parent.envelope.operation_id) == (
            "tobkiri.action.job.adapter.v2",
            "rumi_turn_runtime_pack.chat-saved-job-adapter",
        )
    )
    if isinstance(parent, CapturedInvocationScopeV4) and parent.parent is not None:
        from tobkiri_host.operation_cancellation import _NestedCancellationProof
        adapter = parent if direct_calendar else parent.parent
        job = adapter.parent if isinstance(adapter, CapturedInvocationScopeV4) else None
        witness = getattr(proof, "_calendar_execution_witness", None)
        if (
            type(proof) is _NestedCancellationProof
            and callable(witness)
            and isinstance(adapter, CapturedInvocationScopeV4)
            and isinstance(job, CapturedInvocationScopeV4)
            and (adapter.envelope.contract_id, adapter.envelope.operation_id) == (
                "tobkiri.action.job.adapter.v2", "rumi_turn_runtime_pack.chat-saved-job-adapter")
            and (job.envelope.contract_id, job.envelope.operation_id) == (
                "tobkiri.action.job.v1", "rumi_job_action_broker_pack.job-action-broker")
            and adapter.envelope.contract_version == "2.0.0"
            and job.envelope.payload.get("operation") == "dispatch"
            and owner == (proof._owner_principal, proof._owner_session)
            and all(getattr(ancestor.envelope.context, key) == getattr(scope.envelope.context, key)
                    for ancestor in (adapter, job) for key in CAPTURE)
        ):
            witness()
            if direct_calendar:
                from concurrent.futures import Future

                if proof._envelope is not adapter.envelope:
                    raise PermissionError("Calendar source root changed")
                with proof._registry._lock:
                    if not any(
                        child.envelope is scope.envelope
                        and isinstance(child.future, Future)
                        and child.future.running()
                        and not child.completed
                        for child in proof._children.values()
                    ):
                        raise PermissionError("Calendar source Future unavailable")
            adapter.assert_current()
            job.assert_current()
            calendar = True
    if (
        not isinstance(parent, CapturedInvocationScopeV4)
        or ((parent.envelope.contract_id, parent.envelope.operation_id) != SAVED
            and not (direct_calendar and calendar))
        or (parent.parent is not None and not calendar)
        or proof is None
        or not proof.matches_invocation(scope.envelope, *owner)
    ):
        raise PermissionError("finite message source ancestry is unavailable")
    parent.assert_current()
    source = parent.public_payload().get("request", {})
    guest = scope.public_payload().get("request", {})
    if direct_calendar:
        from tobkiri_protocol.canonical import canonical_digest

        values = parent.public_payload()
        task = values.get("payload", {})
        key = values.get("idempotency_key")
        profile = parent.envelope.context.profile_id
        expected_turn = "calendar:" + canonical_digest(
            [profile, values.get("schedule_id"), key]
        ).removeprefix("sha256:")
        expected_conversation = task.get("conversation_id") or (
            "calendar:" + canonical_digest([profile, key]).removeprefix("sha256:")
        )
        source = {"conversation_id": expected_conversation, "turn_id": expected_turn}
    if any(
        not isinstance(source.get(key), str)
        or not source[key]
        or guest.get(key) != source[key]
        for key in ("conversation_id", "turn_id")
    ):
        raise PermissionError("finite message source saved identity changed")
    root_context = parent.envelope.context
    if not calendar and owner != (root_context.caller_principal.value, root_context.caller_session_id):
        raise PermissionError("finite message source owner changed")
    if not all(
        getattr(scope.envelope.context, key) == getattr(root_context, key)
        for key in CAPTURE
    ):
        raise PermissionError("finite message source capture changed")
    return parent, calendar


@contextmanager
def finite_saved_chat_source(
    scope: CapturedInvocationScopeV4,
    proof: Any,
    payload: Any,
    owner: tuple[str, str],
) -> Iterator[None]:
    """Mint one finite candidate scope from the actual sealed saved callback."""
    if _current.get() is not None:
        # Incoming callbacks retain their original exact source tree; no child
        # can mint a new root to escape its Broker/cascade or approval guards.
        yield
        return
    _assert_saved_source_ancestry(scope, proof, owner)
    if payload.get("tool_id") != "chat_send_message":
        raise PermissionError("finite message source tool changed")
    from tobkiri_protocol.chat_message_v1 import message_arguments

    message_arguments(payload.get("arguments"))
    call_id = payload.get("tool_call_id")
    if not isinstance(call_id, str) or not call_id:
        raise PermissionError("finite message source call is unavailable")
    dispatch = _FiniteChatDispatch(scope, call_id, owner, proof)
    token = _current.set(dispatch)
    try:
        yield
        scope.assert_current()
    finally:
        with dispatch.lock:
            dispatch.active = False
            cleanup = tuple(dispatch.cleanup)
            dispatch.cleanup.clear()
        _current.reset(token)
        for close in cleanup:
            close()
