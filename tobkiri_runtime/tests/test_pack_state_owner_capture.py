"""Scope and admission tests for owner factories, independent of packaging."""

from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_company_state_store_pack.runtime.store import (
    HOST_PROVIDER_FACTORY as TEAM_FACTORIES,
)
from ecosystem.rumi_schedule_store_pack.runtime.store import (
    HOST_PROVIDER_FACTORY as SCHEDULE_FACTORIES,
)
from ecosystem.rumi_scheduler_runtime_pack.runtime.scheduler import (
    HOST_PROVIDER_FACTORY as RUNTIME_FACTORIES,
)
from ecosystem.rumi_job_action_broker_pack.runtime.broker import (
    HOST_PROVIDER_FACTORY as JOB_FACTORIES,
)
from ecosystem.rumi_model_registry_pack.runtime.process import (
    HOST_PROVIDER_FACTORY as MODEL_FACTORIES,
)
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from tobkiri_protocol.secondary_model_policy_v1 import ModelPolicyResolutionError


class _Invocation:
    def __init__(self, *, stale: bool = False, providers: list[dict[str, Any]] | None = None):
        self.stale = stale
        self.providers = providers or []
        self.client_scopes: list[tuple[frozenset[str], str, bool]] = []

    def assert_current(self) -> None:
        if self.stale:
            raise PermissionError("activation changed")

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
        include_credentials: bool = True,
    ) -> Any:
        self.client_scopes.append((allowed_contract_ids, consumer_pack_id, include_credentials))
        return self

    def invoke(self, contract: str, operation: str, payload: dict[str, Any]) -> Any:
        assert contract == "tobkiri.resource.ai.provider.registry.v1"
        assert operation == "rumi_provider_registry_pack.provider-registry-resource"
        assert payload == {"operation": "list"}
        return {"revision": 2, "providers": self.providers}


