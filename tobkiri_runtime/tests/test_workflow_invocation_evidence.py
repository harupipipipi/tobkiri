"""Workflow lineage is read from durable execution, never supplied JSON."""
from dataclasses import replace
from copy import deepcopy

import pytest

from core_runtime.invocation_evidence import EvidenceBinding, EvidenceDenied
from core_runtime.workflow_v4.evidence import WorkflowAttemptEvidence
from tests.test_workflow_v4_step_output_binding import runtime as runtime, start_chain_run
from tobkiri_protocol.canonical import canonical_digest


def _binding(run, request):
    from tobkiri_protocol.canonical import canonical_digest
    return EvidenceBinding(
        issuer_principal="workflow-principal", target_principal=request["function_principal_id"],
        contract_id=request["contract_id"], contract_version="1.0.0",
        operation_id=request["operation_id"], payload_digest=canonical_digest(request["input"]),
        idempotency_key=request["idempotency_key"], profile_id="profile",
        plan_digest="sha256:" + "4" * 64, activation_id=run["activation_id"],
        activation_digest=run["activation_digest"], security_epoch=run["security_epoch"],
        presentation_owner_principal="owner", presentation_owner_session="session",
    )


def test_live_attempt_resolves_only_its_committed_input_lineage(runtime, monkeypatch):
    provider, _, _, invoker = runtime
    start_chain_run(provider, "lineage")
    observed = []
    original = invoker.invoke
    def invoke(request, **kwargs):
        if request["operation_id"] == "tts.run":
            store = provider._engine.store
            run = store.get_run("lineage")
            evidence = WorkflowAttemptEvidence(store, request["request_id"], _binding(run, request))
            observed.append(evidence.read("input:/text"))
            with pytest.raises(EvidenceDenied, match="no committed output binding"):
                evidence.read("input:/voice")
            with pytest.raises(EvidenceDenied):
                evidence.read("other-run")
        return original(request, **kwargs)
    monkeypatch.setattr(invoker, "invoke", invoke)
    for step in ("stt", "llm", "tts"):
        result = provider.invoke("run.step.execute", {"run_id": "lineage", "step_id": step})
        assert result["state"] == "succeeded"
    assert observed[0]["value"] == "echo:hello world"
    assert observed[0]["source_step"] == "llm"
    assert [item["step_id"] for item in observed[0]["lineage"]] == ["stt", "llm"]
    assert observed[0]["run_id"] == "lineage"
    assert all("request" not in item and "outcome" not in item for item in observed[0]["lineage"])
    assert "file://in.mp3" not in str(observed[0])


@pytest.mark.parametrize("change", [
    {"idempotency_key": "other-attempt"}, {"target_principal": "other"},
    {"payload_digest": "changed"}, {"activation_id": "other"},
    {"security_epoch": 99}, {"operation_id": "other"},
])
def test_owner_evidence_rejects_dispatch_substitution(runtime, monkeypatch, change):
    provider, _, _, invoker = runtime
    start_chain_run(provider, "binding")
    checked = []
    original = invoker.invoke
    def invoke(request, **kwargs):
        store = provider._engine.store
        binding = _binding(store.get_run("binding"), request)
        with pytest.raises(EvidenceDenied, match="does not cover"):
            WorkflowAttemptEvidence(store, request["request_id"], replace(binding, **change))
        checked.append(True)
        return original(request, **kwargs)
    monkeypatch.setattr(invoker, "invoke", invoke)
    provider.invoke("run.step.execute", {"run_id": "binding", "step_id": "stt"})
    assert checked == [True]


@pytest.mark.parametrize("kind", ["run-id", "revision-id", "catalog", "compile-digest"])
def test_sealed_record_relabeling_is_rejected(runtime, monkeypatch, kind):
    from copy import deepcopy
    provider, _, _, invoker = runtime
    start_chain_run(provider, "relabel")
    original = invoker.invoke
    checked = []
    def invoke(request, **kwargs):
        store = provider._engine.store
        binding = _binding(store.get_run("relabel"), request)
        with monkeypatch.context() as local:
            if kind == "run-id":
                getter = store.get_run
                local.setattr(store, "get_run", lambda key: {**getter(key), "run_id": "another-run"})
            else:
                getter = store.get_revision
                def changed(key):
                    record = deepcopy(getter(key))
                    if kind == "revision-id":
                        record["revision_digest"] = "sha256:" + "0" * 64
                    elif kind == "catalog":
                        record["compiled"]["catalog_digest"] = "sha256:" + "0" * 64
                    else:
                        record["compiled"]["compile_digest"] = "sha256:" + "0" * 64
                    return record
                local.setattr(store, "get_revision", changed)
            with pytest.raises(EvidenceDenied):
                WorkflowAttemptEvidence(store, request["request_id"], binding)
        checked.append(True)
        return original(request, **kwargs)
    monkeypatch.setattr(invoker, "invoke", invoke)
    provider.invoke("run.step.execute", {"run_id": "relabel", "step_id": "stt"})
    assert checked == [True]


