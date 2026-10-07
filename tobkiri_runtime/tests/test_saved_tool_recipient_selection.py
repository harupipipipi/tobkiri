"""Registry-backed recipient selection units; no captured approval UI claim."""

from concurrent.futures import Future
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from types import SimpleNamespace
from threading import Event
from typing import Any, Iterator
import time

import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4 as Scope
from core_runtime import owned_chat_message_approval_v4 as approval
from ecosystem.rumi_tool_broker_pack.runtime import chat_delivery as delivery
from tests.test_operation_cancellation import _envelope, _child_envelope, _binding
from tobkiri_host import finite_chat_dispatch as finite
from tobkiri_host import operation_cancellation as _REGISTRY
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.saved_tool_request_scope import saved_tool_request_scope as select
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.chat_message_v1 import create_plan


def running(
    proof: _REGISTRY._NestedCancellationProof, envelope: RequestEnvelope
) -> Future[object]:
    """Enroll a genuinely RUNNING Future through the existing registry API."""
    future = Future()
    future.set_running_or_notify_cancel()
    proof.bind_child(proof.reserve_child(envelope), future)
    return future


@contextmanager
def recipient(public_track: bool = False) -> Iterator[SimpleNamespace]:
    """Build the real private source/independent recipient cancellation join."""
    registry = OwnedCancellationHandles()
    saved = replace(
        _envelope(),
        contract_id=delivery.SAVED[0],
        operation_id=delivery.SAVED[1],
        payload={
            "request": {
                "conversation_id": "source",
                "turn_id": "source-turn",
                "action_approval_mode": "full",
            }
        },
    )
    owner = (saved.context.caller_principal.value, saved.context.caller_session_id)
    live = [True]

    def guard() -> None:
        if not live[0]:
            raise PermissionError("original owner revoked")

    def child(
        parent: Scope, route: tuple[str, str], payload: dict[str, Any] | None = None
    ) -> Scope:
        envelope = replace(
            _child_envelope(parent.envelope),
            contract_id=route[0],
            operation_id=route[1],
            payload=payload or {},
        )
        return Scope(envelope, guard, parent)

    with _binding(
        registry,
        saved,
        "execute",
        owner_principal=owner[0],
        owner_session=owner[1],
        guard=guard,
    ).track("source-turn"):
        proof = nested_cancellation_proof_for(saved, *owner)
        source = Scope(saved, guard)
        guest = child(source, finite.GUEST, saved.payload)
        source_guest_future = running(proof, guest.envelope)
        payload = {
            "tool_id": "chat_send_message",
            "tool_call_id": "call",
            "arguments": {
                "target_kind": "chat",
                "target_id": "target",
                "content": "Hello",
                "delivery": "steer",
            },
        }
        with finite.finite_saved_chat_source(guest, proof, payload, owner):
            broker = child(guest, delivery.BROKER, payload)
            assert finite.select_inline_chat_dispatch(broker.envelope, proof, guest)
            source_broker_future = running(proof, broker.envelope)
            executor = child(broker, delivery.EXECUTOR)
            local = child(executor, delivery.LOCAL)
            invocation = SimpleNamespace(
                envelope=local.envelope,
                parent_invocation=executor,
                assert_current=guard,
                presentation_owner_principal_id=owner[0],
                presentation_owner_session_id=owner[1],
            )
            request = approval.register_chat_message_tool_request(
                {
                    **payload["arguments"],
                    "source_conversation_id": "source",
                    "source_turn_id": "source-turn",
                    "profile_id": saved.context.profile_id,
                    "tool_call_id": "call",
                },
                invocation,
            )
            snapshot = {
                "kind": "tobkiri.chat.reference.snapshot.v1",
                "profile_id": saved.context.profile_id,
                "store_revision": 1,
                "project_revision": 1,
                "snapshot_time": int(time.time() * 1000),
                "expires_at": int(time.time() * 1000) + 90000,
                "next_cursor": None,
                "truncated": False,
                "references": [
                    {
                        "kind": "chat",
                        "id": "target",
                        "label": "Target",
                        "conversation_ids": ["target"],
                        "snapshot_digest": "digest",
                        "member_count": 1,
                        "membership_complete": True,
                    }
                ],
            }
            plan = create_plan(request, snapshot)
            bound = {"request": request, "plan": plan}
            effect = child(local, delivery.EFFECT)
            execute = child(effect, delivery.EXECUTE, bound)
            approval.bind_chat_message_effect(
                "effect", request, saved.context, lambda: None, plan=plan
            )
            approval.bind_chat_message_execute_authority(
                request,
                plan,
                SimpleNamespace(envelope=execute.envelope, assert_current=guard),
            )
            incoming = child(execute, delivery.DELIVERY, bound)
            delivery_id = (
                "message-delivery:"
                + canonical_digest(
                    [request["profile_id"], "source-turn", "call", "target"]
                )[7:]
            )
            target_request = {
                "conversation_id": "target",
                "turn_id": "delivery:"
                + canonical_digest([request["profile_id"], delivery_id, "target"])[7:],
                "content": "Hello",
                "action_approval_mode": "ask",
            }
            target_saved = Scope(
                replace(
                    _child_envelope(saved),
                    contract_id=delivery.SAVED[0],
                    operation_id=delivery.SAVED[1],
                    cancellation_requested=Event(),
                    payload={"request": target_request},
                ),
                guard,
                incoming,
            )
            root_future = Future()
            root_future.set_running_or_notify_cancel()
            slot = proof.reserve_independent_saved_child(target_saved.envelope)
            proof.bind_child(slot, root_future)
            try:
                with proof.independent_saved_execution_scope(target_saved.envelope):
                    with ExitStack() as public_stack:
                        if public_track:
                            public_stack.enter_context(
                                _binding(
                                    registry,
                                    target_saved.envelope,
                                    "execute",
                                    owner_principal=owner[0],
                                    owner_session=owner[1],
                                    guard=guard,
                                ).track("public-target-turn")
                            )
                        target_proof = nested_cancellation_proof_for(
                            target_saved.envelope, *owner
                        )
                        target_guest = child(
                            target_saved,
                            finite.GUEST,
                            {"request": dict(target_request)},
                        )
                        target_broker = child(target_guest, delivery.BROKER)
                        target_executor = child(target_broker, delivery.EXECUTOR)
                        futures = [
                            running(target_proof, s.envelope)
                            for s in (target_guest, target_broker, target_executor)
                        ]
                        retained = (target_broker, target_guest, target_saved, incoming)

                        def parent_guard(scopes: tuple[Scope, ...]) -> None:
                            guard()
                            if any(a is not b for a, b in zip(scopes, retained)):
                                raise PermissionError("original Host chain changed")

                        target = SimpleNamespace(
                            envelope=target_executor.envelope,
                            parent_invocation=target_broker,
                            assert_current=guard,
                            presentation_owner_principal_id=owner[0],
                            presentation_owner_session_id=owner[1],
                            _assert_saved_tool_parent_scopes=parent_guard,
                        )
                        try:
                            yield SimpleNamespace(
                                invocation=target,
                                guest=target_guest,
                                broker=target_broker,
                                saved=target_saved,
                                source=guest,
                                root_future=root_future,
                                futures=futures,
                                proof=target_proof,
                                source_futures=[
                                    source_guest_future,
                                    source_broker_future,
                                ],
                                live=live,
                                request=request,
                            )
                        finally:
                            for future in futures:
                                if not future.done():
                                    future.set_result(None)
                                target_proof.record_resource_drain(future)

            finally:
                live[0] = True
                approval.close_chat_message_tool_request(request["invocation_key"])
                for future in [root_future, source_guest_future, source_broker_future]:
                    if not future.done():
                        future.set_result(None)
                    proof.record_resource_drain(future)


