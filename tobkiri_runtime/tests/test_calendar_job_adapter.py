"""Calendar jobs retain source revisions without retaining Host authority."""

from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

PATH = (
    Path(__file__).resolve().parents[1]
    / "ecosystem/rumi_turn_runtime_pack/runtime/scheduled_job.py"
)
SPEC = importlib.util.spec_from_file_location("calendar_job_adapter_test", PATH)
job = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = job
SPEC.loader.exec_module(job)


def captured(tmp_path):
    binding = NS(
        function=NS(
            function_id=job.FUNCTION, implementation_digest="sha256:" + "b" * 64
        ),
        operation=NS(
            contract_id=job.CONTRACT,
            operation_id=job.OPERATION,
            contract_version="2.0.0",
        ),
        principal_ref=NS(value="principal"),
        artifact=NS(digest="sha256:" + "a" * 64),
    )
    context = NS(
        profile_id="profile",
        user_data_root=tmp_path,
        provider_bindings=[binding],
        domain_ids={(job.CONTRACT, job.OPERATION, "principal"): "domain"},
    )
    provider = job.ScheduledSavedJobFactory().capture(context)
    return provider.contributions[0].invoke


def invocation():
    return NS(
        assert_current=lambda: None,
        parent_invocation=NS(
            assert_current=lambda: None,
            envelope=NS(
                contract_id="tobkiri.action.job.v1",
                operation_id="rumi_job_action_broker_pack.job-action-broker",
            ),
        ),
        contract_client=lambda **kwargs: job.GlobalContractClient(
            session=NS(profile_id="profile"),
            allowed_contract_ids=kwargs["allowed_contract_ids"],
            consumer_pack_id=kwargs["consumer_pack_id"],
        ),
        cancellation=NS(track=lambda _: nullcontext()),
    )


def envelope(**changes):
    return {
        "operation": "dispatch",
        "profile_id": "profile",
        "action_id": job.ACTION_ID,
        "idempotency_key": "occurrence",
        "schedule_id": "schedule",
        "lease_id": "lease",
        "payload": {
            "profile_id": "profile",
            "conversation_id": "target",
            "message": "hello",
        },
        **changes,
    }


def test_original_source_revision_and_envelope_are_immutable(tmp_path):
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", {"message": "hello"})
    source = {
        "request": {
            "turn_id": "calendar:one",
            "conversation_id": "target",
            "conversation_revision": 1,
            "content": "hello",
        }
    }
    assert ledger.bind_source("key", source) == source
    assert ledger.recover_source("key", "calendar:one") == source
    with pytest.raises(PermissionError):
        ledger.bind_source(
            "key", {"request": {**source["request"], "conversation_revision": 2}}
        )
    with pytest.raises(PermissionError):
        ledger.admit("key", {"message": "changed"})


def test_adapter_fresh_capture_dispatch_and_replay_actual_receipt(
    tmp_path, monkeypatch
):
    run = captured(tmp_path)
    calls = []

    def helper(task, schedule, execution, **ports):
        calls.append((task, schedule, execution))
        ports["guard"]()
        ports["bind_source"](
            {
                "request": {
                    "turn_id": "calendar:one",
                    "conversation_id": "target",
                    "conversation_revision": 1,
                    "content": "hello",
                }
            }
        )
        return {"status": "completed", "turn_id": "calendar:one"}

    monkeypatch.setattr(job, "execute_scheduled_task", helper)
    monkeypatch.setattr(job, "prepare_calendar_destination", lambda task, *args: task)
    monkeypatch.setattr(
        job,
        "DurableTurnRuntime",
        lambda *args, **kwargs: NS(
            bind_calendar_preparation=lambda **kwargs: kwargs["payload"],
            release_calendar_preparation=lambda **kwargs: None,
        ),
    )
    first = run(job.OPERATION, envelope(), invocation())
    assert run(job.OPERATION, envelope(), invocation()) == first
    assert len(calls) == 1
    with pytest.raises(PermissionError):
        run(job.OPERATION, envelope(payload={"message": "changed"}), invocation())


