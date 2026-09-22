"""Focused Host Provider wiring coverage for AI support packs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    HOST_PROVIDER_FACTORY as PIPELINE_FACTORIES,
)
from ecosystem.rumi_ai_routing_pack.runtime.router import (
    HOST_PROVIDER_FACTORY as ROUTING_FACTORY,
)
from ecosystem.rumi_ai_stream_pack.runtime.normalizer import (
    HOST_PROVIDER_FACTORY as STREAM_FACTORY,
)
from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
    HOST_PROVIDER_FACTORY as TOOL_BRIDGE_FACTORY,
)
from ecosystem.rumi_ai_usage_pack.runtime.usage import (
    HOST_PROVIDER_FACTORY as USAGE_FACTORIES,
)
from ecosystem.rumi_model_registry_pack.runtime import process as registry_process


class _Invocation:
    """Record the contract allow-list acquired by a captured contribution."""

    def __init__(self) -> None:
        self.calls: list[tuple[frozenset[str], str]] = []

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
    ) -> object:
        """Return an opaque fake client with no ambient Host access."""

        self.calls.append((allowed_contract_ids, consumer_pack_id))
        return object()


def _binding(
    *,
    function_id: str,
    contract_id: str,
    operation_id: str,
) -> Any:
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id,
            implementation_digest=f"sha256:implementation-{function_id}",
        ),
        operation=SimpleNamespace(
            contract_id=contract_id,
            contract_version="1.0.0",
            operation_id=operation_id,
        ),
        principal_ref=SimpleNamespace(value=f"principal:{function_id}"),
        artifact=SimpleNamespace(digest=f"sha256:artifact-{function_id}"),
    )


def _capture(
    factory: Any,
    binding: Any,
    *,
    user_data_root: Path | None = None,
) -> Any:
    key = (
        binding.operation.contract_id,
        binding.operation.operation_id,
        binding.principal_ref.value,
    )
    return factory.capture(
        HostProviderCaptureContextV4(
            profile_id="defaults",
            plan_digest="sha256:plan",
            security_epoch=1,
            activation={"activation_id": "activation.ai-support"},
            state_root=Path("/tmp/ai-support-host-provider-state"),
            provider_bindings=(binding,),
            catalog_bindings=(),
            domain_ids={key: "domain.ai-support"},
            user_data_root=user_data_root,
        )
    )


@pytest.mark.parametrize(
    (
        "factory",
        "function_id",
        "contract_id",
        "operation_id",
        "payload",
        "consumer_pack_id",
    ),
    [
        (
            PIPELINE_FACTORIES["rumi_ai_pipeline_pack.ai-pipeline.prepare"],
            "rumi_ai_pipeline_pack.ai-pipeline.prepare",
            "tobkiri.service.ai.request.prepare.v1",
            "rumi_ai_pipeline_pack.ai-request-prepare.generate",
            {"request_id": "request.prepare", "decision_time": 1.0},
            "rumi_ai_pipeline_pack",
        ),
        (
            PIPELINE_FACTORIES["rumi_ai_pipeline_pack.ai-pipeline.failover"],
            "rumi_ai_pipeline_pack.ai-pipeline.failover",
            "tobkiri.service.ai.failover.decide.v1",
            "rumi_ai_pipeline_pack.ai-failover-decide.generate",
            {
                "allow_failover": True,
                "idempotency_key": "key",
                "tools": [],
                "error_code": "provider_unavailable",
                "attempt": 1,
                "candidate_count": 2,
                "decision_time": 1.0,
                "deadline": 2.0,
            },
            "rumi_ai_pipeline_pack",
        ),
        (
            ROUTING_FACTORY,
            "rumi_ai_routing_pack.ai-routing.default",
            "tobkiri.service.ai.route.v1",
            "rumi_ai_routing_pack.ai-route.generate",
            {
                "models": [],
                "execution_providers": [],
                "health": {},
                "requirements": {},
                "decision_time": 1.0,
            },
            "rumi_ai_routing_pack",
        ),
        (
            STREAM_FACTORY,
            "rumi_ai_stream_pack.ai-stream.normalize",
            "tobkiri.service.ai.stream.normalize.v1",
            "rumi_ai_stream_pack.ai-stream-normalize",
            {
                "request_id": "request.stream",
                "value": {"events": [{"type": "finish"}]},
            },
            "rumi_ai_stream_pack",
        ),
        (
            TOOL_BRIDGE_FACTORY,
            "rumi_ai_tool_bridge_pack.ai-tool-bridge.normalize",
            "tobkiri.service.ai.tool_intent.normalize.v1",
            "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate",
            {"request_id": "request.tools", "intents": []},
            "rumi_ai_tool_bridge_pack",
        ),
        (
            USAGE_FACTORIES["rumi_ai_usage_pack.ai-usage.cost"],
            "rumi_ai_usage_pack.ai-usage.cost",
            "tobkiri.service.ai.usage.cost.v1",
            "rumi_ai_usage_pack.ai-usage-cost.generate",
            {
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "pricing": {"input": 1, "output": 1},
            },
            "rumi_ai_usage_pack",
        ),
        (
            USAGE_FACTORIES["rumi_ai_usage_pack.ai-usage.tokenize"],
            "rumi_ai_usage_pack.ai-usage.tokenize",
            "tobkiri.service.ai.tokenize.v1",
            "rumi_ai_usage_pack.ai-tokenize",
            {"input": "hello"},
            "rumi_ai_usage_pack",
        ),
    ],
)
def test_ai_support_factories_use_only_an_empty_declared_contract_client(
    factory: Any,
    function_id: str,
    contract_id: str,
    operation_id: str,
    payload: Mapping[str, Any],
    consumer_pack_id: str,
) -> None:
    """Pure support functions cannot gain ambient Host contract access."""

    binding = _binding(
        function_id=function_id,
        contract_id=contract_id,
        operation_id=operation_id,
    )
    captured = _capture(factory, binding)
    invocation = _Invocation()

    result = captured.contributions[0].invoke(operation_id, payload, invocation)

    assert isinstance(result, Mapping)
    assert invocation.calls == [(frozenset(), consumer_pack_id)]


@pytest.mark.parametrize(
    "function_id, contract_id, operation_id, payload, expected_service_operation",
    [
        (
            "rumi_model_registry_pack.model-registry.profile",
            "tobkiri.resource.ai.model.profile.v1",
            "rumi_model_registry_pack.model-profile-resource.generate",
            {"identifier": "default", "profile_id": "defaults"},
            "rumi_model_registry_pack.model-profile-resource.generate",
        ),
        (
            "rumi_model_registry_pack.model-registry.manage",
            "tobkiri.action.ai.model.profile.manage.v1",
            "rumi_model_registry_pack.model-profile-manage",
            {"operation": "save", "profile_id": "defaults"},
            "save",
        ),
        (
            "rumi_model_registry_pack.model-registry.migrate",
            "tobkiri.action.ai.model.registry.migrate.v1",
            "rumi_model_registry_pack.model-registry-migrate",
            {"action": "migration.apply", "profile_id": "defaults"},
            "migration.apply",
        ),
    ],
)
def test_model_registry_factory_uses_bound_client_and_narrow_owner_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    function_id: str,
    contract_id: str,
    operation_id: str,
    payload: Mapping[str, Any],
    expected_service_operation: str,
) -> None:
    """Registry factory never exposes a raw service method through payload."""

    calls: list[tuple[Path | None, str, Mapping[str, Any]]] = []

    class _Service:
        def __init__(self, *, user_data_root: Path | None) -> None:
            self.user_data_root = user_data_root

        def invoke(
            self,
            operation: str,
            value: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            calls.append((self.user_data_root, operation, value))
            return {"operation": operation}

    monkeypatch.setattr(registry_process, "ModelRegistryService", _Service)
    binding = _binding(
        function_id=function_id,
        contract_id=contract_id,
        operation_id=operation_id,
    )
    captured = _capture(
        registry_process.HOST_PROVIDER_FACTORY[function_id],
        binding,
        user_data_root=tmp_path,
    )
    invocation = _Invocation()

    result = captured.contributions[0].invoke(operation_id, payload, invocation)

    assert result == {"operation": expected_service_operation}
    assert calls == [(tmp_path, expected_service_operation, payload)]
    assert invocation.calls == [(frozenset(), "rumi_model_registry_pack")]


def test_model_registry_manage_rejects_an_unrelated_service_method(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A manage capability cannot select read or migration service methods."""

    binding = _binding(
        function_id="rumi_model_registry_pack.model-registry.manage",
        contract_id="tobkiri.action.ai.model.profile.manage.v1",
        operation_id="rumi_model_registry_pack.model-profile-manage",
    )
    captured = _capture(
        registry_process.HOST_PROVIDER_FACTORY[
            "rumi_model_registry_pack.model-registry.manage"
        ],
        binding,
        user_data_root=tmp_path,
    )
    monkeypatch.setattr(
        registry_process,
        "ModelRegistryService",
        lambda **_kwargs: pytest.fail("service must not be reached"),
    )

    with pytest.raises(ValueError, match="payload is invalid"):
        captured.contributions[0].invoke(
            binding.operation.operation_id,
            {"operation": "list", "profile_id": "defaults"},
            _Invocation(),
        )


def test_factory_rejects_mixed_function_bindings() -> None:
    """A captured factory accepts only its exact manifest Function identity."""

    binding = _binding(
        function_id="rumi_ai_routing_pack.ai-routing.default",
        contract_id="tobkiri.service.ai.route.v1",
        operation_id="rumi_ai_routing_pack.ai-route.generate",
    )
    wrong = _binding(
        function_id="rumi_ai_usage_pack.ai-usage.cost",
        contract_id="tobkiri.service.ai.usage.cost.v1",
        operation_id="rumi_ai_usage_pack.ai-usage-cost.generate",
    )
    key = (
        binding.operation.contract_id,
        binding.operation.operation_id,
        binding.principal_ref.value,
    )
    context = HostProviderCaptureContextV4(
        profile_id="defaults",
        plan_digest="sha256:plan",
        security_epoch=1,
        activation={},
        state_root=Path("/tmp/ai-support-host-provider-state"),
        provider_bindings=(binding, wrong),
        catalog_bindings=(),
        domain_ids={key: "domain.ai-support"},
    )

    with pytest.raises(PermissionError, match="bindings are incomplete"):
        ROUTING_FACTORY.capture(context)