def test_recipient_ask_isolation_and_owner_projection() -> None:
    """Select enrolled recipient ASK while preserving the elevated source data."""
    with recipient() as case:
        case.guest.envelope.payload["request"].update(
            task_context={"owner": "projection"}, resolved_chat_references=[]
        )
        assert select(case.invocation) is case.guest
        assert case.guest.public_payload()["request"]["action_approval_mode"] == "ask"
        assert case.source.public_payload()["request"]["action_approval_mode"] == "full"


@pytest.mark.parametrize(
    "failure",
    [
        "root",
        "guest",
        "broker",
        "executor",
        "source-root",
        "revoked",
        "cancelled",
        "foreign",
        "clone",
        "reparent",
        "native-record",
        "changed-mode",
    ],
)
def test_recipient_rejects_liveness_identity_and_approval_changes(failure: str) -> None:
    """Do not use source preferences when the real recipient branch is invalid."""
    with recipient() as case:
        if failure == "root":
            case.root_future.set_result(None)
        elif failure in ("guest", "broker", "executor"):
            case.futures[["guest", "broker", "executor"].index(failure)].set_result(
                None
            )
        elif failure == "source-root":
            case.source_futures[1].set_result(None)
        elif failure == "revoked":
            case.live[0] = False
        elif failure == "cancelled":
            case.proof.request()
        elif failure == "foreign":
            case.invocation.presentation_owner_session_id = "foreign"
        elif failure == "clone":
            case.invocation.envelope = replace(case.invocation.envelope)
        elif failure == "reparent":
            case.invocation.parent_invocation = Scope(
                case.broker.envelope, lambda: None, case.broker.parent
            )
        elif failure == "native-record":
            approval.close_chat_message_tool_request(case.request["invocation_key"])
        elif failure == "changed-mode":
            case.guest.envelope.payload["request"]["action_approval_mode"] = "full"
        with pytest.raises(PermissionError):
            select(case.invocation)


