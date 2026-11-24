"""Root unit regressions for continuation identity; real Broker tests are separate."""
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from core_runtime.workflow_v4.host_adapters import HostAttemptAuthorityV4
from core_runtime.workflow_v4.models import WorkflowDenied
from tests.test_authority_v4_lifecycle import _Harness, _digest
from tests.test_tobkiri_host_authority_v4_adapter import _context
from tobkiri_host.models import OpaqueAuthorityRef


def _case(tmp_path, *, identity_change=None, snapshot_change=None):
    harness = _Harness(tmp_path)
    original = _context(harness, request_id="reservation-root")
    current = replace(original, request_id="resume-root")
    values = asdict(original)
    values["caller_principal"] = original.caller_principal.value
    values["delegation_chain"] = [item.value for item in original.delegation_chain]
    snapshot = {
        "kind": "workflow-v4-approval-continuation",
        "state": "approval_continuation",
        "effect_id": "reservation-root",
        "context": values,
        "request_digest": _digest("request-root"),
        "target_principal_id": "original-operation",
        "invocation_owner_id": "owner-root",
        "presentation_owner_session_id": "session-root",
        "caller_publisher_lineage": "publisher-root",
        "target_publisher_lineage": "publisher-root",
    }
    snapshot.update(snapshot_change or {})

    def binding(principal, changes=None):
        identity = {
            "artifact": _digest("artifact-root"),
            "implementation": _digest("implementation-root"),
            "function": "workflow.execute", "revision": _digest("revision-root"),
        }
        identity.update(changes or {})
        return SimpleNamespace(
            principal_ref=OpaqueAuthorityRef(principal),
            artifact=SimpleNamespace(digest=identity["artifact"], publisher_lineage="publisher-root"),
            function=SimpleNamespace(implementation_digest=identity["implementation"], function_id=identity["function"]),
            operation=SimpleNamespace(contract_id="workflow", revision_digest=identity["revision"], operation_id=principal),
        )

    invocation = SimpleNamespace(
        envelope=SimpleNamespace(context=current, target_principal=OpaqueAuthorityRef("resume-operation")),
        presentation_owner_principal_id="owner-root",
        presentation_owner_session_id="session-root",
        assert_current=lambda: None,
    )
    authority = HostAttemptAuthorityV4(
        invocation=invocation,
        approvals=SimpleNamespace(get_host_pending_effect=lambda _: (1, snapshot)),
        approval_window=None,
        catalog_bindings=(binding("original-operation"), binding("resume-operation", identity_change)),
        caller_publisher_lineage="publisher-root",
    )
    return authority, original


def _restore(authority):
    return authority._continuation_context(
        "reservation-root", request_digest=_digest("request-root"),
        target_lineage="publisher-root", approved_target_id="original-operation",
    )


def test_operation_change_preserves_exact_captured_function_identity(tmp_path):
    authority, original = _case(tmp_path)
    assert _restore(authority) == original


@pytest.mark.parametrize("field", ["artifact", "implementation", "function", "revision"])
def test_same_presentation_caller_cannot_continue_a_different_target_identity(tmp_path, field):
    authority, _ = _case(tmp_path, identity_change={field: _digest("different-root")})
    with pytest.raises(WorkflowDenied, match="identity"):
        _restore(authority)


@pytest.mark.parametrize("change", [
    {"kind": "other"}, {"state": "completed"}, {"effect_id": "other"},
    {"target_principal_id": "other"}, {"request_digest": _digest("other")},
    {"invocation_owner_id": "other"}, {"presentation_owner_session_id": "other"},
])
def test_continuation_snapshot_cannot_change_binding(tmp_path, change):
    authority, _ = _case(tmp_path, snapshot_change=change)
    with pytest.raises(WorkflowDenied, match="identity"):
        _restore(authority)


@pytest.mark.parametrize("epoch", [0, True, 2])
def test_commit_rejects_wrong_or_boolean_epoch_before_grant_lookup(tmp_path, epoch):
    authority, _ = _case(tmp_path)
    with pytest.raises(WorkflowDenied, match="security epoch"):
        authority.commit("reservation-root", request_digest=_digest("request-root"), security_epoch=epoch)
