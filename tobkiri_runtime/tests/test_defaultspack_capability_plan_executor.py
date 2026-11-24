"""Single Capability Plan executor lifecycle tests.

``domain.capability.plan_executor`` is the one execution boundary shared by
the capability API, the retired tool-invoke compatibility route, and the pack
function tool dispatcher.  These tests pin the durable
validate -> approve -> claim -> dispatch -> complete contract: replay is
rejected, post-claim failures are journaled as ``outcome_unknown`` and can
never be retried, and pre-claim failures leave the grant consumable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from core_runtime.capability_plan import (  # noqa: E402
    CapabilityPlanValidationError,
    canonical_capability_plan_digest,
)
from domain.capability import plan_executor  # noqa: E402
from domain.capability.repository import (  # noqa: E402
    CapabilityOwnerMismatch,
    CapabilityPlanAlreadyExecuted,
    CapabilityRepository,
    StaleCapabilityPlan,
)


_OWNER = {
    "principal_id": "alice",
    "workspace_id": "workspace-a",
    "conversation_id": "conversation-a",
    "profile_id": "profile-a",
}

_INVOCATION = {
    "tool_id": "calculator",
    "arguments": {"expression": "1 + 1"},
}


def _plan() -> dict[str, Any]:
    plan = {
        "schema_version": "tobkiri.capability-plan/v1",
        "plan_id": "plan_exec",
        "trace_id": "trace_exec",
        "registry_revision": "registry-1",
        "policy_revision": "policy-1",
        "effective_capabilities": [],
        "provider_selections": {},
        "tools": {
            "attached": ["calculator"],
            "selected": ["calculator"],
            "schema_hashes": {},
        },
    }
    plan["digest"] = canonical_capability_plan_digest(plan)
    return plan


def _approved(
    tmp_path: Path,
    *,
    invocation: dict[str, Any] | None = None,
) -> tuple[CapabilityRepository, dict[str, Any], dict[str, Any], dict[str, Any]]:
    repository = CapabilityRepository(tmp_path)
    plan = _plan()
    repository.put_plan(plan, owner=_OWNER)
    record = repository.approve_plan(
        plan["plan_id"],
        registry_revision="registry-1",
        policy_revision="policy-1",
        approved_effects=[],
        principal_id=_OWNER["principal_id"],
        owner=_OWNER,
        invocation=invocation if invocation is not None else _INVOCATION,
    )
    return repository, plan, record["approval"], dict(
        invocation if invocation is not None else _INVOCATION
    )


class _RecordingToolExecutor:
    """Gated executor stand-in that records the injected plan context."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.result = (
            result
            if result is not None
            else {"result": "done", "is_error": False, "widget": None}
        )

    def execute(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((tool_id, dict(arguments), dict(context)))
        return self.result


class _ExplodingToolExecutor:
    def execute(self, *args: Any) -> dict[str, Any]:
        raise RuntimeError("dispatch blew up mid-effect")


def test_claim_execute_complete_lifecycle(tmp_path: Path) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)
    tool_executor = _RecordingToolExecutor()

    stored = plan_executor.execute(
        plan,
        approval,
        invocation,
        {"flow_id": "capability-api"},
        owner=_OWNER,
        repository=repository,
        tool_executor=tool_executor,
    )

    assert stored["state"] == "succeeded"
    assert stored["execution"]["status"] == "succeeded"
    assert stored["execution"]["result"] == {
        "result": "done",
        "is_error": False,
        "widget": None,
    }
    (tool_id, arguments, context) = tool_executor.calls[0]
    assert tool_id == "calculator"
    assert arguments == {"expression": "1 + 1"}
    # The executor injects the canonical plan and the four-field owner scope.
    assert context["capability_plan"]["plan_id"] == "plan_exec"
    assert context["capability_plan_owner"] == _OWNER
    for field, value in _OWNER.items():
        assert context[field] == value


def test_replay_is_rejected_after_success(tmp_path: Path) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)
    tool_executor = _RecordingToolExecutor()
    plan_executor.execute(
        plan,
        approval,
        invocation,
        {},
        owner=_OWNER,
        repository=repository,
        tool_executor=tool_executor,
    )

    with pytest.raises(CapabilityPlanAlreadyExecuted):
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=tool_executor,
        )
    assert len(tool_executor.calls) == 1