def test_ordinary_one_guest_keeps_accepted_scope() -> None:
    """Ordinary selection does not acquire recipient or native approval authority."""
    saved = Scope(_envelope(), lambda: None)
    guest = Scope(
        replace(
            _child_envelope(saved.envelope),
            contract_id=finite.GUEST[0],
            operation_id=finite.GUEST[1],
            payload={"request": {"action_approval_mode": "ask"}},
        ),
        lambda: None,
        saved,
    )
    invocation = SimpleNamespace(parent_invocation=guest, assert_current=lambda: None)
    assert select(invocation) is guest


@pytest.mark.parametrize(
    "failure",
    [
        "clone-guest",
        "clone-saved",
        "extra-prefix",
        "duplicate",
        "absent-proof",
        "claimed-resolved",
    ],
)
def test_rejects_scope_replacement_and_unowned_projection(failure: str) -> None:
    """Original Host parent identity and native registry remain necessary."""
    with recipient() as case:
        broker = case.broker
        guest = case.guest
        if failure == "clone-guest":
            guest = Scope(guest.envelope, lambda: None, guest.parent)
            broker = Scope(broker.envelope, lambda: None, guest)
        elif failure == "clone-saved":
            saved = Scope(case.saved.envelope, lambda: None, case.saved.parent)
            guest = Scope(guest.envelope, lambda: None, saved)
            broker = Scope(broker.envelope, lambda: None, guest)
        elif failure == "extra-prefix":
            broker = Scope(_envelope(), lambda: None, broker)
        elif failure == "duplicate":
            broker = Scope(guest.envelope, lambda: None, broker)
        elif failure == "absent-proof":
            case.invocation.envelope = _envelope()
        elif failure == "claimed-resolved":
            case.saved.envelope.payload["request"]["resolved_chat_references"] = []
        case.invocation.parent_invocation = broker
        with pytest.raises(PermissionError):
            select(case.invocation)


@pytest.mark.parametrize("missing_link", [False, True])
def test_public_target_track_requires_actual_private_parent(missing_link: bool) -> None:
    """A genuine public tracked proof must retain its entered Saved parent."""
    with recipient(public_track=True) as case:
        assert case.proof._saved_execution_parent is not None
        if missing_link:
            case.proof._saved_execution_parent = None
            with pytest.raises(PermissionError, match="track linkage"):
                select(case.invocation)
        else:
            assert select(case.invocation) is case.guest