def _capture(factory: Any, function: str, contract: str, operation: str, root: Path) -> Any:
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function, implementation_digest="sha256:impl"),
        operation=SimpleNamespace(
            contract_id=contract, operation_id=operation, contract_version="1.0.0"
        ),
        principal_ref=SimpleNamespace(value=f"principal:{function}"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    key = (contract, operation, binding.principal_ref.value)
    return factory.capture(
        HostProviderCaptureContextV4(
            profile_id="default",
            plan_digest="sha256:plan",
            security_epoch=1,
            activation={"activation_id": "test"},
            state_root=root / "host",
            provider_bindings=(binding,),
            catalog_bindings=(),
            domain_ids={key: "test.domain"},
            user_data_root=root,
        )
    ).contributions[0]


@pytest.mark.parametrize(
    "factories,function,contract,operation",
    [
        (
            SCHEDULE_FACTORIES,
            "rumi_schedule_store_pack.schedule-store.action",
            "tobkiri.action.schedule.v1",
            "rumi_schedule_store_pack.schedule-action",
        ),
        (
            RUNTIME_FACTORIES,
            "rumi_scheduler_runtime_pack.scheduler.control",
            "tobkiri.action.scheduler.v1",
            "rumi_scheduler_runtime_pack.scheduler-control",
        ),
        (
            JOB_FACTORIES,
            "rumi_job_action_broker_pack.job-action.broker",
            "tobkiri.action.job.v1",
            "rumi_job_action_broker_pack.job-action-broker",
        ),
        (
            TEAM_FACTORIES,
            "rumi_company_state_store_pack.company-state.action",
            "tobkiri.action.company.state.v1",
            "rumi_company_state_store_pack.company-state-action",
        ),
    ],
)
def test_owner_factory_rejects_wrong_profile_and_stale_activation(
    tmp_path: Path,
    factories: dict[str, Any],
    function: str,
    contract: str,
    operation: str,
) -> None:
    contribution = _capture(factories[function], function, contract, operation, tmp_path)
    with pytest.raises(PermissionError):
        contribution.invoke(operation, {"profile_id": "other"}, _Invocation())
    with pytest.raises(PermissionError):
        contribution.invoke(operation, {}, _Invocation(stale=True))
    assert not (tmp_path / "packs").exists()


def test_schedule_factory_rejects_unbound_operation_and_client_approval(
    tmp_path: Path,
) -> None:
    function = "rumi_schedule_store_pack.schedule-store.action"
    operation = "rumi_schedule_store_pack.schedule-action"
    contribution = _capture(
        SCHEDULE_FACTORIES[function], function, "tobkiri.action.schedule.v1", operation, tmp_path
    )
    payload = {
        "operation": "create",
        "schedule_id": "test",
        "expected_revision": 0,
        "name": "Test",
        "action_id": "test.action",
        "payload": {},
        "next_run_at_ms": 0,
        "interval_ms": 0,
        "max_attempts": 2,
    }
    with pytest.raises(PermissionError):
        contribution.invoke("other.operation", payload, _Invocation())
    with pytest.raises(PermissionError):
        contribution.invoke(operation, {**payload, "approved": True}, _Invocation())
    result = contribution.invoke(operation, payload, _Invocation())
    assert result["revision"] == 1
    assert result["schedule"]["id"] == "test"


def test_model_policy_owner_uses_current_provider_connection_without_secret_values(
    tmp_path: Path,
) -> None:
    ModelRegistry("default", user_data_root=tmp_path).save(
        {
            "model_profile_id": "model",
            "model_id": "native",
            "metadata": {
                "provider_connection_id": "connection",
                "requires_credentials": True,
                "supports_tool_calling": True,
            },
        },
        expected_revision=0,
    )
    function = "rumi_model_registry_pack.model-registry.profile"
    operation = "rumi_model_registry_pack.model-profile-resource"
    contribution = _capture(
        MODEL_FACTORIES[function],
        function,
        "tobkiri.resource.ai.model.profile.v1",
        operation,
        tmp_path,
    )
    payload = {
        "operation": "policy.resolve",
        "model_policy": {"mode": "fixed", "profile_id": "model"},
    }
    missing = _Invocation(providers=[{"provider_instance_id": "connection", "enabled": True}])
    with pytest.raises(ModelPolicyResolutionError) as failure:
        contribution.invoke(operation, payload, missing)
    assert failure.value.code == "MODEL_API_KEY_MISSING"
    current = _Invocation(
        providers=[
            {
                "provider_instance_id": "connection",
                "enabled": True,
                "credential_handle": "opaque:current",
            }
        ]
    )
    result = contribution.invoke(operation, payload, current)
    assert result["resolved_profile_id"] == "model"
    assert "opaque:current" not in str(result)
    assert current.client_scopes == [
        (
            frozenset({"tobkiri.resource.ai.provider.registry.v1"}),
            "rumi_model_registry_pack",
            False,
        )
    ]


def test_team_public_action_uses_team_cas_after_another_team_update(tmp_path: Path) -> None:
    function = "rumi_company_state_store_pack.company-state.action"
    operation = "rumi_company_state_store_pack.company-state-action"
    contribution = _capture(
        TEAM_FACTORIES[function], function, "tobkiri.action.company.state.v1", operation, tmp_path
    )
    for team in ("a", "b"):
        contribution.invoke(
            operation,
            {
                "operation": "company.create",
                "company_id": team,
                "expected_revision": 0,
                "name": team,
            },
            _Invocation(),
        )
    first = contribution.invoke(
        operation,
        {
            "operation": "message.append",
            "company_id": "a",
            "expected_revision": 1,
            "record": {"id": "message-a", "text": "one"},
        },
        _Invocation(),
    )
    second = contribution.invoke(
        operation,
        {
            "operation": "message.append",
            "company_id": "b",
            "expected_revision": 1,
            "record": {"id": "message-b", "text": "two"},
        },
        _Invocation(),
    )
    assert first["revision"] == second["revision"] == 2


@pytest.mark.parametrize("suffix", ["", ".generate", ".stream"])
def test_model_policy_adoption_preserves_gateway_identifier_wire(
    tmp_path: Path, suffix: str
) -> None:
    adoption = json.loads(
        (Path(__file__).parents[1] / "docs/pack-state-scheduler-adoption.v1.json").read_text(
            encoding="utf-8"
        )
    )
    schema = adoption["semantic_operations"]["rumi_model_registry_pack"][0]["schemas"]["input"]
    validator = Draft202012Validator(schema)
    validator.validate({"identifier": "model"})
    validator.validate({"operation": "policy.resolve"})
    assert list(validator.iter_errors({}))
    assert list(validator.iter_errors({"identifier": "model", "approved": True}))
    ModelRegistry("default", user_data_root=tmp_path).save(
        {"model_profile_id": "model", "model_id": "native"}, expected_revision=0
    )
    function = "rumi_model_registry_pack.model-registry.profile"
    operation = f"rumi_model_registry_pack.model-profile-resource{suffix}"
    contribution = _capture(
        MODEL_FACTORIES[function],
        function,
        "tobkiri.resource.ai.model.profile.v1",
        operation,
        tmp_path,
    )
    result = contribution.invoke(operation, {"identifier": "model"}, _Invocation())
    assert result["resolved_profile_id"] == "model"