def test_exception_after_claim_marks_outcome_unknown(tmp_path: Path) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)

    with pytest.raises(plan_executor.CapabilityPlanExecutionFailed) as raised:
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_ExplodingToolExecutor(),
        )

    assert raised.value.status == "outcome_unknown"
    assert raised.value.error_code == "OUTCOME_UNKNOWN"
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "outcome_unknown"
    assert "dispatch blew up mid-effect" in record["execution"]["error"]

    # A claimed-then-unknown plan can never be retried automatically.
    with pytest.raises(CapabilityPlanAlreadyExecuted):
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(),
        )


def test_pre_effect_rejection_marks_failed_pre_effect(tmp_path: Path) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)
    rejected = {
        "result": "CapabilityPlan is required for tool execution",
        "is_error": True,
        "widget": None,
        "error_type": "capability_plan_required",
    }

    with pytest.raises(plan_executor.CapabilityPlanExecutionFailed) as raised:
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(result=rejected),
        )

    assert raised.value.status == "failed_pre_effect"
    assert raised.value.error_code == "CAPABILITY_PLAN_REQUIRED"
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "failed_pre_effect"
    with pytest.raises(CapabilityPlanAlreadyExecuted):
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(),
        )


def test_error_result_without_pre_effect_marker_is_outcome_unknown(
    tmp_path: Path,
) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)
    failed = {
        "result": "Capability execution failed: backend exploded",
        "is_error": True,
        "widget": None,
    }

    with pytest.raises(plan_executor.CapabilityPlanExecutionFailed) as raised:
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(result=failed),
        )

    assert raised.value.status == "outcome_unknown"
    assert raised.value.error_code == "OUTCOME_UNKNOWN"
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "outcome_unknown"


def test_invalid_plan_is_rejected_before_any_claim(tmp_path: Path) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)
    forged = dict(plan)
    forged["plan_id"] = "plan_exec_forged"
    tool_executor = _RecordingToolExecutor()

    with pytest.raises(CapabilityPlanValidationError):
        plan_executor.execute(
            forged,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=tool_executor,
        )

    assert tool_executor.calls == []
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "approved"


def test_tampered_approval_record_is_rejected_before_claim(
    tmp_path: Path,
) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)
    tool_executor = _RecordingToolExecutor()
    tampered = dict(approval)
    tampered["registry_revision"] = "registry-2"

    with pytest.raises(StaleCapabilityPlan):
        plan_executor.execute(
            plan,
            tampered,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=tool_executor,
        )

    assert tool_executor.calls == []
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "approved"


def test_invocation_mismatch_is_rejected_before_claim(tmp_path: Path) -> None:
    repository, plan, approval, _ = _approved(tmp_path)
    tool_executor = _RecordingToolExecutor()

    with pytest.raises(StaleCapabilityPlan, match="invocation changed"):
        plan_executor.execute(
            plan,
            approval,
            {"tool_id": "calculator", "arguments": {"expression": "9 * 9"}},
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=tool_executor,
        )

    assert tool_executor.calls == []
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "approved"


def test_missing_plan_is_rejected(tmp_path: Path) -> None:
    repository = CapabilityRepository(tmp_path)
    plan = _plan()

    with pytest.raises(KeyError):
        plan_executor.execute(
            plan,
            {"registry_revision": "registry-1"},
            _INVOCATION,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(),
        )


def test_unapproved_plan_is_rejected(tmp_path: Path) -> None:
    repository = CapabilityRepository(tmp_path)
    plan = _plan()
    repository.put_plan(plan, owner=_OWNER)

    with pytest.raises(PermissionError, match="not approved"):
        plan_executor.execute(
            plan,
            {"registry_revision": "registry-1"},
            _INVOCATION,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(),
        )


def test_owner_mismatch_is_rejected(tmp_path: Path) -> None:
    repository, plan, approval, invocation = _approved(tmp_path)
    intruder = {**_OWNER, "principal_id": "mallory"}

    with pytest.raises(CapabilityOwnerMismatch):
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=intruder,
            repository=repository,
            tool_executor=_RecordingToolExecutor(),
        )

    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "approved"