def test_same_root_typed_parent_copy_cannot_replace_entered_proof() -> None:
    """Payload, envelope, owner and registry equality do not confer linkage."""
    with recipient(public_track=True) as case:
        entered = case.proof._saved_execution_parent
        forged = _REGISTRY._NestedCancellationProof(
            entered._registry,
            entered._key,
            entered._envelope,
            entered._owner_principal,
            entered._owner_session,
        )
        case.proof._saved_execution_parent = forged
        with pytest.raises(PermissionError, match="track linkage"):
            select(case.invocation)


def test_ordinary_different_root_track_does_not_inherit_saved_link() -> None:
    """A separately tracked root stays independent of a selected Saved branch."""
    with recipient(public_track=True) as case:
        other = _envelope()
        owner = (
            case.invocation.presentation_owner_principal_id,
            case.invocation.presentation_owner_session_id,
        )
        with _binding(
            case.proof._registry,
            other,
            "execute",
            owner_principal=owner[0],
            owner_session=owner[1],
        ).track("other"):
            proof = nested_cancellation_proof_for(other, *owner)
            assert proof._saved_execution_parent is None


def test_copied_public_proof_cannot_borrow_registered_identity() -> None:
    """Even the exact private parent and real children cannot replace the record."""
    with recipient(public_track=True) as case:
        original = case.proof
        copied = _REGISTRY._NestedCancellationProof(
            original._registry,
            original._key,
            original._envelope,
            original._owner_principal,
            original._owner_session,
        )
        copied._saved_execution_parent = original._saved_execution_parent
        copied._children = dict(original._children)
        copied._execution_guard = original._execution_guard
        assert original._registry._records[original._key] is original
        token = _REGISTRY._active_nested_cancellation_proof.set(copied)
        try:
            assert (
                nested_cancellation_proof_for(
                    case.invocation.envelope,
                    case.invocation.presentation_owner_principal_id,
                    case.invocation.presentation_owner_session_id,
                )
                is None
            )
            with pytest.raises(PermissionError, match="recipient proof"):
                select(case.invocation)
        finally:
            _REGISTRY._active_nested_cancellation_proof.reset(token)


@pytest.mark.parametrize(
    "consumer", ["default", "composed", "policy-mode", "consent", "owner", "elevated"]
)
def test_consumer_boundaries_keep_recipient_preference_and_owner(consumer: str) -> None:
    """All finite consumers read recipient ASK rather than source FULL data."""
    from core_runtime.saved_tool_capture_v4 import (
        assert_saved_tool_requested_mode,
        capture_saved_tool_requested_mode,
    )
    from core_runtime.saved_tool_policy_context_v4 import (
        capture_saved_tool_policy_context,
    )
    from tobkiri_host.saved_tool_consent import _turn_id
    from tobkiri_host.saved_tool_context import saved_tool_owner
    from tobkiri_host.saved_tool_policy_execution import requested_saved_tool_mode

    with recipient(public_track=True) as case:
        target_turn = case.saved.public_payload()["request"]["turn_id"]
        if consumer == "default":
            assert assert_saved_tool_requested_mode(case.invocation) == "ask"
        elif consumer == "composed":
            assert capture_saved_tool_requested_mode(case.invocation) == "ask"
        elif consumer == "policy-mode":
            assert requested_saved_tool_mode(case.invocation) == ("ask", target_turn)
        elif consumer == "consent":
            assert _turn_id(case.invocation) == target_turn
        elif consumer == "owner":
            owner = saved_tool_owner(case.invocation)
            assert owner.caller_principal.value == (
                case.invocation.presentation_owner_principal_id
            )
            assert owner.caller_session_id == (
                case.invocation.presentation_owner_session_id
            )
        else:
            reads: list[str] = []

            def read_conversation(invocation: Any, conversation: str) -> dict[str, Any]:
                reads.append("conversation")
                pytest.fail("ASK must fail before any conversation read")

            def resolve_workspace(profile: str, workspace: str) -> Any:
                reads.append("workspace")
                pytest.fail("ASK must fail before any workspace read")

            def selected_workspace() -> dict[str, Any]:
                reads.append("selected")
                pytest.fail("ASK must fail before selected workspace read")

            with pytest.raises(PermissionError, match="elevated request"):
                capture_saved_tool_policy_context(
                    case.invocation,
                    profile_id=case.saved.envelope.context.profile_id,
                    read_conversation=read_conversation,
                    resolve_workspace=resolve_workspace,
                    selected_workspace=selected_workspace,
                )
            assert reads == []
        assert case.source.public_payload()["request"]["action_approval_mode"] == "full"


