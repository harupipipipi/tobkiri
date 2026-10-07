"""Saved guidance uses real durable/message owners and controlled model replies.

These owner tests are not native acceptance or release evidence. The explicit
contract transport runs the actual saved conversation continuation; only model
replies and lost transport acknowledgements are controlled by the test.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest

from core_runtime.global_contract_dispatch import GlobalContractClient
from ecosystem.tobkiri_conversation_orchestration_pack.runtime import saved_conversation as application
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.host import TurnHostFactoryV4
from ecosystem.rumi_turn_runtime_pack.runtime.saved import (
    LIFECYCLE_CONTRACT,
    LIFECYCLE_OPERATION,
    RECEIPT_CONTRACT,
    RECEIPT_OPERATION,
    SAVED_CONTRACTS,
    execute_saved_turn,
    reconcile_saved_turn,
)
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
from tests.test_operation_cancellation import _envelope
from tests.test_saved_conversation_steps import _owner, _setup
from tobkiri_host.operation_cancellation import OwnedCancellationHandles
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import (
    SAVED_CONVERSATION_CONTRACT,
    SAVED_CONVERSATION_OPERATION,
)


def _current() -> None:
    """Represent a live captured invocation in isolated owner tests."""


class _OwnerSession:
    """Transport explicit contracts to their real isolated owners."""

    profile_id = "defaults"
    plan_digest = "owner-test-capture"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.conversations, request = _setup(root)
        self.initial = {"request": request}
        self.turns = DurableTurnRuntime(self.profile_id, user_data_root=root)
        self.model_inputs: list[dict[str, Any]] = []
        self.dispatch_inputs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.before_model: Callable[[dict[str, Any]], None] = lambda _: None
        self.before_dispatch: Callable[[dict[str, Any]], None] = lambda _: None
        self.after_lifecycle: Callable[[str, dict[str, Any]], None] = lambda _action, _initial: None
        self.receipt_reply: Callable[[Any], Any] = lambda value: value
        self.lose_result: set[str] = set()
        self.lose_claim: set[str] = set()

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        """No ambient provider lookup is used by this finite transport."""
        return ()

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: Mapping[str, Any],
        **_options: Any,
    ) -> Mapping[str, Any]:
        """Dispatch real lifecycle/message code and a controlled model outcome."""
        self.calls.append((contract_id, operation, deepcopy(dict(payload))))
        target = (contract_id, operation)
        if target == (LIFECYCLE_CONTRACT, LIFECYCLE_OPERATION):
            assert payload["profile_id"] == self.profile_id
            initial = {
                key: value
                for key, value in payload.items()
                if key not in {"profile_id", "operation"}
            }
            action = payload["operation"]
            if action == "begin_saved":
                result = self.turns.begin_saved(initial)
            else:
                assert action == "claim_saved"
                result = self.turns.claim_saved(initial)
                turn_id = initial["request"]["turn_id"]
                if result["claimed"] and turn_id in self.lose_claim:
                    self.lose_claim.remove(turn_id)
                    raise TimeoutError("claim acknowledgement lost")
            self.after_lifecycle(action, initial)
            return result
        if target == (RECEIPT_CONTRACT, RECEIPT_OPERATION):
            assert payload["profile_id"] == self.profile_id
            if payload["operation"] == "get":
                return {"conversation": self.conversations.get(payload["conversation_id"])}
            assert payload["operation"] == "saved_receipt"
            return {
                "receipt": self.receipt_reply(self.conversations.saved_receipt(payload["turn_id"]))
            }
        assert target == (
            SAVED_CONVERSATION_CONTRACT,
            SAVED_CONVERSATION_OPERATION,
        )
        initial = deepcopy(dict(payload))
        self.dispatch_inputs.append(initial)
        self.before_dispatch(initial)
        intent = application.start(initial["request"])
        for _hop in range(20):
            if intent.get("status") in {"ok", "error"}:
                turn_id = initial["request"]["turn_id"]
                if turn_id in self.lose_result:
                    self.lose_result.remove(turn_id)
                    raise TimeoutError("saved acknowledgement lost after commit")
                return intent
            if intent["state"]["stage"] == "ai":
                self.model_inputs.append(initial)
                self.before_model(initial)
                outcome = {
                    "status": "ok",
                    "value": {
                        "status": "ok",
                        "output": f"Reply to {initial['request']['content']}",
                    },
                }
            else:
                outcome = _owner(self.conversations, intent, saved_input=initial)
            intent = application.resume(intent["state"], outcome)
        raise AssertionError("saved continuation exceeded its finite hop bound")


def _client(session: _OwnerSession, *, reader_only: bool = False) -> GlobalContractClient:
    return GlobalContractClient(
        session=session,
        allowed_contract_ids=(frozenset({RECEIPT_CONTRACT}) if reader_only else SAVED_CONTRACTS),
        consumer_pack_id="rumi_turn_runtime_pack",
    )


def _run(
    session: _OwnerSession,
    *,
    guard: Callable[[], None] = _current,
    **options: Any,
) -> dict[str, Any]:
    return execute_saved_turn(
        session.turns,
        session.initial,
        client=_client(session),
        guard=guard,
        **options,
    )


def _context(root: Path, factory: TurnHostFactoryV4) -> SimpleNamespace:
    """Supply an exact capture identity to the actual Host contract factory."""
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(
            SimpleNamespace(
                function=SimpleNamespace(
                    function_id=factory.function_id,
                    implementation_digest="owner-test-implementation",
                ),
                operation=SimpleNamespace(
                    contract_id=factory.contract_id,
                    operation_id=factory.operation_id,
                    contract_version="1.0.0",
                ),
                principal_ref=SimpleNamespace(value="owner-test-principal"),
                artifact=SimpleNamespace(digest="owner-test-artifact"),
            ),
        ),
        domain_ids={
            (factory.contract_id, factory.operation_id, "owner-test-principal"): "owner-test-domain"
        },
    )


def _host(
    session: _OwnerSession,
    kind: str,
    payload: Mapping[str, Any],
    *,
    invocation: SimpleNamespace | None = None,
) -> Mapping[str, Any]:
    factory = TurnHostFactoryV4(kind)
    captured = factory.capture(_context(session.root, factory))
    try:
        return captured.contributions[0].invoke(
            factory.operation_id,
            payload,
            invocation or SimpleNamespace(assert_current=_current),
        )
    finally:
        captured.close()


def _guidance(prompt: str = "Continue.") -> dict[str, Any]:
    return {
        "prompt": prompt,
        "target_type": "conversation",
        "target_id": "conversation-1",
        "conversation_id": "conversation-1",
        "visible": True,
        "auto_send": True,
        "metadata": {},
    }


def _attach(
    session: _OwnerSession,
    guidance_id: str,
    prompt: str = "Continue.",
    *,
    revision: int | None = None,
) -> Mapping[str, Any]:
    parent = session.turns.get("turn-1")
    assert parent is not None
    return _host(
        session,
        "guidance",
        {
            "profile_id": "defaults",
            "turn_id": "turn-1",
            "expected_revision": (parent["revision"] if revision is None else revision),
            "guidance_id": guidance_id,
            "guidance": _guidance(prompt),
        },
    )


def _child_id(parent: str, guidance: str) -> str:
    return "steer:" + canonical_digest(
        {"parent_turn_id": parent, "guidance_id": guidance}
    ).removeprefix("sha256:")


def _settled_parent(session: _OwnerSession) -> dict[str, Any]:
    """Prime reservation tests with a real owner receipt, never a synthetic ACK."""
    claimed = session.turns.claim_saved(session.initial)["turn"]
    _attach(session, "guidance-1")
    session.invoke(
        SAVED_CONVERSATION_CONTRACT,
        SAVED_CONVERSATION_OPERATION,
        session.initial,
    )
    receipt = session.conversations.saved_receipt("turn-1")
    assert receipt is not None
    return session.turns.settle_saved_from_receipt(
        "turn-1",
        input_digest=claimed["input_digest"],
        result_reference=receipt["result_reference"],
    )


def _followup(parent: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "request": {
            "turn_id": _child_id("turn-1", "guidance-1"),
            "conversation_id": parent["conversation_id"],
            "conversation_revision": parent["result_reference"]["conversation_revision"],
            "content": "Continue.",
        }
    }


def test_concurrent_guidance_keeps_receipt_and_completed_reentry_is_idempotent(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    entered, release = Event(), Event()

    def model(initial: dict[str, Any]) -> None:
        if initial["request"]["turn_id"] == "turn-1":
            entered.set()
            assert release.wait(10), "test did not release the model reply"

    session.before_model = model
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run, session)
        try:
            assert entered.wait(10), "parent never entered model dispatch"
            attached = _attach(session, "guidance-1")
            assert attached["status"] == "running"
        finally:
            release.set()
        result = future.result(timeout=10)
    child_id = _child_id("turn-1", "guidance-1")
    parent = session.turns.get("turn-1")
    child = session.turns.get(child_id)
    assert result["status"] == parent["status"] == child["status"] == "completed"
    assert parent["guidance"][0]["status"] == "sent"
    assert parent["guidance"][0]["followup_turn_id"] == child_id
    assert parent["result_reference"]["conversation_revision"] == 3
    assert child["conversation_revision"] == 3
    assert child["result_reference"]["conversation_revision"] == 5
    assert (
        child["result_reference"]
        == session.conversations.saved_receipt(child_id)["result_reference"]
    )
    assert [value["request"]["content"] for value in session.model_inputs] == [
        "Hello",
        "Continue.",
    ]
    before = session.conversations.path.read_bytes()
    assert _run(session)["turn"] == parent
    assert len(session.model_inputs) == 2
    assert session.conversations.path.read_bytes() == before


def test_lost_guidance_ack_replays_before_stale_revision_and_rebinding_is_denied(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    running = session.turns.claim_saved(session.initial)["turn"]
    first = _attach(session, "guidance-1", revision=running["revision"])
    replay = _attach(session, "guidance-1", revision=running["revision"])
    assert replay == first
    with pytest.raises(TurnConflict):
        _attach(session, "guidance-2", revision=running["revision"])
    with pytest.raises(TurnConflict):
        _attach(session, "guidance-1", "Different.")
    assert session.turns.get("turn-1") == first


def test_guidance_ack_replay_after_root_handoff_does_not_attach_to_child(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    entered, release = Event(), Event()
    child_id = _child_id("turn-1", "guidance-1")

    def model(initial: dict[str, Any]) -> None:
        if initial["request"]["turn_id"] == "turn-1":
            _attach(session, "guidance-1")
        else:
            assert initial["request"]["turn_id"] == child_id
            entered.set()
            assert release.wait(10), "test did not release child reply"

    session.before_model = model
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run, session)
        try:
            assert entered.wait(10), "reserved child never entered model dispatch"
            before = deepcopy(session.turns.list())
            replay = _attach(session, "guidance-1")
            assert replay["id"] == "turn-1"
            assert session.turns.list() == before
        finally:
            release.set()
        future.result(timeout=10)
    assert len(session.model_inputs) == 2
    assert session.turns.get(child_id)["guidance"] == []


def test_parallel_reservation_binds_one_child_and_rejects_unacknowledged_revision(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    parent = _settled_parent(session)
    followup = _followup(parent)
    stale = deepcopy(followup)
    stale["request"]["conversation_revision"] += 1
    with pytest.raises(TurnConflict):
        session.turns.reserve_guidance_followup("turn-1", "guidance-1", "turn-1", stale)
    assert session.turns.get("turn-1") == parent
    assert session.turns.get(followup["request"]["turn_id"]) is None

    def reserve() -> dict[str, Any]:
        return DurableTurnRuntime("defaults", user_data_root=tmp_path).reserve_guidance_followup(
            "turn-1", "guidance-1", "turn-1", followup
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: reserve(), range(2)))
    assert first == second
    assert first["id"] == _child_id("turn-1", "guidance-1")
    assert first["input_digest"] == canonical_digest(followup)
    assert len(session.turns.list()) == 2
    reserved = session.turns.get("turn-1")["guidance"][0]
    assert reserved["status"] == "consumed"
    assert reserved["followup_turn_id"] == first["id"]
    assert reserved["followup_source_turn_id"] == "turn-1"
    assert reserved["followup_input"] == followup
    assert session.turns.get(reserved["followup_turn_id"]) == first


def test_nested_guidance_uses_last_acknowledged_descendant_before_next_sibling(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    first_id = _child_id("turn-1", "guidance-1")
    nested_id = _child_id(first_id, "guidance-3")
    second_id = _child_id("turn-1", "guidance-2")

    def model(initial: dict[str, Any]) -> None:
        turn_id = initial["request"]["turn_id"]
        if turn_id == "turn-1":
            _attach(session, "guidance-1", "First.")
            _attach(session, "guidance-2", "Second.")
        elif turn_id == first_id:
            _attach(session, "guidance-3", "Nested.")

    session.before_model = model
    result = _run(session)
    assert result["status"] == "completed"
    assert [value["request"]["content"] for value in session.model_inputs] == [
        "Hello",
        "First.",
        "Nested.",
        "Second.",
    ]
    for turn_id, revision in ((first_id, 3), (nested_id, 5), (second_id, 7)):
        child = session.turns.get(turn_id)
        assert child["status"] == "completed"
        assert child["conversation_revision"] == revision
        assert child["result_reference"]["conversation_revision"] == revision + 2
    assert session.turns.get(second_id)["guidance_source_turn_id"] == nested_id
    assert session.conversations.get("conversation-1")["conversation_revision"] == 9
    _run(session)
    assert len(session.model_inputs) == 4


def test_lost_child_result_recovers_from_real_receipt_without_model_retry(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    child_id = _child_id("turn-1", "guidance-1")
    session.lose_result.add(child_id)
    session.before_model = lambda initial: (
        _attach(session, "guidance-1") if initial["request"]["turn_id"] == "turn-1" else None
    )
    _run(session)
    assert session.turns.get(child_id)["status"] == "waiting"
    assert session.turns.get("turn-1")["guidance"][0]["status"] == "consumed"
    before = session.conversations.path.read_bytes()
    offset = len(session.calls)
    recovered = reconcile_saved_turn(
        session.turns,
        child_id,
        client=_client(session, reader_only=True),
        guard=_current,
    )
    assert recovered["status"] == "completed"
    assert (
        recovered["turn"]["result_reference"]
        == (session.conversations.saved_receipt(child_id)["result_reference"])
    )
    assert [call[:2] for call in session.calls[offset:]] == [
        (RECEIPT_CONTRACT, RECEIPT_OPERATION),
    ]
    _run(session)
    assert session.turns.get("turn-1")["guidance"][0]["status"] == "sent"
    assert len(session.model_inputs) == 2
    assert session.conversations.path.read_bytes() == before


@pytest.mark.parametrize("field", ["input_digest", "conversation_revision"])
def test_mismatched_child_receipt_cannot_complete_or_retry_model(
    tmp_path: Path,
    field: str,
) -> None:
    session = _OwnerSession(tmp_path)
    child_id = _child_id("turn-1", "guidance-1")
    session.lose_result.add(child_id)
    session.before_model = lambda initial: (
        _attach(session, "guidance-1") if initial["request"]["turn_id"] == "turn-1" else None
    )
    _run(session)
    waiting = session.turns.get(child_id)

    def wrong(receipt: Any) -> Any:
        receipt = deepcopy(receipt)
        if field == "input_digest":
            receipt[field] = "sha256:" + "f" * 64
        else:
            receipt["result_reference"][field] += 1
        return receipt

    session.receipt_reply = wrong
    with pytest.raises(ValueError):
        reconcile_saved_turn(
            session.turns,
            child_id,
            client=_client(session, reader_only=True),
            guard=_current,
        )
    assert session.turns.get(child_id) == waiting
    assert len(session.model_inputs) == 2


def test_lost_child_claim_ack_never_reclaims_or_dispatches_model(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    child_id = _child_id("turn-1", "guidance-1")
    session.lose_claim.add(child_id)
    session.before_model = lambda initial: (
        _attach(session, "guidance-1") if initial["request"]["turn_id"] == "turn-1" else None
    )
    with pytest.raises(TimeoutError, match="claim acknowledgement lost"):
        _run(session)
    running = session.turns.get(child_id)
    assert running["status"] == "running"
    assert session.conversations.saved_receipt(child_id) is None
    _run(session)
    assert session.turns.get(child_id) == running
    assert len(session.model_inputs) == 1
    assert session.turns.get("turn-1")["guidance"][0]["status"] == "consumed"


def test_stale_capture_after_child_reservation_does_not_claim_or_dispatch_child(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    expired = Event()
    child_id = _child_id("turn-1", "guidance-1")
    session.before_model = lambda initial: (
        _attach(session, "guidance-1") if initial["request"]["turn_id"] == "turn-1" else None
    )

    def after(action: str, initial: dict[str, Any]) -> None:
        if action == "begin_saved" and initial["request"]["turn_id"] == child_id:
            expired.set()

    def guard() -> None:
        if expired.is_set():
            raise PermissionError("captured activation is stale")

    session.after_lifecycle = after
    with pytest.raises(PermissionError, match="captured activation is stale"):
        _run(session, guard=guard)
    assert session.turns.get(child_id)["status"] == "queued"
    assert len(session.model_inputs) == 1
    assert session.turns.get("turn-1")["guidance"][0]["status"] == "consumed"
    session.after_lifecycle = lambda _action, _initial: None
    _run(session)
    assert session.turns.get(child_id)["status"] == "completed"
    assert len(session.model_inputs) == 2


def test_changed_conversation_never_rebases_queued_child_onto_latest_revision(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    child_id = _child_id("turn-1", "guidance-1")

    def model(initial: dict[str, Any]) -> None:
        if initial["request"]["turn_id"] == "turn-1":
            _attach(session, "guidance-1")
            _attach(session, "guidance-2", "Later.")

    def dispatch(initial: dict[str, Any]) -> None:
        if initial["request"]["turn_id"] == child_id:
            session.conversations.update(
                "conversation-1",
                {"title": "Concurrent owner update"},
                expected_conversation_revision=3,
            )

    session.before_model = model
    session.before_dispatch = dispatch
    _run(session)
    child = session.turns.get(child_id)
    assert child["status"] == "failed"
    assert child["conversation_revision"] == 3
    assert child["events"][-1]["details"]["error_code"] == ("CONVERSATION_REVISION_CONFLICT")
    assert len(session.model_inputs) == 1
    parent = session.turns.get("turn-1")
    assert [item["status"] for item in parent["guidance"]] == ["failed", "queued"]
    assert session.conversations.get("conversation-1")["conversation_revision"] == 4


def test_stale_stop_cannot_cancel_queued_guidance_before_reservation(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    parent = _settled_parent(session)

    def stale() -> None:
        raise PermissionError("captured activation is stale")

    with pytest.raises(PermissionError, match="captured activation is stale"):
        _host(
            session,
            "stop",
            {"turn_id": "turn-1"},
            invocation=SimpleNamespace(assert_current=stale),
        )
    assert session.turns.get("turn-1") == parent
    assert len(session.turns.list()) == 1


def test_stop_before_reservation_cancels_queue_without_starting_child(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    _settled_parent(session)
    result = _host(session, "stop", {"turn_id": "turn-1"})
    # Queue cancellation has no live Host drain observation. It prevents new
    # dispatch, but cannot manufacture a verified execution-stop receipt.
    assert result == {
        "status": "cancellation_requested",
        "turn_id": "turn-1",
        "stopped": False,
    }
    parent = session.turns.get("turn-1")
    assert parent["status"] == "completed"
    assert parent["guidance"][0]["status"] == "failed"
    _run(session)
    assert len(session.model_inputs) == 1
    assert len(session.turns.list()) == 1


def _cancellations(
    guard: Callable[[], None] = _current,
    *,
    stop_session: str = "owner-session",
) -> tuple[Any, Any, Any]:
    registry = OwnedCancellationHandles()
    execution = _envelope()
    execution = replace(execution, context=replace(execution.context, profile_id="defaults"))
    stop_envelope = replace(
        _envelope(),
        context=execution.context,
        deadline_monotonic=time.monotonic() + 0.1,
    )
    values = {
        "group": ("rumi_turn_runtime_pack", "saved-turn"),
        "owner_principal": "owner",
        "guard": guard,
    }
    execute = registry.bind(
        envelope=execution,
        role="execute",
        owner_session="owner-session",
        **values,
    )
    stop = registry.bind(
        envelope=stop_envelope,
        role="stop",
        owner_session=stop_session,
        **{**values, "guard": _current},
    )
    return (
        execution,
        execute,
        SimpleNamespace(
            cancellation=stop,
            assert_current=_current,
            envelope=stop_envelope,
        ),
    )


def test_root_stop_signals_live_child_without_claiming_unverified_drain(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    entered, release = Event(), Event()
    execution, execute, stop_invocation = _cancellations()
    child_id = _child_id("turn-1", "guidance-1")

    def guard() -> None:
        if execution.cancellation_requested.is_set():
            raise PermissionError("captured execution was cancelled")

    def model(initial: dict[str, Any]) -> None:
        if initial["request"]["turn_id"] == "turn-1":
            _attach(session, "guidance-1")
        else:
            entered.set()
            assert release.wait(10), "test did not release the child reply"

    session.before_model = model
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run, session, guard=guard, track_execution=execute.track)
        try:
            assert entered.wait(10), "child never entered tracked model dispatch"
            result = _host(
                session,
                "stop",
                {"turn_id": "turn-1"},
                invocation=stop_invocation,
            )
            assert result == {
                "status": "cancellation_requested",
                "turn_id": "turn-1",
                "stopped": False,
            }
            assert execution.cancellation_requested.is_set()
            assert any(
                event["name"] == "turn.cancellation_requested"
                for event in session.turns.get(child_id)["events"]
            )
        finally:
            release.set()
        future.result(timeout=10)
    # The stop never proved drain. The exact owner receipt is therefore the
    # authoritative terminal and reader reconciliation must not replay work.
    assert session.conversations.saved_receipt(child_id) is not None
    recovered = reconcile_saved_turn(
        session.turns,
        child_id,
        client=_client(session, reader_only=True),
        guard=_current,
    )
    assert recovered["turn"]["status"] == "completed"
    assert recovered["turn"]["result_reference"] == (
        session.conversations.saved_receipt(child_id)["result_reference"]
    )
    assert session.turns.get("turn-1")["status"] == "completed"
    assert len(session.model_inputs) == 2


def test_foreign_session_root_stop_cannot_signal_or_persist_child_intent(
    tmp_path: Path,
) -> None:
    session = _OwnerSession(tmp_path)
    parent = _settled_parent(session)
    followup = _followup(parent)
    session.turns.reserve_guidance_followup("turn-1", "guidance-1", "turn-1", followup)
    child = session.turns.claim_saved(followup)["turn"]
    execution, execute, stop_invocation = _cancellations(stop_session="foreign")
    with execute.track(child["id"]):
        with pytest.raises(PermissionError):
            _host(
                session,
                "stop",
                {"turn_id": "turn-1"},
                invocation=stop_invocation,
            )
        assert not execution.cancellation_requested.is_set()
        assert session.turns.get(child["id"]) == child


@pytest.mark.parametrize("complete_before_fence", [True, False])
def test_root_stop_preserves_exact_completion_while_fencing_descendants(
    tmp_path: Path,
    complete_before_fence: bool,
) -> None:
    """A completion racing stop cannot start an acknowledged queued descendant."""
    session = _OwnerSession(tmp_path)
    parent = _settled_parent(session)
    followup = _followup(parent)
    child_id = followup["request"]["turn_id"]
    nested_id = _child_id(child_id, "guidance-2")
    session.turns.reserve_guidance_followup("turn-1", "guidance-1", "turn-1", followup)
    session.turns.claim_saved(followup)
    _attach(session, "guidance-2", "Queued descendant.")
    # The conversation owner has committed the real child outcome, while the
    # coordinator has not settled it or reserved its queued descendant yet.
    session.invoke(
        SAVED_CONVERSATION_CONTRACT,
        SAVED_CONVERSATION_OPERATION,
        followup,
    )
    receipt = session.conversations.saved_receipt(child_id)
    assert receipt is not None
    execution, execute, stop_invocation = _cancellations()
    cancellation = stop_invocation.cancellation
    requested: list[str] = []
    intent_before_handle: list[bool] = []
    completion_fenced: list[bool] = []
    early_completion: list[str] = []

    def can_request(reference: str) -> bool:
        available = cancellation.can_request(reference)
        if available and complete_before_fence and not early_completion:
            child = session.turns.get(child_id)
            session.turns.settle_saved_from_receipt(
                child_id,
                input_digest=child["input_digest"],
                result_reference=receipt["result_reference"],
            )
            early_completion.append(child_id)
        return available

    def request(reference: str) -> Any:
        requested.append(reference)
        child = session.turns.get(child_id)
        intent_before_handle.append(
            any(event["name"] == "turn.cancellation_requested" for event in child["events"])
        )
        # Force the precise old race: receipt settlement happens after stop
        # resolved a running child, before it asks the live cancellation handle.
        try:
            session.turns.settle_saved_from_receipt(
                child_id,
                input_digest=child["input_digest"],
                result_reference=receipt["result_reference"],
            )
        except TurnConflict:
            completion_fenced.append(True)
        else:
            completion_fenced.append(False)
        return cancellation.request(reference)

    stop_invocation.cancellation = SimpleNamespace(
        can_request=can_request,
        request=request,
        active_for=cancellation.active_for,
    )
    with execute.track(child_id):
        result = _host(
            session,
            "stop",
            {"turn_id": "turn-1"},
            invocation=stop_invocation,
        )
        assert result == {
            "status": "cancellation_requested",
            "turn_id": "turn-1",
            "stopped": False,
        }
        if complete_before_fence:
            assert early_completion == [child_id]
            assert requested == []
            assert not execution.cancellation_requested.is_set()
            assert session.turns.get(child_id)["guidance"][0]["status"] == "failed"
        else:
            assert execution.cancellation_requested.is_set()
            assert requested == [child_id]
            assert intent_before_handle == [True]
            assert completion_fenced == [False]
        assert session.turns.get(nested_id) is None
    # A new captured request and reader reconciliation preserve the owner
    # completion without dispatching another descendant.
    reconcile_saved_turn(
        session.turns,
        child_id,
        client=_client(session, reader_only=True),
        guard=_current,
    )
    _run(session)
    assert len(session.model_inputs) == 2
    assert session.turns.get(nested_id) is None
    child = session.turns.get(child_id)
    assert child["status"] == "completed"
    assert child["guidance"][0]["status"] == "failed"
    assert child["guidance"][0]["failure_reason"] == "cancelled_before_dispatch"
    assert session.turns.get("turn-1")["status"] == "completed"
