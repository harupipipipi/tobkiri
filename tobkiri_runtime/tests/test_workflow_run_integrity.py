"""Regression proofs for replay and atomic occurrence ownership."""
from copy import deepcopy

import pytest

from core_runtime.workflow_v4 import WorkflowConflict
from tests.test_workflow_v4 import definition, runtime as runtime


def _published(provider):
    document = definition()
    second = deepcopy(document["steps"][0])
    second.update(id="later", depends_on=["echo"])
    document["steps"].append(second)
    created = provider.invoke("definition.create", {
        "definition_id": "workflow.integrity", "document": document,
    })
    provider.invoke("definition.publish", {
        "definition_id": "workflow.integrity", "if_match": created["etag"],
    })


def test_successful_step_cannot_be_dispatched_again_with_unused_retry_budget(runtime):
    provider, _, _, invoker = runtime
    _published(provider)
    provider.invoke("run.create", {
        "definition_id": "workflow.integrity", "run_id": "replay",
        "inputs": {"message": "once"},
    })
    first = provider.invoke("run.step.execute", {"run_id": "replay", "step_id": "echo"})
    assert first["state"] == "succeeded"
    with pytest.raises(WorkflowConflict):
        provider.invoke("run.step.execute", {"run_id": "replay", "step_id": "echo"})
    assert len(invoker.requests) == 1


def test_failed_run_insert_does_not_claim_fresh_occurrence(runtime):
    provider, _, _, _ = runtime
    _published(provider)
    common = {"definition_id": "workflow.integrity", "inputs": {"message": "once"}}
    provider.invoke("run.create", {**common, "run_id": "existing"})
    with pytest.raises(WorkflowConflict):
        provider.invoke("run.create", {
            **common, "run_id": "existing", "occurrence_id": "fresh-occurrence",
        })
    created = provider.invoke("run.create", {
        **common, "run_id": "new-run", "occurrence_id": "fresh-occurrence",
    })
    assert created["run_id"] == "new-run"


def test_reservation_failure_is_terminal_without_dispatch(runtime, monkeypatch):
    provider, _, authority, invoker = runtime
    _published(provider)
    provider.invoke("run.create", {"definition_id": "workflow.integrity", "run_id": "reserve-fail", "inputs": {"message": "once"}})
    def fail(_request):
        raise RuntimeError("reservation unavailable")
    monkeypatch.setattr(authority, "reserve", fail)
    with pytest.raises(RuntimeError, match="reservation unavailable"):
        provider.invoke("run.step.execute", {"run_id": "reserve-fail", "step_id": "echo"})
    assert provider._engine.store.get_run("reserve-fail")["state"] == "failed"
    assert provider._engine.store.list_attempts("reserve-fail")[0]["state"] == "failed"
    assert invoker.requests == []


def test_crash_before_dispatch_recovers_to_failure_without_replay(runtime, monkeypatch, tmp_path):
    from core_runtime.workflow_v4 import WorkflowEngineV4, WorkflowStoreV4
    from tests.test_workflow_v4 import Authority, Validator
    provider, catalog, authority, invoker = runtime
    _published(provider)
    provider.invoke("run.create", {"definition_id": "workflow.integrity", "run_id": "crash", "inputs": {"message": "once"}})
    class SimulatedCrash(BaseException):
        pass
    def crash(_request):
        raise SimulatedCrash()
    monkeypatch.setattr(authority, "reserve", crash)
    with pytest.raises(SimulatedCrash):
        provider.invoke("run.step.execute", {"run_id": "crash", "step_id": "echo"})
    provider._engine.store.close()
    engine = WorkflowEngineV4(
        store=WorkflowStoreV4(tmp_path / "workflow.sqlite3", clock=lambda: 100.0),
        catalog=catalog, authority=Authority(), invoker=invoker, validator=Validator(), clock=lambda: 100.0,
    )
    try:
        assert engine.reconcile_recovery("crash")["state"] == "failed"
        assert engine.store.list_attempts("crash")[0]["error_code"] == "interrupted_before_dispatch"
        with pytest.raises(WorkflowConflict):
            engine.execute_step("crash", "echo")
        assert invoker.requests == []
    finally:
        engine.store.close()