@pytest.mark.parametrize("gate", ["default", "composed", "consent", "policy"])
def test_one_original_selector_per_paired_gate(
    gate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each boundary calls the actual selector once without changing its result."""
    from core_runtime.saved_tool_capture_v4 import (
        assert_saved_tool_requested_mode,
        capture_saved_tool_requested_mode,
    )
    from tobkiri_host import saved_tool_request_scope as selector
    from tobkiri_host.saved_tool_consent import SavedToolConsentExecution
    from tobkiri_host.saved_tool_policy_execution import SavedToolApprovalExecution

    original = selector.saved_tool_request_scope
    calls: list[Any] = []

    def observed(invocation: Any) -> Scope:
        calls.append(invocation)
        return original(invocation)

    monkeypatch.setattr(selector, "saved_tool_request_scope", observed)
    with recipient(public_track=True) as case:
        if gate == "default":
            assert assert_saved_tool_requested_mode(case.invocation) == "ask"
        elif gate == "composed":
            assert capture_saved_tool_requested_mode(case.invocation) == "ask"
        elif gate == "consent":
            # Unsupported input fails after the real entry gate, before effects.
            consent = object.__new__(SavedToolConsentExecution)
            with pytest.raises(PermissionError, match="read route is unsupported"):
                consent(case.invocation, {}, {})
        else:
            delegated: list[Any] = []

            def ask(invocation: Any, execution: Any, payload: Any) -> dict[str, Any]:
                delegated.append(invocation)
                return {"route": "ask"}

            policy = object.__new__(SavedToolApprovalExecution)
            policy._ask = ask
            assert policy(case.invocation, {}, {}) == {"route": "ask"}
            assert delegated == [case.invocation]
        assert calls == [case.invocation]


@pytest.mark.parametrize(
    "changed", ["revoked", "cancelled", "native-expired", "request", "scope"]
)
def test_new_paired_gate_rechecks_live_record_and_exact_request(changed: str) -> None:
    """A prior successful pair cannot authenticate a subsequent changed call."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    with recipient(public_track=True) as case:
        owner, accepted = saved_tool_owner_and_request_scope(case.invocation)
        assert accepted is case.guest
        assert (
            owner.caller_principal.value
            == case.invocation.presentation_owner_principal_id
        )
        assert accepted.public_payload()["request"]["action_approval_mode"] == "ask"
        assert case.source.public_payload()["request"]["action_approval_mode"] == "full"
        if changed == "revoked":
            case.live[0] = False
        elif changed == "cancelled":
            case.proof.request()
        elif changed == "native-expired":
            with approval._live_lock:
                approval._live_requests[case.request["invocation_key"]].deadline = (
                    time.monotonic() - 1
                )
        elif changed == "request":
            case.guest.envelope.payload["request"]["content"] = "changed"
        else:
            case.invocation.parent_invocation = Scope(
                case.broker.envelope, lambda: None, case.broker.parent
            )
        with pytest.raises(PermissionError):
            saved_tool_owner_and_request_scope(case.invocation)


@pytest.mark.parametrize("change", ["close", "replace-equal"])
def test_live_record_changed_during_original_source_guard_rejects(change: str) -> None:
    """Post-call exact identity rejects drain and equal-field replacement."""
    with recipient(public_track=True) as case:
        key = case.request["invocation_key"]
        with approval._live_lock:
            record = approval._live_requests[key]
        original_guard = record.guard
        called: list[str] = []

        def mutate_after_original() -> None:
            original_guard()
            called.append(change)
            if change == "close":
                approval.close_chat_message_tool_request(key)
            else:
                with approval._live_lock:
                    approval._live_requests[key] = replace(record)

        record.guard = mutate_after_original
        with pytest.raises(PermissionError, match="invocation drained"):
            approval.assert_chat_message_request_live(
                case.request, case.source.envelope.context
            )
        assert called == [change]


def test_private_clock_crossing_guard_does_not_refresh_or_add_end_time_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve pretime/postidentity semantics and reject the next real gate."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    with recipient(public_track=True) as case:
        key = case.request["invocation_key"]
        record = approval._live_requests[key]
        deadline = record.deadline
        now = [deadline - 1]
        # Only this module's private clock is controlled; parent/lease clocks
        # and their original guards remain real and unchanged.
        monkeypatch.setattr(
            approval, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time)
        )
        original_guard = record.guard
        calls: list[float] = []

        def advance_after_original() -> None:
            original_guard()
            calls.append(now[0])
            now[0] = deadline + 1

        record.guard = advance_after_original
        approval.assert_chat_message_request_live(
            case.request, case.source.envelope.context
        )
        assert calls == [deadline - 1]
        assert record.deadline == deadline
        assert approval._live_requests[key] is record
        with pytest.raises(PermissionError, match="original invocation is unavailable"):
            saved_tool_owner_and_request_scope(case.invocation)
        assert calls == [deadline - 1]  # Expired next gate never enters source guard.
        assert record.deadline == deadline