def test_profile_authority_flags_and_missing_broker_ancestry_rejected(tmp_path):
    run = captured(tmp_path)
    with pytest.raises(PermissionError):
        run(job.OPERATION, envelope(profile_id="foreign"), invocation())
    with pytest.raises(ValueError):
        run(job.OPERATION, {**envelope(), "approved": True}, invocation())
    inv = invocation()
    inv.parent_invocation = None
    with pytest.raises(PermissionError, match="broker ancestry"):
        run(job.OPERATION, envelope(), inv)


def test_crash_recovery_keeps_original_source_for_new_fresh_invocation(
    tmp_path, monkeypatch
):
    run = captured(tmp_path)
    original = {
        "request": {
            "turn_id": "calendar:one",
            "conversation_id": "target",
            "conversation_revision": 1,
            "content": "hello",
        }
    }
    calls = []

    def helper(*args, **ports):
        old = ports["recover_source"]("calendar:one")
        calls.append(old)
        if old is None:
            ports["bind_source"](original)
            raise RuntimeError("uncertain transport")
        assert old == original
        return {"status": "completed", "turn_id": "calendar:one"}

    monkeypatch.setattr(job, "execute_scheduled_task", helper)
    monkeypatch.setattr(job, "prepare_calendar_destination", lambda task, *args: task)
    monkeypatch.setattr(
        job,
        "DurableTurnRuntime",
        lambda *args, **kwargs: NS(
            bind_calendar_preparation=lambda **kwargs: kwargs["payload"],
            release_calendar_preparation=lambda **kwargs: None,
        ),
    )
    with pytest.raises(RuntimeError):
        run(job.OPERATION, envelope(), invocation())
    assert run(job.OPERATION, envelope(), invocation())["status"] == "completed"
    assert calls == [None, original]


def test_null_destination_created_once_per_occurrence_and_model_applied(
    tmp_path, monkeypatch
):
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    owner = ConversationStore("profile", user_data_root=tmp_path)
    normalization = NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task))
    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        normalization,
    )
    writes = []

    def dispatch(contract, operation, payload):
        if contract == "tobkiri.resource.conversation.v1":
            return (
                owner.snapshot()
                if payload["operation"] == "list"
                else {"conversation": owner.get(payload["conversation_id"])}
            )
        writes.append(payload)
        if payload["operation"] == "create":
            return owner.create(
                payload["conversation"], expected_revision=payload["expected_revision"]
            )
        return owner.update(
            payload["conversation_id"],
            payload["patch"],
            expected_conversation_revision=payload["expected_conversation_revision"],
        )

    client = NS(invoke=dispatch, providers=lambda contract: [])
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    intent = {
        "message": "Hello",
        "profile_id": "profile",
        "conversation_id": None,
        "model": "configured-model",
    }
    ledger.admit("first", intent)
    first = job.prepare_calendar_destination(
        intent, "first", ledger, client, "profile", lambda: None
    )
    assert owner.get(first["conversation_id"])["model_reference"] == "configured-model"
    replay = job.prepare_calendar_destination(
        intent, "first", ledger, client, "profile", lambda: None
    )
    assert replay == first and len(writes) == 1
    ledger.admit("second", intent)
    second = job.prepare_calendar_destination(
        intent, "second", ledger, client, "profile", lambda: None
    )
    assert second["conversation_id"] != first["conversation_id"] and len(writes) == 2
    existing_intent = {
        **intent,
        "conversation_id": first["conversation_id"],
        "model": "new-model",
    }
    ledger.admit("third", existing_intent)
    job.prepare_calendar_destination(
        existing_intent, "third", ledger, client, "profile", lambda: None
    )
    assert owner.get(first["conversation_id"])["model_reference"] == "new-model"
    assert writes[-1]["operation"] == "update"


