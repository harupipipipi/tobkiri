"""Calendar cancellation requires actual private proof and owner durable ACK."""

from concurrent.futures import Future
from dataclasses import replace
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace as NS
import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_operation_cancellation import _envelope, _binding, _child_envelope
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

from tobkiri_host import scheduled_job_cancellation as host

PATH = (
    Path(__file__).resolve().parents[1]
    / "ecosystem/rumi_turn_runtime_pack/runtime/scheduled_job.py"
)
SPEC = importlib.util.spec_from_file_location("calendar_cancel_adapter_test", PATH)
job = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job)


def setup(tmp_path):
    raw = {"profile_id": "profile-1", "conversation_id": "target", "message": "hello"}
    envelope = {
        "profile_id": "profile-1",
        "action_id": "chat.saved",
        "payload": raw,
        "idempotency_key": "occurrence",
        "schedule_id": "schedule",
        "lease_id": "lease",
    }
    turn = job._calendar_turn_id("profile-1", "schedule", "occurrence")
    source = {
        "request": {
            "turn_id": turn,
            "conversation_id": "target",
            "conversation_revision": 1,
            "content": "hello",
        }
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile-1")
    ledger.admit("occurrence", envelope)
    ledger.bind_source("occurrence", source)
    store = DurableTurnRuntime("profile-1", user_data_root=tmp_path)
    store.bind_saved_input(source, source)
    store.claim_saved(source)
    target = replace(
        _envelope(),
        contract_id=host.ADAPTER[0],
        operation_id=host.ADAPTER[1],
        payload={**envelope, "operation": "dispatch"},
    )
    cancel = replace(
        target,
        payload={**envelope, "operation": "cancel"},
        cancellation_requested=threading.Event(),
    )
    parent = replace(
        _envelope(),
        contract_id=host.BROKER[0],
        operation_id=host.BROKER[1],
        payload={
            "profile_id": "profile-1",
            "idempotency_key": "occurrence",
            "operation": "cancel",
        },
    )
    scope = CapturedInvocationScopeV4(
        cancel, lambda: None, CapturedInvocationScopeV4(parent, lambda: None)
    )
    registry = OwnedCancellationHandles()
    port = host.ScheduledJobCancellationBinding(
        registry=registry, scope=scope, guard=lambda: None
    )
    binding = _binding(
        registry,
        target,
        "execute",
        group=host.GROUP,
        owner_session="private-target-session",
    )
    invocation = NS(assert_current=lambda: None, scheduled_job_cancellation=port)
    args = dict(
        store=store,
        ledger=ledger,
        key="occurrence",
        values=envelope,
        envelope=envelope,
        invocation=invocation,
    )
    return args, source, turn, target, binding


def test_actual_private_signal_verified_drain_then_durable_ack(tmp_path):
    args, source, turn, target, binding = setup(tmp_path)
    ready = threading.Event()

    def worker():
        with binding.track(turn):
            proof = nested_cancellation_proof_for(
                target, "owner", "private-target-session"
            )
            child_id = proof.reserve_child(_child_envelope(target))
            future = Future()
            proof.bind_child(child_id, future)
            ready.set()
            assert target.cancellation_requested.wait(2)
            record = args["store"].get(turn)
            assert record["status"] == "running"
            assert any(
                event["name"] == "turn.cancellation_requested"
                for event in record["events"]
            )
            future.set_result(None)
            proof.record_backend_cancellation(child_id, future)
            proof.record_resource_drain(future)

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(2)
    receipt = job.cancel_scheduled_task(**args)
    thread.join(2)
    assert not thread.is_alive()
    assert receipt["status"] == "cancelled"
    assert args["store"].get(turn)["status"] == "cancelled"
    assert args["ledger"].admit("occurrence", args["envelope"]) == receipt


def test_missing_private_handle_stays_pending_without_owner_stop(tmp_path):
    args, source, turn, target, binding = setup(tmp_path)
    assert job.cancel_scheduled_task(**args)["status"] == "cancellation_pending"
    record = args["store"].get(turn)
    assert record["status"] == "running"
    assert not any(
        event["name"] == "turn.cancellation_requested" for event in record["events"]
    )
    assert not target.cancellation_requested.is_set()


def test_changed_occurrence_cannot_signal_or_modify_owner(tmp_path):
    args, source, turn, target, binding = setup(tmp_path)
    args["envelope"] = {**args["envelope"], "lease_id": "other"}
    with binding.track(turn):
        assert job.cancel_scheduled_task(**args)["status"] == "cancellation_pending"
    assert not target.cancellation_requested.is_set()
    assert args["store"].get(turn)["status"] == "running"


def test_queued_preparation_releases_without_active_signal(tmp_path):
    raw = {"profile_id": "profile-1", "conversation_id": "target", "message": "hello"}
    envelope = {
        "profile_id": "profile-1",
        "action_id": "chat.saved",
        "payload": raw,
        "idempotency_key": "occurrence",
        "schedule_id": "schedule",
        "lease_id": "lease",
    }
    turn = job._calendar_turn_id("profile-1", "schedule", "occurrence")
    ledger = job.ScheduledSourceLedger(tmp_path, "profile-1")
    ledger.admit("occurrence", envelope)
    store = DurableTurnRuntime("profile-1", user_data_root=tmp_path)
    store.reserve_calendar_preparation(
        occurrence_key="occurrence",
        source=raw,
        turn_id=turn,
        conversation_id="target",
        conversation_revision=1,
    )
    result = job.cancel_scheduled_task(
        store=store,
        ledger=ledger,
        key="occurrence",
        values=envelope,
        envelope=envelope,
        invocation=NS(assert_current=lambda: None),
    )
    assert result["status"] == "cancelled"
    assert store.get(turn)["status"] == "cancelled"


def test_requested_signal_without_verified_resource_drain_stays_pending(
    tmp_path, monkeypatch
):
    import time

    args, source, turn, target, binding = setup(tmp_path)
    monkeypatch.setattr(job, "time", NS(monotonic=lambda: time.monotonic() - 10))
    with binding.track(turn):
        result = job.cancel_scheduled_task(**args)
        assert result["status"] == "cancellation_pending"
        assert target.cancellation_requested.is_set()
        assert args["store"].get(turn)["status"] == "running"
        assert args["ledger"].admit("occurrence", args["envelope"]) is None


def test_calendar_61441_utf8_bytes_rejected_before_any_owner_operation(tmp_path):
    raw = {
        "profile_id": "profile-1",
        "conversation_id": None,
        "message": "あ" * 20480 + "x",
        "model": "chosen",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile-1")
    ledger.admit("key", raw)
    with pytest.raises(ValueError, match="message is invalid"):
        job.prepare_calendar_destination(
            raw,
            "key",
            ledger,
            NS(invoke=lambda *args: pytest.fail("invalid message reached owner")),
            "profile-1",
            lambda: None,
        )