@pytest.mark.parametrize("change", ["source", "accepted-request"])
def test_guard_callback_input_mutation_is_rejected_before_pair_returns(
    change: str,
) -> None:
    """Original source validation and recipient digest bind callback mutations."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    with recipient(public_track=True) as case:
        record = approval._live_requests[case.request["invocation_key"]]
        original_guard = record.guard

        def mutate_before_original() -> None:
            if change == "source":
                case.source.envelope.payload["request"]["conversation_id"] = "other"
            else:
                case.guest.envelope.payload["request"]["content"] = "changed"
            original_guard()

        record.guard = mutate_before_original
        with pytest.raises(PermissionError):
            saved_tool_owner_and_request_scope(case.invocation)


@pytest.mark.parametrize("change", ["revoked", "cancelled", "native-expired"])
def test_policy_to_real_consent_boundary_revalidates_after_callback_change(
    change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh consent entry rejects changes after a successful policy gate."""
    from tobkiri_host import saved_tool_request_scope as selector
    from tobkiri_host.saved_tool_consent import SavedToolConsentExecution
    from tobkiri_host.saved_tool_policy_execution import SavedToolApprovalExecution

    original = selector.saved_tool_request_scope
    selectors: list[Any] = []

    def observe(invocation: Any) -> Scope:
        selectors.append(invocation)
        return original(invocation)

    monkeypatch.setattr(selector, "saved_tool_request_scope", observe)
    from tobkiri_host import saved_tool_consent, saved_tool_policy_execution

    pairs: list[str] = []
    original_pair = saved_tool_consent.saved_tool_owner_and_request_scope

    def count_pair(module: Any, label: str) -> None:
        def observed_pair(invocation: Any) -> Any:
            pairs.append(label)
            return original_pair(invocation)

        monkeypatch.setattr(module, "saved_tool_owner_and_request_scope", observed_pair)

    count_pair(saved_tool_policy_execution, "policy")
    count_pair(saved_tool_consent, "consent")
    with recipient(public_track=True) as case:
        consent = object.__new__(SavedToolConsentExecution)
        delegates: list[str] = []

        def changed_ask(invocation: Any, execution: Any, payload: Any) -> Any:
            delegates.append(change)
            if change == "revoked":
                case.live[0] = False
            elif change == "cancelled":
                case.proof.request()
            else:
                approval._live_requests[case.request["invocation_key"]].deadline = (
                    time.monotonic() - 1
                )
            # This is the ORIGINAL consent method. An uninitialized object
            # makes any admission/store/prepare access a test failure, proving
            # the changed authority rejects before an effect can be prepared.
            return consent(invocation, execution, payload)

        policy = object.__new__(SavedToolApprovalExecution)
        policy._ask = changed_ask
        reason = {
            "revoked": "original owner revoked",
            "cancelled": "tracked Saved execution parent changed",
            "native-expired": "chat_message create original invocation is unavailable",
        }[change]
        with pytest.raises(PermissionError, match=reason):
            policy(
                case.invocation,
                {
                    "contract_id": "tobkiri.service.tool.local.operation.v1",
                    "operation": "rumi_default_tools_pack.calculator-evaluate",
                },
                {
                    "tool_id": "calculator",
                    "tool_call_id": "call",
                    "arguments": {"expression": "1 + 1"},
                },
            )
        assert delegates == [change]
        assert pairs == ["policy", "consent"]
        assert len(selectors) >= 1
        if change == "native-expired":
            assert selectors == [case.invocation, case.invocation]


