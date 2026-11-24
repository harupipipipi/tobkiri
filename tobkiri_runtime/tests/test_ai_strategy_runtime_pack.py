"""Contract tests for generic, Pack-provided AI strategy dispatch."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.global_contract_dispatch import GlobalContractUnavailable
from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from core_runtime.bootstrap.production_v4 import (
    _resolve_packvm_bridge_target,
    _validate_packvm_bridge_continuation,
)
from core_runtime.authority.v4 import AuthorityDenied
from tobkiri_protocol.canonical import canonical_digest
from ecosystem.rumi_ai_strategy_runtime_pack.runtime.strategy import (
    CATALOG_OPERATION_ID,
    DISPATCH_CONTRACT_ID,
    DISPATCH_OPERATION_ID,
    EXECUTE_CONTRACT_ID,
    HOST_PROVIDER_FACTORY,
    create_catalog_operation,
    create_dispatch_operation,
)


def _request() -> dict[str, Any]:
    return {
        "request_id": "turn-1",
        "model_reference": "main",
        "messages": [{"role": "user", "content": "hello"}],
        "parameters": {},
        "tools": [],
        "requirements": {},
        "deadline": 2_000_000_000_000,
        "idempotency_key": "turn-1:strategy",
        "maximum_cost_microusd": 50_000,
    }


class _Client:
    def __init__(self, providers: tuple[Mapping[str, Any], ...]) -> None:
        self._providers = providers
        self.calls: list[tuple[str, str, Mapping[str, Any], str]] = []

    def providers(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        assert contract_id == EXECUTE_CONTRACT_ID
        return self._providers

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        provider_instance_id: str,
    ) -> Mapping[str, Any]:
        self.calls.append(
            (contract_id, operation_id, payload, provider_instance_id)
        )
        return {"text": "strategy result", "usage": {"output_tokens": 2}}


def test_dispatches_new_strategy_provider_without_gateway_changes() -> None:
    """Any selected Pack provider is invoked from captured metadata alone."""

    client = _Client(
        (
            {
                "provider_instance_id": "third-party.review-loop",
                "provider_id": "third_party_pack.strategy.execute",
                "operation_id": "third_party_pack.strategy.execute",
            },
        )
    )

    result = create_dispatch_operation(client)(
        DISPATCH_OPERATION_ID,
        {
            "strategy_reference": "third_party_pack.strategy.execute",
            "request": _request(),
        },
    )

    assert result == {
        "text": "strategy result",
        "usage": {"output_tokens": 2},
    }
    assert client.calls == [
        (
            EXECUTE_CONTRACT_ID,
            "third_party_pack.strategy.execute",
            _request(),
            "third-party.review-loop",
        )
    ]


def test_dispatcher_seals_default_cost_before_packvm_outer_capture() -> None:
    """An omitted caller budget becomes an immutable conservative ceiling."""

    provider = {
        "provider_instance_id": "third-party.review-loop",
        "provider_id": "third_party_pack.strategy.execute",
        "operation_id": "third_party_pack.strategy.execute",
    }
    client = _Client((provider,))
    request = _request()
    del request["maximum_cost_microusd"]

    create_dispatch_operation(client)(
        DISPATCH_OPERATION_ID,
        {
            "strategy_reference": "third_party_pack.strategy.execute",
            "request": request,
        },
    )

    assert client.calls[0][2]["maximum_cost_microusd"] == 50_000

    request = _request()
    request["maximum_cost_microusd"] = 10_000_000
    create_dispatch_operation(client)(
        DISPATCH_OPERATION_ID,
        {
            "strategy_reference": "third_party_pack.strategy.execute",
            "request": request,
        },
    )
    assert client.calls[1][2]["maximum_cost_microusd"] == 1_000_000


@pytest.mark.parametrize(
    "providers",
    [
        (),
        (
            {
                "provider_instance_id": "other",
                "provider_id": "other_pack.strategy.execute",
                "operation_id": "other_pack.strategy.execute",
            },
        ),
    ],
)
def test_selected_disabled_or_missing_strategy_fails_closed(
    providers: tuple[Mapping[str, Any], ...],
) -> None:
    """A selected strategy never falls back to direct model generation."""

    client = _Client(providers)
    with pytest.raises(GlobalContractUnavailable, match="unavailable"):
        create_dispatch_operation(client)(
            DISPATCH_OPERATION_ID,
            {
                "strategy_reference": "selected_pack.strategy.execute",
                "request": _request(),
            },
        )
    assert client.calls == []


def test_backend_unavailable_strategy_fails_before_invocation() -> None:
    """Captured metadata cannot route to an unavailable provider backend."""

    client = _Client(
        (
            {
                "provider_instance_id": "selected",
                "provider_id": "selected_pack.strategy.execute",
                "operation_id": "third_party_pack.strategy.execute",
                "backend_unavailable_reason": "disabled",
            },
        )
    )
    with pytest.raises(GlobalContractUnavailable, match="backend"):
        create_dispatch_operation(client)(
            DISPATCH_OPERATION_ID,
            {
                "strategy_reference": "selected_pack.strategy.execute",
                "request": _request(),
            },
        )
    assert client.calls == []


def test_catalog_projects_only_host_admitted_available_strategies() -> None:
    """UI availability comes from captured Host metadata, not Pack flags."""

    available = {
        "provider_instance_id": "selected",
        "provider_id": "third_party_pack.strategy.execute",
        "pack_id": "third_party_pack",
        "operation_id": "third_party_pack.strategy.execute",
        "artifact_digest": "sha256:artifact",
        "implementation_digest": "sha256:implementation",
        "pack_display_name": "Third Party Review",
        "pack_description": "Reviews a response with an installed strategy.",
    }
    client = _Client(
        (
            available,
            {
                **available,
                "provider_instance_id": "disabled",
                "backend_unavailable_reason": "disabled",
            },
        )
    )

    result = create_catalog_operation(client)(CATALOG_OPERATION_ID, {})

    assert result == {
        "strategies": [
            {
                "provider_instance_id": "selected",
                "provider_id": "third_party_pack.strategy.execute",
                "pack_id": "third_party_pack",
                "operation_id": "third_party_pack.strategy.execute",
                "artifact_digest": "sha256:artifact",
                "implementation_digest": "sha256:implementation",
                "strategy_reference": "third_party_pack.strategy.execute",
                "label": "Third Party Review",
                "description": (
                    "Reviews a response with an installed strategy."
                ),
            }
        ],
        "count": 1,
    }


def test_dispatch_rejects_authority_smuggling_and_incomplete_fences() -> None:
    """The generic envelope accepts no approval flags or missing replay fences."""

    client = _Client(())
    payload: dict[str, Any] = {
        "strategy_reference": "selected_pack.strategy.execute",
        "request": {**_request(), "approved": True},
    }
    with pytest.raises(ValueError, match="fields"):
        create_dispatch_operation(client)(DISPATCH_OPERATION_ID, payload)

    del payload["request"]["approved"]
    del payload["request"]["idempotency_key"]
    with pytest.raises(ValueError, match="idempotency"):
        create_dispatch_operation(client)(DISPATCH_OPERATION_ID, payload)

    payload["request"]["idempotency_key"] = "turn-1:strategy"
    payload["request"]["maximum_cost_microusd"] = 0.05
    with pytest.raises(ValueError, match="micro-USD"):
        create_dispatch_operation(client)(DISPATCH_OPERATION_ID, payload)


def test_packvm_strategy_bridge_resolves_contract_from_captured_plan() -> None:
    """A sandbox strategy selects a generic contract, never a concrete Pack."""

    def edge(contract_id: str, operation_id: str):
        return SimpleNamespace(
            resolved_binding=SimpleNamespace(
                operation=SimpleNamespace(
                    contract_id=contract_id,
                    operation_id=operation_id,
                )
            )
        )

    quote = edge(
        "tobkiri.resource.ai.route.quote.v1",
        "some_gateway.route.quote",
    )
    selected, target = _resolve_packvm_bridge_target(
        (quote,),
        {"contract_id": "tobkiri.resource.ai.route.quote.v1"},
    )
    assert selected is quote
    assert target == {"contract_id": "tobkiri.resource.ai.route.quote.v1"}

    duplicate = edge(
        "tobkiri.resource.ai.route.quote.v1",
        "other_gateway.route.quote",
    )
    with pytest.raises(AuthorityDenied, match="ambiguous"):
        _resolve_packvm_bridge_target(
            (quote, duplicate),
            {"contract_id": "tobkiri.resource.ai.route.quote.v1"},
        )


def test_host_validates_extended_packvm_continuation_binding() -> None:
    """Multi-hop state stays bound to its outer owner, target, and digest."""

    target = {"contract_id": "tobkiri.resource.ai.route.quote.v1"}
    request_digest = "sha256:" + "1" * 64
    state = {"phase": "quote", "iteration": 1}
    continuation = {
        "kind": "tobkiri.packvm.continuation.v1",
        "protocol": "io.tobkiri.packvm.bridge.v1",
        "version": 1,
        "operation_id": "review.execute",
        "nonce": "a" * 48,
        "target": target,
        "request_digest": request_digest,
        "hop": 1,
        "max_hops": 3,
        "previous_result_digest": "sha256:" + "2" * 64,
        "state": state,
        "state_digest": canonical_digest(state),
    }

    assert _validate_packvm_bridge_continuation(
        continuation,
        operation_id="review.execute",
        target=target,
        request_digest=request_digest,
    ) == continuation

    for field, replacement in (
        ("operation_id", "other.execute"),
        ("target", {"contract_id": "other.contract"}),
        ("request_digest", "sha256:" + "3" * 64),
        ("nonce", "z" * 48),
        ("hop", 3),
        ("max_hops", 17),
        ("previous_result_digest", "invalid"),
        ("state_digest", "sha256:" + "4" * 64),
    ):
        tampered = {**continuation, field: replacement}
        with pytest.raises(AuthorityDenied, match="continuation"):
            _validate_packvm_bridge_continuation(
                tampered,
                operation_id="review.execute",
                target=target,
                request_digest=request_digest,
            )

class _Invocation:
    def __init__(self, client: _Client) -> None:
        self.client = client
        self.calls: list[tuple[frozenset[str], str, bool]] = []
        self.current_checks = 0

    def assert_current(self) -> None:
        self.current_checks += 1

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
        include_credentials: bool = True,
    ) -> _Client:
        self.calls.append(
            (allowed_contract_ids, consumer_pack_id, include_credentials)
        )
        return self.client


def test_host_factory_exposes_only_plan_pinned_strategy_contract() -> None:
    """The Host factory grants neither credentials nor ambient contracts."""

    function = SimpleNamespace(
        function_id=DISPATCH_OPERATION_ID,
        implementation_digest="sha256:implementation",
    )
    operation = SimpleNamespace(
        contract_id=DISPATCH_CONTRACT_ID,
        contract_version="1.0.0",
        operation_id=DISPATCH_OPERATION_ID,
    )
    binding = SimpleNamespace(
        function=function,
        operation=operation,
        principal_ref=SimpleNamespace(value="principal:strategy-dispatch"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    key = (
        DISPATCH_CONTRACT_ID,
        DISPATCH_OPERATION_ID,
        "principal:strategy-dispatch",
    )
    captured = HOST_PROVIDER_FACTORY[DISPATCH_OPERATION_ID].capture(
        HostProviderCaptureContextV4(
            profile_id="defaults",
            plan_digest="sha256:plan",
            security_epoch=1,
            activation={"activation_id": "activation.strategy"},
            state_root=Path("/tmp/strategy-runtime-test"),
            provider_bindings=(binding,),
            catalog_bindings=(),
            domain_ids={key: "domain.strategy"},
        )
    )
    client = _Client(
        (
            {
                "provider_instance_id": "selected",
                "provider_id": "selected_pack.strategy.execute",
                "operation_id": "third_party_pack.strategy.execute",
            },
        )
    )
    invocation = _Invocation(client)

    result = captured.contributions[0].invoke(
        DISPATCH_OPERATION_ID,
        {
            "strategy_reference": "selected_pack.strategy.execute",
            "request": _request(),
        },
        invocation,
    )

    assert result["text"] == "strategy result"
    assert invocation.calls == [
        (
            frozenset({EXECUTE_CONTRACT_ID}),
            "rumi_ai_strategy_runtime_pack",
            False,
        )
    ]
    assert invocation.current_checks == 2
