from pathlib import Path
import multiprocessing

import pytest

from domain.capability.repository import (
    CapabilityPlanAlreadyExecuted,
    CapabilityRepository,
    StaleCapabilityPlan,
    CapabilityOwnerMismatch,
)
from domain.capability.skill_lifecycle import SkillLifecycleStore


def _plan() -> dict:
    return {
        "schema_version": "tobkiri.capability-plan/v1",
        "plan_id": "plan_test",
        "trace_id": "trace_test",
        "registry_revision": "registry-1",
        "policy_revision": "policy-1",
    }


def _claim_worker(root: str, barrier, queue) -> None:
    repository = CapabilityRepository(Path(root))
    barrier.wait()
    try:
        repository.claim_execution(
            "plan_test",
            {"worker": multiprocessing.current_process().name},
        )
    except CapabilityPlanAlreadyExecuted:
        queue.put("already")
    else:
        queue.put("claimed")


def test_settings_are_normalized_and_atomically_persisted(tmp_path: Path):
    repository = CapabilityRepository(tmp_path)

    updated = repository.update_settings(
        {
            "enabled": False,
            "advanced": {"max_attached_tools": 99},
        }
    )

    assert updated["capabilities"]["enabled"] is False
    assert updated["capabilities"]["advanced"]["max_attached_tools"] == 8
    assert repository.settings() == updated
    assert not list(tmp_path.glob("*.tmp"))


def test_settings_reject_unknown_keys(tmp_path: Path):
    repository = CapabilityRepository(tmp_path)

    with pytest.raises(ValueError, match="unknown capability setting"):
        repository.update_settings({"secret_override": True})


def test_plan_approval_is_revision_bound_and_single_use(tmp_path: Path):
    repository = CapabilityRepository(tmp_path)
    repository.put_plan(_plan())
    assert repository.get_trace("trace_test")["plan"]["plan_id"] == "plan_test"

    with pytest.raises(StaleCapabilityPlan):
        repository.approve_plan(
            "plan_test",
            registry_revision="registry-old",
            policy_revision="policy-1",
            approved_effects=[],
            principal_id="user",
        )

    repository.approve_plan(
        "plan_test",
        registry_revision="registry-1",
        policy_revision="policy-1",
        approved_effects=[{"effect": "write", "decision": "confirm"}],
        principal_id="user",
    )
    repository.mark_executed("plan_test", {"result": "ok"})

    with pytest.raises(CapabilityPlanAlreadyExecuted):
        repository.mark_executed("plan_test", {"result": "again"})


def test_plan_claim_is_single_use_across_processes(tmp_path: Path):
    repository = CapabilityRepository(tmp_path)
    repository.put_plan(_plan())
    repository.approve_plan(
        "plan_test",
        registry_revision="registry-1",
        policy_revision="policy-1",
        approved_effects=[],
        principal_id="user",
    )
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_claim_worker,
            args=(str(tmp_path), barrier, queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(queue.get(timeout=2) for _ in processes) == [
        "already",
        "claimed",
    ]


def test_plan_owner_and_exact_invocation_are_enforced(tmp_path: Path):
    owner = {
        "principal_id": "alice",
        "workspace_id": "workspace-a",
        "conversation_id": "conversation-a",
        "profile_id": "profile-a",
    }
    repository = CapabilityRepository(tmp_path)
    repository.put_plan(_plan(), owner=owner)

    with pytest.raises(CapabilityOwnerMismatch):
        repository.get_plan(
            "plan_test",
            owner={**owner, "principal_id": "bob"},
            require_owner=True,
        )

    invocation = {"tool_id": "mail.send", "arguments": {"to": "a@example.com"}}
    repository.approve_plan(
        "plan_test",
        registry_revision="registry-1",
        policy_revision="policy-1",
        approved_effects=[],
        principal_id="alice",
        owner=owner,
        invocation=invocation,
    )
    with pytest.raises(StaleCapabilityPlan, match="invocation changed"):
        repository.claim_execution(
            "plan_test",
            {},
            owner=owner,
            invocation={
                "tool_id": "mail.send",
                "arguments": {"to": "b@example.com"},
            },
        )


def test_secret_values_are_redacted_before_sqlite_persistence(tmp_path: Path):
    repository = CapabilityRepository(tmp_path)
    plan = {
        **_plan(),
        "diagnostics": [
            {
                "authorization": "Bearer top-secret-token",
                "message": "password=hunter2",
            }
        ],
    }
    repository.put_plan(plan)

    persisted = (tmp_path / "capabilities.sqlite3").read_bytes()
    assert b"top-secret-token" not in persisted
    assert b"hunter2" not in persisted
    assert repository.get_plan("plan_test")["plan"]["diagnostics"][0][
        "authorization"
    ] == "[REDACTED]"
    assert repository.get_plan("plan_test")["plan"]["diagnostics"][0][
        "message"
    ] == "[REDACTED]"


def test_policy_generation_is_monotonic_even_when_settings_return_to_a(tmp_path: Path):
    repository = CapabilityRepository(tmp_path)
    original = repository.settings()["capabilities"]["enabled"]
    repository.update_settings({"enabled": not original})
    first = repository.policy_generation()
    repository.update_settings({"enabled": original})
    second = repository.policy_generation()

    assert second > first


def test_skill_lifecycle_is_shared_by_catalog_and_runtime(tmp_path: Path):
    store = SkillLifecycleStore(tmp_path / "skills.json")
    skills = [
        {"id": "skill/one", "enabled": True},
        {"id": "skill/two", "enabled": True},
    ]

    store.set_enabled("skill/two", False, skills)

    assert store.list(skills) == [
        {
            "id": "skill/one",
            "enabled": True,
            "source_path": "",
            "schema_version": "",
        },
        {
            "id": "skill/two",
            "enabled": False,
            "source_path": "",
            "schema_version": "",
        },
    ]
    assert [skill["id"] for skill in store.apply(skills)] == ["skill/one"]