@pytest.mark.parametrize("field", ["capture", "digest"])
def test_original_record_in_place_callback_change_requires_fresh_next_gate(
    field: str,
) -> None:
    """Postidentity is not postfield revalidation; next original gate is fresh."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    with recipient(public_track=True) as case:
        key = case.request["invocation_key"]
        record = approval._live_requests[key]
        original_guard = record.guard
        deadline = record.deadline

        def change_after_original() -> None:
            original_guard()
            if field == "capture":
                record.capture = ("foreign-profile", *record.capture[1:])
            else:
                record.digest = "sha256:" + "0" * 64

        record.guard = change_after_original
        approval.assert_chat_message_request_live(
            case.request, case.source.envelope.context
        )
        assert approval._live_requests[key] is record
        assert record.deadline == deadline
        with pytest.raises(PermissionError, match="original invocation is unavailable"):
            saved_tool_owner_and_request_scope(case.invocation)


@pytest.mark.parametrize("change", ["remove", "replace-route"])
def test_single_guest_delivery_cannot_take_ordinary_path(change: str) -> None:
    """A malformed internal delivery tree cannot skip recipient authentication."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    with recipient(public_track=True) as case:
        def malformed(scope: Scope | None) -> Scope | None:
            if scope is None:
                return None
            parent = malformed(scope.parent)
            if scope is case.source:
                if change == "remove":
                    return parent
                envelope = replace(
                    scope.envelope,
                    contract_id="conversation.unrelated.v1",
                    operation_id="unrelated",
                )
                return replace(scope, envelope=envelope, parent=parent)
            return replace(scope, parent=parent)

        case.invocation.parent_invocation = malformed(
            case.invocation.parent_invocation
        )
        with pytest.raises(PermissionError, match="incoming source ancestry"):
            saved_tool_owner_and_request_scope(case.invocation)


def test_ordinary_single_guest_source_keeps_original_owner_and_guards() -> None:
    """The real source branch remains ordinary and keeps every live guard."""
    from tobkiri_host.saved_tool_context import saved_tool_owner_and_request_scope

    with recipient(public_track=True) as case:
        scope = case.invocation.parent_invocation
        while scope.parent is not case.source:
            scope = scope.parent
        source_broker = scope
        # The source executor is the physical child immediately above Broker.
        scope = case.invocation.parent_invocation
        while scope.parent is not source_broker:
            scope = scope.parent
        original = SimpleNamespace(**vars(case.invocation))
        original.envelope = scope.envelope
        original.parent_invocation = source_broker
        owner, accepted = saved_tool_owner_and_request_scope(original)
        assert accepted is case.source
        assert owner.caller_principal.value == (
            original.presentation_owner_principal_id
        )
        assert owner.caller_session_id == original.presentation_owner_session_id
        case.live[0] = False
        with pytest.raises(PermissionError, match="revoked"):
            saved_tool_owner_and_request_scope(original)