def test_stored_plan_with_secret_shaped_keys_still_executes(
    tmp_path: Path,
) -> None:
    """Storage redacts secret-shaped keys; execute must not fail stale on it.

    ``put_plan`` persists ``_redact_for_storage(plan)``, so a signed plan whose
    ``provider_selections`` key contains ``credential``/``session``/``token``
    material arrives from the repository with that value as ``[REDACTED]``.
    The stored comparison must account for redaction or every such plan would
    be rejected as tampered before the claim.
    """

    repository = CapabilityRepository(tmp_path)
    plan = _plan()
    plan["provider_selections"] = {
        "rumi.resource.credential.status.v1": ["credential.local"],
    }
    plan["digest"] = canonical_capability_plan_digest(plan)
    repository.put_plan(plan, owner=_OWNER)
    record = repository.approve_plan(
        plan["plan_id"],
        registry_revision="registry-1",
        policy_revision="policy-1",
        approved_effects=[],
        principal_id=_OWNER["principal_id"],
        owner=_OWNER,
        invocation=_INVOCATION,
    )

    stored = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert stored["plan"]["provider_selections"] == {
        "rumi.resource.credential.status.v1": "[REDACTED]"
    }

    done = plan_executor.execute(
        plan,
        record["approval"],
        dict(_INVOCATION),
        {},
        owner=_OWNER,
        repository=repository,
        tool_executor=_RecordingToolExecutor(),
    )
    assert done["state"] == "succeeded"


def test_executor_setup_failure_after_claim_is_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure between claim and dispatch must still journal the grant."""

    repository, plan, approval, invocation = _approved(tmp_path)

    def _no_executor() -> Any:
        raise RuntimeError("executor construction failed")

    monkeypatch.setattr(plan_executor, "_tool_executor", _no_executor)

    with pytest.raises(plan_executor.CapabilityPlanExecutionFailed) as raised:
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
        )

    assert raised.value.status == "outcome_unknown"
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "outcome_unknown"


def test_completion_write_failure_after_success_is_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed durable completion write must not strand 'executing'."""

    repository, plan, approval, invocation = _approved(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("completion write failed")

    monkeypatch.setattr(repository, "complete_execution", _boom)

    with pytest.raises(plan_executor.CapabilityPlanExecutionFailed) as raised:
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(),
        )

    assert raised.value.status == "outcome_unknown"


def test_unbound_capability_dispatch_is_failed_pre_effect(tmp_path: Path) -> None:
    """A deterministic not-bound dispatch result is pre-effect, not unknown."""

    repository, plan, approval, invocation = _approved(tmp_path)
    unbound = {
        "result": (
            "Capability execution failed: CapabilityExecutor is not bound; "
            "implicit executor creation is forbidden"
        ),
        "is_error": True,
        "widget": None,
        "error_type": "capability_executor_unbound",
    }

    with pytest.raises(plan_executor.CapabilityPlanExecutionFailed) as raised:
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(result=unbound),
        )

    assert raised.value.status == "failed_pre_effect"
    assert raised.value.error_code == "CAPABILITY_EXECUTOR_UNBOUND"
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "failed_pre_effect"


class _ForeignPlanFailureExecutor:
    """Stand-in raising the executor failure type from a nested boundary."""

    def execute(self, *args: Any) -> dict[str, Any]:
        raise plan_executor.CapabilityPlanExecutionFailed(
            "nested execution boundary failed",
            status="failed_pre_effect",
        )


class _InterruptingToolExecutor:
    def execute(self, *args: Any) -> dict[str, Any]:
        raise KeyboardInterrupt()