def test_unready_model_blocks_canonical_destination_write(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task)),
    )
    intent = {
        "message": "Hello",
        "profile_id": "profile",
        "conversation_id": None,
        "model": "bad-model",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", intent)
    calls = []

    def dispatch(contract, operation, payload):
        calls.append(contract)
        return {"profile": None}

    with pytest.raises(ValueError, match="model is unavailable"):
        job.prepare_calendar_destination(
            intent,
            "key",
            ledger,
            NS(invoke=dispatch, providers=lambda contract: [{}]),
            "profile",
            lambda: None,
        )
    assert calls == ["tobkiri.resource.ai.model.profile.v1"]


def test_null_destination_without_model_rejected_before_owner_write(tmp_path):
    task = {"message": "hello", "profile_id": "profile", "conversation_id": None}
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)
    with pytest.raises(ValueError, match="requires a model"):
        job.prepare_calendar_destination(
            task,
            "key",
            ledger,
            NS(invoke=lambda *args: pytest.fail("unexpected owner read/write")),
            "profile",
            lambda: None,
        )


def test_busy_reservation_rejects_before_model_cas(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task)),
    )
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": "target",
        "model": "new",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)
    calls = []

    def invoke(contract, operation, payload):
        calls.append(payload["operation"])
        assert payload["operation"] == "get"
        return {
            "conversation": {
                "id": "target",
                "model_reference": "old",
                "conversation_revision": 7,
            }
        }

    def reserve(**kwargs):
        assert kwargs["conversation_revision"] == 7
        assert kwargs["source"] == task
        raise PermissionError("target busy")

    with pytest.raises(PermissionError, match="busy"):
        job.prepare_calendar_destination(
            task,
            "key",
            ledger,
            NS(invoke=invoke, providers=lambda _: []),
            "profile",
            lambda: None,
            NS(
                recover_calendar_preparation=lambda **kwargs: None,
                reserve_calendar_preparation=reserve,
            ),
            "schedule",
        )
    assert calls == ["get"]


def test_bound_preparation_recovers_before_latest_model_read(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task)),
    )
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": "target",
        "model": "new",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)
    turn = job._calendar_turn_id("profile", "schedule", "key")
    source = {
        "request": {
            "turn_id": turn,
            "conversation_id": "target",
            "conversation_revision": 8,
            "content": "hello",
        }
    }
    result = job.prepare_calendar_destination(
        task,
        "key",
        ledger,
        NS(invoke=lambda *args: pytest.fail("latest owner read/write")),
        "profile",
        lambda: None,
        NS(
            recover_calendar_preparation=lambda **kwargs: {
                "status": "bound",
                "payload": source,
            }
        ),
        "schedule",
    )
    assert result["conversation_id"] == "target"
    assert ledger.recover_source("key", turn) == source


def test_unavailable_selection_rejects_before_reservation_or_model_write(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task)),
    )
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": "target",
        "model": "new",
        "tool_selection": {"mode": "manual", "include": ["missing"]},
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)

    def invoke(contract, operation, payload):
        assert contract == "tobkiri.resource.tool.definition.v1"
        assert payload["operation"] == "select"
        raise PermissionError("selected tool execution is unavailable")

    with pytest.raises(PermissionError, match="unavailable"):
        job.prepare_calendar_destination(
            task,
            "key",
            ledger,
            NS(invoke=invoke, providers=lambda _: []),
            "profile",
            lambda: None,
            NS(
                recover_calendar_preparation=lambda **kwargs: None,
                reserve_calendar_preparation=lambda **kwargs: pytest.fail(
                    "reserved invalid selection"
                ),
            ),
            "schedule",
        )


def test_actual_owner_busy_admission_preserves_target_model(tmp_path, monkeypatch):
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
    from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict

    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task)),
    )
    store = DurableTurnRuntime("profile", user_data_root=tmp_path)
    store.begin_saved(
        {
            "request": {
                "turn_id": "active-ui",
                "conversation_id": "target",
                "conversation_revision": 7,
                "content": "running",
            }
        }
    )
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": "target",
        "model": "new",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)
    model = {"id": "target", "model_reference": "old", "conversation_revision": 7}
    calls = []

    def invoke(contract, operation, payload):
        calls.append(payload["operation"])
        assert payload["operation"] == "get"
        return {"conversation": dict(model)}

    with pytest.raises(TurnConflict):
        job.prepare_calendar_destination(
            task,
            "key",
            ledger,
            NS(invoke=invoke, providers=lambda _: []),
            "profile",
            lambda: None,
            store,
            "schedule",
        )
    assert calls == ["get"]
    assert model["model_reference"] == "old" and model["conversation_revision"] == 7
    assert store.get(job._calendar_turn_id("profile", "schedule", "key")) is None