def test_current_binds_full_run_input_without_exposing_content(runtime, monkeypatch):
    provider, _, _, invoker = runtime
    start_chain_run(provider, "current-input")
    original = invoker.invoke
    observations = []

    def invoke(request, **kwargs):
        store = provider._engine.store
        run = store.get_run("current-input")
        view = WorkflowAttemptEvidence(store, request["request_id"], _binding(run, request)).read("current")
        assert view["definition_id"] == "workflow.current-input"
        assert view["run_inputs_digest"] == canonical_digest(run["inputs"])
        assert "file://in.mp3" not in str(view)
        assert "inputs" not in view and "lineage" not in view
        observations.append(view)
        return original(request, **kwargs)

    monkeypatch.setattr(invoker, "invoke", invoke)
    for step in ("stt", "llm", "tts"):
        assert provider.invoke("run.step.execute", {"run_id": "current-input", "step_id": step})["state"] == "succeeded"
    assert len(observations) == 3


def test_current_rejects_matching_dispatch_with_substituted_materialized_input(runtime, monkeypatch):
    provider, _, _, invoker = runtime
    start_chain_run(provider, "substituted-input")
    original = invoker.invoke
    checked = []

    def invoke(request, **kwargs):
        store = provider._engine.store
        changed = deepcopy(request)
        changed["input"]["audio"] = "file://different.mp3"
        getter = store.get_attempt_for_request
        with monkeypatch.context() as local:
            def changed_attempt(reference):
                return {**getter(reference), "request": changed, "request_digest": canonical_digest(changed)}
            local.setattr(store, "get_attempt_for_request", changed_attempt)
            evidence = WorkflowAttemptEvidence(store, request["request_id"], _binding(store.get_run("substituted-input"), changed))
            with pytest.raises(EvidenceDenied, match="input lineage"):
                evidence.read("current")
        checked.append(True)
        return original(request, **kwargs)

    monkeypatch.setattr(invoker, "invoke", invoke)
    provider.invoke("run.step.execute", {"run_id": "substituted-input", "step_id": "stt"})
    assert checked == [True]


def test_current_checks_upstream_materialization_not_only_immediate_request(runtime, monkeypatch):
    provider, _, _, invoker = runtime
    start_chain_run(provider, "upstream-current")
    original = invoker.invoke
    checked = []

    def invoke(request, **kwargs):
        store = provider._engine.store
        if request["operation_id"] == "tts.run":
            getter = store.list_attempts
            def changed_attempts(run_id):
                records = deepcopy(getter(run_id))
                record = next(item for item in records if item["step_id"] == "stt")
                record["request"]["input"]["audio"] = "file://substituted.mp3"
                record["request_digest"] = canonical_digest(record["request"])
                return records
            with monkeypatch.context() as local:
                local.setattr(store, "list_attempts", changed_attempts)
                evidence = WorkflowAttemptEvidence(store, request["request_id"], _binding(store.get_run("upstream-current"), request))
                with pytest.raises(EvidenceDenied, match="input lineage"):
                    evidence.read("current")
            checked.append(True)
        return original(request, **kwargs)

    monkeypatch.setattr(invoker, "invoke", invoke)
    for step in ("stt", "llm", "tts"):
        provider.invoke("run.step.execute", {"run_id": "upstream-current", "step_id": step})
    assert checked == [True]


def test_current_rechecks_liveness_after_reading_lineage(runtime, monkeypatch):
    provider, _, _, invoker = runtime
    start_chain_run(provider, "cancel-current")
    original = invoker.invoke
    checked = []

    def invoke(request, **kwargs):
        store = provider._engine.store
        evidence = WorkflowAttemptEvidence(store, request["request_id"], _binding(store.get_run("cancel-current"), request))
        getter = store.get_run
        attempts = store.list_attempts
        cancelled = False
        with monkeypatch.context() as local:
            def after_read(run_id):
                nonlocal cancelled
                result = attempts(run_id)
                cancelled = True
                return result
            local.setattr(store, "list_attempts", after_read)
            local.setattr(store, "get_run", lambda run_id: {**getter(run_id), "cancel_requested": cancelled})
            with pytest.raises(EvidenceDenied, match="does not cover"):
                evidence.read("current")
        checked.append(True)
        return original(request, **kwargs)

    monkeypatch.setattr(invoker, "invoke", invoke)
    provider.invoke("run.step.execute", {"run_id": "cancel-current", "step_id": "stt"})
    assert checked == [True]


def test_current_missing_run_template_is_evidence_denial(runtime, monkeypatch):
    provider, _, _, invoker = runtime
    start_chain_run(provider, "missing-current")
    original = invoker.invoke
    checked = []

    def invoke(request, **kwargs):
        store = provider._engine.store
        binding = _binding(store.get_run("missing-current"), request)
        getter = store.get_run
        with monkeypatch.context() as local:
            local.setattr(store, "get_run", lambda run_id: {**getter(run_id), "inputs": {}})
            evidence = WorkflowAttemptEvidence(store, request["request_id"], binding)
            with pytest.raises(EvidenceDenied, match="materialization"):
                evidence.read("current")
        checked.append(True)
        return original(request, **kwargs)

    monkeypatch.setattr(invoker, "invoke", invoke)
    provider.invoke("run.step.execute", {"run_id": "missing-current", "step_id": "stt"})
    assert checked == [True]
