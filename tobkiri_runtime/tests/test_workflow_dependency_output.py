"""Real Workflow engine execution with explicit isolated authority/invoker fixtures."""

from __future__ import annotations

from copy import deepcopy

import pytest

from core_runtime.workflow_v4.models import (
    ApprovalState,
    WorkflowConflict,
    WorkflowDenied,
    WorkflowValidationError,
)
from core_runtime.workflow_v4.provider import WorkflowProviderV4
from tests.test_workflow_v4 import definition

pytest_plugins = ("tests.test_workflow_v4",)


def _chain(provider: WorkflowProviderV4, *, reference: str = "${steps.owner.output.ok}") -> None:
    document = definition("owner-read")
    document["steps"][0]["id"] = "owner"
    child = deepcopy(document["steps"][0])
    child.update(id="context", depends_on=["owner"])
    child["request"]["input"] = {"message": reference}
    document["steps"].append(child)
    created = provider.invoke("definition.create", {"definition_id": "chain", "document": document})
    provider.invoke("definition.publish", {"definition_id": "chain", "if_match": created["etag"]})
    provider.invoke("run.create", {"definition_id": "chain", "inputs": {}, "run_id": "chain-run"})


def test_dependency_output_is_materialized_before_normal_attempt_authority(runtime) -> None:
    provider, _catalog, authority, invoker = runtime
    _chain(provider)
    provider.invoke("run.advance", {"run_id": "chain-run"})
    result = provider.invoke("run.advance", {"run_id": "chain-run"})
    assert result["run"]["state"] == "succeeded"
    assert invoker.requests[1]["input"] == {"message": True}
    assert authority.commit_count == 2
    assert provider._engine.store.list_attempts("chain-run")[1]["outcome"] == {"ok": True}


@pytest.mark.parametrize(
    "reference",
    [
        "${steps.other.output.ok}",
        "${steps.echo.output.ok}",
        "${steps.owner.output.ok + 1}",
        "prefix ${steps.owner.output.ok}",
    ],
)
def test_undeclared_or_executable_output_expression_fails_before_attempt_authority(
    runtime, reference
) -> None:
    provider, _catalog, authority, invoker = runtime
    with pytest.raises(WorkflowValidationError):
        _chain(provider, reference=reference)
    assert not authority.reservations and not invoker.requests


def test_waiting_dependency_cannot_supply_output_or_autoapprove(runtime) -> None:
    provider, _catalog, authority, invoker = runtime
    authority.state = ApprovalState.WAITING_APPROVAL
    _chain(provider)
    result = provider.invoke("run.advance", {"run_id": "chain-run"})
    assert result["run"]["state"] == "waiting_approval"
    with pytest.raises(WorkflowConflict, match="dependencies"):
        provider.invoke("run.step.execute", {"run_id": "chain-run", "step_id": "context"})
    assert not invoker.requests and authority.commit_count == 0


def test_skipped_dependency_has_no_trusted_output(runtime) -> None:
    provider, _catalog, authority, invoker = runtime
    document = definition("unused")
    document["steps"][0].update(id="owner", when="false")
    child = deepcopy(document["steps"][0])
    child.update(id="context", when="true", depends_on=["owner"])
    child["request"]["input"] = {"message": "${steps.owner.output}"}
    document["steps"].append(child)
    created = provider.invoke("definition.create", {"definition_id": "chain", "document": document})
    provider.invoke("definition.publish", {"definition_id": "chain", "if_match": created["etag"]})
    provider.invoke("run.create", {"definition_id": "chain", "inputs": {}, "run_id": "chain-run"})
    provider.invoke("run.advance", {"run_id": "chain-run"})
    with pytest.raises(WorkflowDenied, match="unique successful output"):
        provider.invoke("run.step.execute", {"run_id": "chain-run", "step_id": "context"})
    assert not invoker.requests and authority.commit_count == 0


def test_selected_source_drives_engine_and_replay_cannot_change_inputs(
    runtime, tmp_path, monkeypatch
):
    from tests.test_profile_workflow_catalog import _selection, _view
    from core_runtime.resolved_profile_scope import (
        activate_resolved_profile,
        restore_resolved_profile,
    )

    selection = _selection(tmp_path, monkeypatch)
    token = activate_resolved_profile(_view(selection))
    provider, _catalog, authority, invoker = runtime
    payload = {
        "definition_id": "example",
        "inputs": {"message": "original"},
        "occurrence_id": "occurrence",
    }
    try:
        first = provider.invoke("run.selected", payload)
        assert first["run"]["state"] == "succeeded"
        assert authority.commit_count == 1 and invoker.requests[0]["input"] == {
            "message": "original"
        }
        replay = provider.invoke("run.selected", payload)
        assert replay["run"]["run_id"] == first["run"]["run_id"] and authority.commit_count == 1
        with pytest.raises(WorkflowDenied, match="stale or altered"):
            provider.invoke("run.selected", {**payload, "inputs": {"message": "changed"}})
        assert authority.commit_count == 1
    finally:
        restore_resolved_profile(token)


def test_selected_source_preserves_waiting_and_rejects_unselected(runtime, tmp_path, monkeypatch):
    from tests.test_profile_workflow_catalog import _selection, _view
    from core_runtime.resolved_profile_scope import (
        activate_resolved_profile,
        restore_resolved_profile,
    )

    selection = _selection(tmp_path, monkeypatch)
    token = activate_resolved_profile(_view(selection))
    provider, _catalog, authority, invoker = runtime
    authority.state = ApprovalState.WAITING_APPROVAL
    try:
        result = provider.invoke(
            "run.selected",
            {
                "definition_id": "example",
                "inputs": {"message": "test"},
                "occurrence_id": "occurrence",
            },
        )
        assert result["run"]["state"] == "waiting_approval" and not invoker.requests
        assert authority.commit_count == 0
    finally:
        restore_resolved_profile(token)
    token = activate_resolved_profile(_view())
    try:
        with pytest.raises(ValueError, match="outside"):
            provider.invoke(
                "run.selected",
                {"definition_id": "example", "inputs": {}, "occurrence_id": "occurrence"},
            )
    finally:
        restore_resolved_profile(token)