@pytest.mark.parametrize("mode", ["agent", "full"])
def test_autonomous_new_destination_requires_registered_workspace_before_write(
    tmp_path, mode
):
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": None,
        "model": "chosen",
        "action_approval_mode": mode,
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)
    with pytest.raises(PermissionError, match="requires a workspace"):
        job.prepare_calendar_destination(
            task,
            "key",
            ledger,
            NS(invoke=lambda *args: pytest.fail("owner operation before rejection")),
            "profile",
            lambda: None,
        )


def test_unknown_workspace_rejected_before_destination_write(tmp_path):
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": None,
        "model": "chosen",
        "workspace_id": "missing",
        "action_approval_mode": "full",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)
    seen = []

    def invoke(contract, operation, payload):
        seen.append(contract)
        assert payload == {
            "profile_id": "profile",
            "operation": "binding",
            "workspace_id": "missing",
        }
        return None

    with pytest.raises(PermissionError, match="unavailable"):
        job.prepare_calendar_destination(
            task, "key", ledger, NS(invoke=invoke), "profile", lambda: None
        )
    assert seen == ["tobkiri.resource.workspace.v1"]


def test_existing_destination_workspace_mismatch_precedes_model_cas(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task)),
    )
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": "target",
        "model": "new",
        "workspace_id": "chosen",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)
    calls = []

    def invoke(contract, operation, payload):
        calls.append(contract)
        if contract == "tobkiri.resource.workspace.v1":
            return {
                "mount": {"id": "chosen", "mount_revision": 1},
                "binding": {
                    "workspace_id": "chosen",
                    "mount_revision": 1,
                    "root_st_dev": 1,
                    "root_st_ino": 2,
                },
            }
        assert payload["operation"] == "get"
        return {
            "conversation": {
                "id": "target",
                "model_reference": "old",
                "conversation_revision": 7,
                "metadata": {"workspace_id": "other"},
            }
        }

    with pytest.raises(PermissionError, match="workspace differs"):
        job.prepare_calendar_destination(
            task,
            "key",
            ledger,
            NS(invoke=invoke, providers=lambda _: []),
            "profile",
            lambda: None,
            NS(
                recover_calendar_preparation=lambda **kwargs: None,
                reserve_calendar_preparation=lambda **kwargs: pytest.fail(
                    "reserved mismatch"
                ),
            ),
            "schedule",
        )
    assert calls == [
        "tobkiri.resource.workspace.v1",
        "tobkiri.resource.conversation.v1",
    ]


def test_registered_workspace_new_destination_association_is_owner_data_only(
    tmp_path, monkeypatch
):
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    monkeypatch.setitem(
        sys.modules,
        "ecosystem.rumi_turn_runtime_pack.runtime.scheduled_saved",
        NS(normalize_scheduled_saved_task=lambda task, **kwargs: dict(task)),
    )
    owner = ConversationStore("profile", user_data_root=tmp_path)
    task = {
        "message": "hello",
        "profile_id": "profile",
        "conversation_id": None,
        "model": "chosen",
        "workspace_id": "registered",
        "action_approval_mode": "full",
    }
    ledger = job.ScheduledSourceLedger(tmp_path, "profile")
    ledger.admit("key", task)

    def invoke(contract, operation, payload):
        if contract == "tobkiri.resource.workspace.v1":
            return {
                "mount": {"id": "registered", "mount_revision": 1},
                "binding": {
                    "workspace_id": "registered",
                    "mount_revision": 1,
                    "root_st_dev": 1,
                    "root_st_ino": 2,
                },
            }
        if contract == "tobkiri.resource.conversation.v1":
            return owner.snapshot()
        return owner.create(
            payload["conversation"], expected_revision=payload["expected_revision"]
        )

    result = job.prepare_calendar_destination(
        task,
        "key",
        ledger,
        NS(invoke=invoke, providers=lambda _: []),
        "profile",
        lambda: None,
    )
    assert "workspace_id" not in result
    record = owner.get(result["conversation_id"])
    assert record["metadata"] == {
        "calendar_occurrence": "key",
        "workspace_id": "registered",
    }
    assert "/never-copy" not in str(record)