def _tool_schema_hash(tool: dict[str, Any]) -> str:
    schema = tool.get("schema")
    if not isinstance(schema, dict):
        contract = tool.get("contract")
        schema = (
            contract.get("input_schema")
            if isinstance(contract, dict)
            and isinstance(contract.get("input_schema"), dict)
            else {}
        )
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def test_secret_keyed_plan_executes_via_capability_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real api._execute + ToolExecutor path runs a redacted record.

    Storage redaction rewrites secret-shaped keys, secret-looking values and
    oversized strings, so the stored plan's embedded digest no longer
    recomputes.  The whole production seam — api._execute, plan_executor and
    the real ToolExecutor plan gate — must still execute; only the leaf memo
    handler is stubbed.
    """

    from blocks.capability import api
    from domain.tool.registry import ToolRegistry
    import blocks.memory.memo_notes as memo_notes

    tool = ToolRegistry().get("memo_search")
    assert isinstance(tool, dict)

    repository = CapabilityRepository(tmp_path)
    plan = {
        "schema_version": "tobkiri.capability-plan/v1",
        "plan_id": "plan_secret_keys",
        "trace_id": "trace_secret_keys",
        "registry_revision": "registry-1",
        "policy_revision": "policy-1",
        "policy_generation": repository.policy_generation(),
        "effective_capabilities": [],
        "provider_selections": {
            "credential_contract": ["rumi.resource.credential.status.v1"],
        },
        "tools": {
            "attached": ["memo_search"],
            "selected": ["memo_search"],
            "schema_hashes": {"memo_search": _tool_schema_hash(tool)},
        },
        "arguments": {"api_key": "sk-live-secret-value"},
        "notes": "x" * 5000,
    }
    plan["digest"] = canonical_capability_plan_digest(plan)
    repository.put_plan(plan, owner=_OWNER)

    invocation = {"tool_id": "memo_search", "arguments": {"query": "hello"}}
    approved = api._approve(
        repository,
        {
            "plan_id": plan["plan_id"],
            "registry_revision": "registry-1",
            "policy_revision": "policy-1",
            "invocation": dict(invocation),
        },
        dict(_OWNER),
    )
    assert approved["status"] == "ok", approved

    handler_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def _stub_handler(arguments, context=None):
        handler_calls.append((dict(arguments or {}), dict(context or {})))
        return {"status": "ok", "data": {"notes": []}}

    monkeypatch.setattr(memo_notes, "tool_search_notes", _stub_handler)

    context = dict(_OWNER)
    context["profile_policy"] = {"yolo_mode": True}
    result = api._execute(
        repository,
        {"plan_id": plan["plan_id"], "invocation": dict(invocation)},
        context,
    )
    assert result["status"] == "ok", result
    assert result["data"]["state"] == "succeeded"
    assert len(handler_calls) == 1
    # The persisted redacted plan (not the secret-bearing original) is what
    # reaches the gated executor context.
    (_, tool_context) = handler_calls[0]
    plan_context = tool_context.get("capability_plan")
    assert plan_context["arguments"]["api_key"] == "[REDACTED]"


def test_tampered_stored_plan_echo_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored-echo variant that does not match the record must not run."""

    from blocks.capability import api
    from domain.tool.registry import ToolRegistry
    import blocks.memory.memo_notes as memo_notes

    tool = ToolRegistry().get("memo_search")
    assert isinstance(tool, dict)

    repository = CapabilityRepository(tmp_path)
    plan = _plan()
    plan["tools"] = {
        "attached": ["memo_search"],
        "selected": ["memo_search"],
        "schema_hashes": {"memo_search": _tool_schema_hash(tool)},
    }
    plan["digest"] = canonical_capability_plan_digest(plan)
    repository.put_plan(plan, owner=_OWNER)

    invocation = {"tool_id": "memo_search", "arguments": {"query": "hello"}}
    approved = api._approve(
        repository,
        {
            "plan_id": plan["plan_id"],
            "registry_revision": "registry-1",
            "policy_revision": "policy-1",
            "invocation": dict(invocation),
        },
        dict(_OWNER),
    )
    assert approved["status"] == "ok", approved

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("handler must not run for a tampered plan")

    monkeypatch.setattr(memo_notes, "tool_search_notes", _boom)

    # Attacker-controlled echo: stored record with a widened attached set.
    forged = repository.get_plan(plan["plan_id"], owner=_OWNER)["plan"]
    forged = dict(forged)
    forged["tools"] = dict(forged["tools"])
    forged["tools"]["attached"] = ["memo_search", "web_search"]

    context = dict(_OWNER)
    context["profile_policy"] = {"yolo_mode": True}
    with pytest.raises((StaleCapabilityPlan, CapabilityPlanValidationError)):
        plan_executor.execute(
            forged,
            repository.get_plan(plan["plan_id"], owner=_OWNER)["approval"],
            invocation,
            context,
            owner=_OWNER,
            repository=repository,
        )
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "approved"


def test_foreign_plan_failure_after_claim_is_journaled(tmp_path: Path) -> None:
    """A CapabilityPlanExecutionFailed escaping dispatch still journals."""

    repository, plan, approval, invocation = _approved(tmp_path)

    with pytest.raises(plan_executor.CapabilityPlanExecutionFailed) as raised:
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_ForeignPlanFailureExecutor(),
        )

    assert "nested execution boundary failed" in str(raised.value)
    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "outcome_unknown"
    with pytest.raises(CapabilityPlanAlreadyExecuted):
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_RecordingToolExecutor(),
        )


def test_base_exception_after_claim_is_journaled(tmp_path: Path) -> None:
    """KeyboardInterrupt must not strand the record in 'executing'."""

    repository, plan, approval, invocation = _approved(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        plan_executor.execute(
            plan,
            approval,
            invocation,
            {},
            owner=_OWNER,
            repository=repository,
            tool_executor=_InterruptingToolExecutor(),
        )

    record = repository.get_plan(plan["plan_id"], owner=_OWNER)
    assert record["state"] == "outcome_unknown"