@pytest.mark.parametrize("change", ["expiry", "revoked", "close"])
def test_original_native_guard_callback_preserves_fresh_consent_boundary(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A callback mutation cannot authenticate a later original consent gate."""
    from tobkiri_host import saved_tool_consent, saved_tool_policy_execution
    from tobkiri_host.saved_tool_consent import SavedToolConsentExecution
    from tobkiri_host.saved_tool_policy_execution import SavedToolApprovalExecution

    with recipient(public_track=True) as case:
        key = case.request["invocation_key"]
        record = approval._live_requests[key]
        deadline = record.deadline
        now = [deadline - 1]
        monkeypatch.setattr(
            approval, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time)
        )
        original_guard = record.guard
        callback_calls: list[str] = []

        def mutate_after_original() -> None:
            original_guard()
            callback_calls.append(change)
            if change == "expiry":
                now[0] = deadline + 1
            elif change == "revoked":
                case.live[0] = False
            else:
                approval.close_chat_message_tool_request(key)

        record.guard = mutate_after_original
        original_pair = saved_tool_consent.saved_tool_owner_and_request_scope
        pairs: list[str] = []

        def count_pair(module: Any, label: str) -> None:
            def observed(invocation: Any) -> Any:
                pairs.append(label)
                return original_pair(invocation)

            monkeypatch.setattr(module, "saved_tool_owner_and_request_scope", observed)

        count_pair(saved_tool_policy_execution, "policy")
        count_pair(saved_tool_consent, "consent")
        # ORIGINAL consent with no admission/store attributes is a sentinel:
        # any access past its denied entry would raise AttributeError, not the
        # expected original PermissionError. No effect can be prepared here.
        consent = object.__new__(SavedToolConsentExecution)
        delegated: list[str] = []

        def ask(invocation: Any, execution: Any, payload: Any) -> Any:
            delegated.append("consent")
            return consent(invocation, execution, payload)

        policy = object.__new__(SavedToolApprovalExecution)
        policy._ask = ask
        reason = {
            "revoked": "original owner revoked",
            "close": "invocation drained",
            "expiry": "chat_message create original invocation is unavailable",
        }[change]
        with pytest.raises(PermissionError, match=reason):
            policy(
                case.invocation,
                {
                    "contract_id": "tobkiri.service.tool.local.operation.v1",
                    "operation": "rumi_default_tools_pack.calculator-evaluate",
                },
                {
                    "tool_id": "calculator",
                    "tool_call_id": "call",
                    "arguments": {"expression": "1 + 1"},
                },
            )
        assert callback_calls == [change]
        assert record.deadline == deadline
        if change == "expiry":
            assert pairs == ["policy", "consent"]
            assert delegated == ["consent"]
            assert now[0] == deadline + 1
            assert approval._live_requests[key] is record
        else:
            assert pairs == ["policy"]
            assert delegated == []
            if change == "close":
                assert key not in approval._live_requests
            else:
                assert approval._live_requests[key] is record


@pytest.mark.parametrize("supported", [False, True])
def test_cached_pair_control_cannot_fake_native_expiry_denial(
    supported: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deliberately cached test pair cannot satisfy precise expiry evidence."""
    from tobkiri_host import saved_tool_consent
    from tobkiri_host.saved_tool_consent import SavedToolConsentExecution

    with recipient(public_track=True) as case:
        cached = saved_tool_consent.saved_tool_owner_and_request_scope(
            case.invocation
        )
        approval._live_requests[case.request["invocation_key"]].deadline = (
            time.monotonic() - 1
        )
        # This deliberately invalid test control bypasses the pair to prove the
        # regression cannot falsely pass from unrelated unsupported-route denial.
        monkeypatch.setattr(
            saved_tool_consent,
            "saved_tool_owner_and_request_scope",
            lambda invocation: cached,
        )
        consent = object.__new__(SavedToolConsentExecution)
        if supported:
            execution = {
                "contract_id": "tobkiri.service.tool.local.operation.v1",
                "operation": "rumi_default_tools_pack.calculator-evaluate",
            }
            payload = {
                "tool_id": "calculator",
                "tool_call_id": "call",
                "arguments": {"expression": "1 + 1"},
            }
            with pytest.raises(AttributeError, match="_admissions"):
                consent(case.invocation, execution, payload)
        else:
            with pytest.raises(AssertionError, match="Regex pattern did not match"):
                with pytest.raises(
                    PermissionError,
                    match="chat_message create original invocation is unavailable",
                ):
                    consent(case.invocation, {}, {})
