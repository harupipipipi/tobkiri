"""Isolated public wire execution tests; no packaged Profile is claimed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ecosystem.rumi_job_action_broker_pack.runtime.broker import JobActionBroker
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from ecosystem.rumi_model_registry_pack.runtime.service import ModelRegistryService
from ecosystem.rumi_schedule_store_pack.runtime.store import (
    ScheduleConflict,
    ScheduleStore,
    _arguments,
)
from ecosystem.rumi_scheduler_runtime_pack.runtime.scheduler import SchedulerRuntime
from tobkiri_protocol.secondary_model_policy_v1 import (
    ModelPolicyResolutionError,
    resolve_secondary_model_policy,
)


def _profile(identifier: str, **metadata: Any) -> dict[str, Any]:
    return {
        "model_profile_id": identifier,
        "model_id": identifier,
        "enabled": True,
        "metadata": {
            "supports_thinking": True,
            "thinking_levels": ["none", "high"],
            "capabilities": ["tool_calling"],
            **metadata,
        },
    }


def _resolve(payload: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any]:
    return resolve_secondary_model_policy(payload, profiles=profiles, store_revision=3)


def test_inherit_re_resolves_and_snapshot_preserves_model_and_thinking() -> None:
    profiles = [_profile("a"), _profile("b")]
    payload = {
        "context": {"conversation_model_profile_id": "a", "conversation_thinking_level": "high"}
    }
    first = _resolve(payload, profiles)
    assert first["resolved_profile_id"] == "a"
    payload["context"]["conversation_model_profile_id"] = "b"
    assert _resolve(payload, profiles)["resolved_profile_id"] == "b"
    payload["model_policy"] = {"mode": "snapshot"}
    snapshot = _resolve(payload, profiles)
    payload["snapshot_receipt"] = snapshot
    payload["context"] = {
        "conversation_model_profile_id": "a",
        "conversation_thinking_level": "none",
    }
    later = _resolve(payload, profiles)
    assert later["resolved_profile_id"] == "b"
    assert later["thinking_level"] == "high"
    assert later["snapshot_captured"] is False


def test_fixed_policy_records_request_and_does_not_follow_config_change() -> None:
    payload = {
        "model_policy": {"mode": "fixed", "profile_id": "a"},
        "context": {"conversation_model_profile_id": "b"},
    }
    result = _resolve(payload, [_profile("a"), _profile("b")])
    assert result["requested_model_policy"]["profile_id"] == "a"
    assert result["resolved_profile_id"] == "a"
    assert result["store_revision"] == 3


@pytest.mark.parametrize(
    "profile,required,level,code",
    [
        (dict(_profile("a"), enabled=False), [], "none", "MODEL_PROFILE_UNAVAILABLE"),
        (_profile("a", requires_api_key=True), [], "none", "MODEL_API_KEY_MISSING"),
        (
            _profile("a", capabilities=[]),
            ["model.tool_calling"],
            "none",
            "MODEL_CAPABILITY_UNSATISFIED",
        ),
        (_profile("a"), [], "xhigh", "MODEL_THINKING_LEVEL_UNSUPPORTED"),
    ],
)
def test_unavailable_or_unsupported_policy_fails_with_auditable_request(
    profile: dict[str, Any],
    required: list[str],
    level: str,
    code: str,
) -> None:
    payload = {
        "model_policy": {"mode": "fixed", "profile_id": "a", "required_capabilities": required},
        "thinking_policy": {"mode": "fixed", "level": level},
    }
    with pytest.raises(ModelPolicyResolutionError) as failure:
        _resolve(payload, [profile])
    assert failure.value.code == code
    assert failure.value.receipt["requested_model_policy"]["profile_id"] == "a"
    assert failure.value.receipt["error"] == {"code": code}


def test_only_explicit_available_fallback_is_used() -> None:
    payload = {
        "model_policy": {
            "mode": "fixed",
            "profile_id": "missing",
            "fallback_profile_id": "a",
            "on_unavailable": "fallback",
        }
    }
    result = _resolve(payload, [_profile("a")])
    assert result["resolved_profile_id"] == "a"
    assert result["fallback_reason"] == "MODEL_PROFILE_UNKNOWN"
    assert result["resolution_source"] == "declared_fallback"


def test_model_owner_service_uses_saved_catalog_not_consumer_profiles(tmp_path: Path) -> None:
    registry = ModelRegistry("default", user_data_root=tmp_path)
    registry.save(_profile("a"), expected_revision=0)
    result = ModelRegistryService(user_data_root=tmp_path).invoke(
        "policy.resolve",
        {"profile_id": "default", "model_policy": {"mode": "fixed", "profile_id": "a"}},
    )
    assert result["resolved_profile_id"] == "a"
    assert result["store_revision"] == 1


class _WireClient:
    def __init__(self, root: Path, clock: list[int]) -> None:
        self.store = ScheduleStore("default", root=root, clock=lambda: clock[0])
        self.broker = JobActionBroker(self, "default", root=root, canonical=True)
        self.dispatch_count = 0
        self.result_status = "completed"
        self.fail_once = False

    def providers(self, contract: str) -> tuple[dict[str, str], ...]:
        assert contract == "tobkiri.action.job.adapter.v2"
        return ({"provider_id": "test.adapter", "operation_id": "test.job-adapter"},)

    def invoke(self, contract: str, operation: str, payload: dict[str, Any]) -> Any:
        name = payload["operation"]
        if contract == "tobkiri.resource.schedule.v1":
            assert operation == "rumi_schedule_store_pack.schedule-resource"
            if name == "list":
                return self.store.snapshot()
            if name == "get":
                return {"schedule": self.store.get(payload["schedule_id"])}
            return self.store.due(
                payload["now_ms"], payload["limit"], payload.get("schedule_id", "")
            )
        if contract == "tobkiri.action.schedule.v1":
            assert operation == "rumi_schedule_store_pack.schedule-action"
            return self.store.apply(name, _arguments(name, payload))
        if contract == "tobkiri.action.job.v1":
            assert operation == "rumi_job_action_broker_pack.job-action-broker"
            return self.broker.invoke(name, payload)
        assert contract == "tobkiri.action.job.adapter.v2"
        assert operation == "test.job-adapter"
        if name == "describe":
            return {"action_ids": ["test.action"]}
        if name == "dispatch":
            self.dispatch_count += 1
            if self.fail_once:
                self.fail_once = False
                # A confirmed owner failure may retry. An exception with an
                # unknown effect outcome must retain its original lease.
                return {"status": "failed"}
        return {"status": self.result_status}


def _create(client: _WireClient, **changes: Any) -> None:
    client.store.apply(
        "create",
        _arguments(
            "create",
            {
                "schedule_id": "task",
                "expected_revision": 0,
                "name": "Test action",
                "action_id": "test.action",
                "payload": {"prompt": "Do work"},
                "next_run_at_ms": 1000,
                "interval_ms": 0,
                "max_attempts": 2,
                **changes,
            },
        ),
    )


def test_public_scheduler_dispatches_after_clock_boundary_and_retries(tmp_path: Path) -> None:
    clock = [999]
    client = _WireClient(tmp_path, clock)
    _create(client)
    runtime = SchedulerRuntime(client, "default", clock=lambda: clock[0], canonical=True)
    assert runtime.control("tick", {})["count"] == 0
    clock[0] = 1000
    client.fail_once = True
    failed = runtime.control("tick", {})
    assert failed["count"] == 1
    record = client.store.get("task")
    assert record["status"] == "scheduled"
    assert record["next_run_at_ms"] == 2000
    assert "private secret" not in record["last_error"]
    clock[0] = 1999
    assert runtime.control("tick", {})["count"] == 0
    clock[0] = 2000
    assert runtime.control("tick", {})["count"] == 1
    assert client.store.get("task")["status"] == "completed"
    assert client.dispatch_count == 2


def test_pause_resume_edit_and_stale_save_are_real_store_transitions(tmp_path: Path) -> None:
    clock = [1000]
    client = _WireClient(tmp_path, clock)
    _create(client)
    client.store.apply("pause", {"schedule_id": "task", "expected_revision": 1})
    assert client.store.due(1000, 20)["schedules"] == []
    client.store.apply(
        "update", {"schedule_id": "task", "expected_revision": 2, "updates": {"name": "Edited"}}
    )
    with pytest.raises(ScheduleConflict):
        client.store.apply("resume", {"schedule_id": "task", "expected_revision": 2})
    client.store.apply("resume", {"schedule_id": "task", "expected_revision": 3})
    assert client.store.due(1000, 20)["schedules"][0]["name"] == "Edited"


def test_accepted_job_survives_restart_without_false_completion_or_duplicate(
    tmp_path: Path,
) -> None:
    clock = [1000]
    client = _WireClient(tmp_path, clock)
    _create(client)
    client.result_status = "accepted"
    runtime = SchedulerRuntime(client, "default", clock=lambda: clock[0], canonical=True)
    runtime.control("tick", {})
    assert client.store.get("task")["status"] == "running"
    clock[0] += 300001
    restarted_client = _WireClient(tmp_path, clock)
    restarted_client.result_status = "accepted"
    restarted = SchedulerRuntime(
        restarted_client, "default", clock=lambda: clock[0], canonical=True
    )
    assert restarted.control("tick", {})["count"] == 0
    assert restarted_client.dispatch_count == 0
    restarted_client.result_status = "completed"
    assert restarted.control("tick", {})["count"] == 1
    assert restarted_client.store.get("task")["status"] == "completed"


def test_claim_and_running_editor_guard_bind_the_exact_lease(tmp_path: Path) -> None:
    clock = [1000]
    client = _WireClient(tmp_path, clock)
    _create(client)
    client.store.apply(
        "claim",
        {
            "schedule_id": "task",
            "expected_revision": 1,
            "lease_id": "one",
            "lease_expires_at_ms": 2000,
        },
    )
    with pytest.raises(ScheduleConflict):
        client.store.apply(
            "update", {"schedule_id": "task", "expected_revision": 2, "updates": {"name": "bad"}}
        )
    with pytest.raises(ScheduleConflict):
        client.store.apply(
            "complete",
            {"schedule_id": "task", "expected_revision": 2, "lease_id": "wrong", "error": ""},
        )
    assert client.store.get("task")["status"] == "running"
