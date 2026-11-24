"""Real registry/Future source-join units; native captured approval not simulated."""

from contextlib import contextmanager
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
from typing import Iterator
import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_finite_calendar_source import calendar_source
from tests.test_operation_cancellation import _child_envelope, _envelope
from tobkiri_host import finite_chat_dispatch as scheduling
from tobkiri_host.operation_cancellation import _NestedCancellationProof
from ecosystem.rumi_tool_broker_pack.runtime import chat_delivery as delivery


@contextmanager
def joined_calendar() -> Iterator[
    tuple[
        list[CapturedInvocationScopeV4],
        _NestedCancellationProof,
        tuple[str, str],
        list[bool],
        Future[object],
        Future[object],
    ]
]:
    """Enroll actual source Futures under the selected private Calendar witness."""
    with calendar_source() as (guest, proof, payload, owner, live, guest_future):
        with scheduling.finite_saved_chat_source(guest, proof, payload, owner):
            root = replace(
                _child_envelope(guest.envelope),
                contract_id=scheduling.BROKER[0],
                operation_id=scheduling.BROKER[1],
                payload=payload,
            )
            assert scheduling.select_inline_chat_dispatch(root, proof, guest)
            child = proof.reserve_child(root)
            future = Future()
            future.set_running_or_notify_cancel()
            proof.bind_child(child, future)
            scopes = [
                CapturedInvocationScopeV4(root, lambda: None, guest),
                guest,
                guest.parent,
            ]
            for route in (
                delivery.EXECUTOR,
                delivery.LOCAL,
                delivery.EFFECT,
                delivery.EXECUTE,
            ):
                envelope = replace(
                    _child_envelope(root), contract_id=route[0], operation_id=route[1]
                )
                scopes.insert(
                    0, CapturedInvocationScopeV4(envelope, lambda: None, scopes[0])
                )
            try:
                yield scopes, proof, owner, live, guest_future, future
            finally:
                if not future.done():
                    future.set_result(None)
                proof.record_resource_drain(future)


def routes(scopes: list[CapturedInvocationScopeV4]) -> list[tuple[str, str]]:
    """Read exact captured operation identities without granting authority."""
    return [(s.envelope.contract_id, s.envelope.operation_id) for s in scopes]


def test_genuine_selected_calendar_keeps_original_guest_source_and_root_future() -> (
    None
):
    """Admit the genuine join and a Guest recapture with its retained parent."""
    with joined_calendar() as (scopes, _proof, _owner, _live, _guest, _root):
        assert delivery._delivery_saved_source(scopes, routes(scopes)) is scopes[5]
        # Production recaptures Guest but retains its exact parent object.
        scopes[5] = CapturedInvocationScopeV4(
            scopes[5].envelope, lambda: None, scopes[6]
        )
        assert delivery._delivery_saved_source(scopes, routes(scopes)).envelope is (
            scopes[5].envelope
        )


@pytest.mark.parametrize(
    "failure",
    [
        "foreign_root",
        "revoked",
        "cancelled",
        "completed_guest",
        "completed_root",
        "cloned_adapter_scope",
        "reparented_guest",
        "duplicate_saved",
        "extra_scope",
    ],
)
def test_calendar_source_admission_rejects_changed_liveness_and_physical_join(
    failure: str,
) -> None:
    """Deny changed ownership, liveness, or the physical retained scope join."""
    with joined_calendar() as (scopes, proof, _owner, live, guest, root):
        if failure == "foreign_root":
            scopes[4] = CapturedInvocationScopeV4(
                replace(scopes[4].envelope), lambda: None, scopes[5]
            )
        elif failure == "revoked":
            live[0] = False
        elif failure == "cancelled":
            proof.request()
        elif failure == "completed_guest":
            guest.set_result(None)
        elif failure == "completed_root":
            root.set_result(None)
        elif failure == "cloned_adapter_scope":
            original = scopes[6]
            scopes[6] = CapturedInvocationScopeV4(
                original.envelope, lambda: None, original.parent
            )
        elif failure == "reparented_guest":
            scopes[5] = CapturedInvocationScopeV4(
                scopes[5].envelope, lambda: None, None
            )
        elif failure == "duplicate_saved":
            for index in (5, 6):
                envelope = replace(
                    scopes[index].envelope,
                    contract_id=delivery.SAVED[0],
                    operation_id=delivery.SAVED[1],
                )
                scopes[index] = CapturedInvocationScopeV4(envelope, lambda: None)
        elif failure == "extra_scope":
            scopes.append(CapturedInvocationScopeV4(_envelope(), lambda: None))
        with pytest.raises(PermissionError):
            delivery._delivery_saved_source(scopes, routes(scopes))


def test_calendar_payload_tags_without_private_token_cannot_admit_source() -> None:
    """Payload labels cannot replace the private selected source witness."""
    envelope = replace(_envelope(), payload={"origin": "calendar", "approved": True})
    scopes = [CapturedInvocationScopeV4(envelope, lambda: None)] * 7
    with pytest.raises(PermissionError):
        delivery._delivery_saved_source(scopes, routes(scopes))


def test_ordinary_saved_source_selection_remains_unchanged() -> None:
    """Keep the established ordinary Saved owner selection."""
    saved = CapturedInvocationScopeV4(
        replace(
            _envelope(), contract_id=delivery.SAVED[0], operation_id=delivery.SAVED[1]
        ),
        lambda: None,
    )
    guest = CapturedInvocationScopeV4(
        _child_envelope(saved.envelope), lambda: None, saved
    )
    scopes = [guest, saved]
    assert delivery._delivery_saved_source(scopes, routes(scopes)) is saved


def test_duplicate_source_broker_rejected_before_native_plan_use() -> None:
    """Reject duplicate Broker ancestry before considering a native plan."""
    with joined_calendar() as (scopes, _proof, owner, _live, _guest, _root):
        scopes[1] = CapturedInvocationScopeV4(
            scopes[4].envelope, lambda: None, scopes[2]
        )
        scopes[0] = CapturedInvocationScopeV4(
            scopes[0].envelope, lambda: None, scopes[1]
        )
        envelope = replace(
            _envelope(),
            contract_id=delivery.DELIVERY[0],
            operation_id=delivery.DELIVERY[1],
            payload={},
        )
        invocation = SimpleNamespace(
            envelope=envelope,
            parent_invocation=scopes[0],
            assert_current=lambda: None,
            presentation_owner_principal_id=owner[0],
            presentation_owner_session_id=owner[1],
        )
        with pytest.raises(PermissionError):
            delivery.assert_delivery_saved_branch(invocation, {"approved": True})