def test_saved_client_narrows_same_session_and_preserves_saved_owner_gate(tmp_path):
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import execute_saved_turn

    calls = []
    session = NS(
        profile_id="profile",
        plan_digest="capture",
        invoke=lambda *args, **kwargs: calls.append(args) or {"actual": True},
        provider_metadata=lambda _: (),
    )
    broad = job.GlobalContractClient(
        session=session,
        allowed_contract_ids=job.CONSUMED_CONTRACTS,
        consumer_pack_id=job.PACK,
    )
    narrow = job._saved_execution_client(broad, "profile")
    assert narrow.session is broad.session
    assert narrow.allowed_contract_ids == job.SAVED_CONTRACTS
    assert broad.allowed_contract_ids == job.CONSUMED_CONTRACTS
    narrow.invoke(next(iter(job.SAVED_CONTRACTS)), "operation", {})
    assert len(calls) == 1
    with pytest.raises(PermissionError, match="not declared"):
        narrow.invoke("tobkiri.action.conversation.manage.v1", "update", {})

    class ReachedGuard(Exception):
        pass

    def guard():
        raise ReachedGuard()

    source = {
        "request": {
            "turn_id": "calendar-test",
            "conversation_id": "target",
            "conversation_revision": 1,
            "content": "hello",
        }
    }
    store = NS(profile_id="profile")
    with pytest.raises(ReachedGuard):
        execute_saved_turn(store, source, client=narrow, guard=guard)
    with pytest.raises(PermissionError, match="does not match the owner"):
        execute_saved_turn(store, source, client=broad, guard=guard)


@pytest.mark.parametrize("change", ["profile", "consumer", "contracts", "credential"])
def test_saved_client_rejects_foreign_or_incomplete_owner_capture(change):
    values = {
        "session": NS(profile_id="profile"),
        "allowed_contract_ids": job.CONSUMED_CONTRACTS,
        "consumer_pack_id": job.PACK,
    }
    if change == "profile":
        values["session"] = NS(profile_id="foreign")
    elif change == "consumer":
        values["consumer_pack_id"] = "other_pack"
    elif change == "contracts":
        values["allowed_contract_ids"] = frozenset()
    else:
        values["host_credential_transport"] = object()
    with pytest.raises(PermissionError, match="owner differs"):
        job._saved_execution_client(job.GlobalContractClient(**values), "profile")


def test_adapter_preparation_and_saved_execution_share_one_immutable_capture(
    tmp_path, monkeypatch
):
    run = captured(tmp_path)
    invocation_scope = invocation()
    bindings = []
    session = NS(profile_id="profile")
    broad = job.GlobalContractClient(
        session=session,
        allowed_contract_ids=job.CONSUMED_CONTRACTS,
        consumer_pack_id=job.PACK,
    )

    def capture(**kwargs):
        assert not bindings, "Host capture must never be rebound"
        bindings.append(kwargs)
        return broad

    invocation_scope.contract_client = capture

    def prepare(task, key, ledger, client, *args):
        assert client is broad
        return task

    def helper(task, schedule, execution, **ports):
        assert ports["client"].session is session
        assert ports["client"].allowed_contract_ids == job.SAVED_CONTRACTS
        assert ports["guard"] == invocation_scope.assert_current
        assert ports["track_execution"] == invocation_scope.cancellation.track
        return {"status": "completed"}

    monkeypatch.setattr(job, "prepare_calendar_destination", prepare)
    monkeypatch.setattr(job, "execute_scheduled_task", helper)
    assert run(job.OPERATION, envelope(), invocation_scope)["status"] == "completed"
    assert (
        len(bindings) == 1
        and bindings[0]["allowed_contract_ids"] == job.CONSUMED_CONTRACTS
    )
