"""Security boundaries for Host-owned aggregate PackVM bridge budgets."""

from __future__ import annotations

from types import SimpleNamespace
import time
from typing import Any

import pytest

from tobkiri_host.packvm_bridge_budget import (
    AI_GENERATE_CONTRACT,
    MAX_GENERATION_TOKENS,
    ROUTE_QUOTE_CONTRACT,
    STRATEGY_EXECUTE_CONTRACT,
    PackVMBridgeBudgetError,
    PackVMBridgeBudgetRegistry,
    _estimated_cost_microusd,
)


def _outer(maximum: int = 1_000) -> SimpleNamespace:
    return SimpleNamespace(
        contract_id=STRATEGY_EXECUTE_CONTRACT,
        context=SimpleNamespace(
            activation_digest="sha256:" + "a" * 64,
            request_id="outer-request",
        ),
        request_digest="sha256:" + "b" * 64,
        deadline_monotonic=time.monotonic() + 60,
        payload={"maximum_cost_microusd": maximum},
    )


def _binding(
    *,
    unit: str = "usd_per_token",
    input_rate: Any = "0",
    output_rate: Any = "0.000001",
) -> dict[str, Any]:
    return {
        "model_id": "provider/model",
        "provider_instance_id": "provider-instance",
        "catalog_provider_instance_id": "catalog-instance",
        "catalog_revision": "catalog-v1",
        "pricing_revision": "pricing-v1",
        "pricing": {
            "input": input_rate,
            "output": output_rate,
            "currency": "USD",
            "unit": unit,
        },
    }


def _quote_result(binding: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = binding or _binding()
    return {
        "status": "ok",
        "value": {
            "ready": True,
            **{
                key: selected[key]
                for key in (
                    "model_id",
                    "provider_instance_id",
                    "catalog_provider_instance_id",
                    "catalog_revision",
                    "pricing_revision",
                )
            },
            "pricing": dict(selected["pricing"]),
            "route_binding": selected,
        },
    }


def _generate_request(
    binding: dict[str, Any] | None = None,
    *,
    max_tokens: Any = 100,
) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
        "parameters": {"max_tokens": max_tokens},
        "route_binding": binding or _binding(),
    }


def _generate_result(
    binding: dict[str, Any] | None = None,
    *,
    cost: Any = "0.0001",
    known: Any = True,
) -> dict[str, Any]:
    selected = binding or _binding()
    return {
        "status": "ok",
        "value": {
            **{
                key: selected[key]
                for key in (
                    "model_id",
                    "provider_instance_id",
                    "catalog_provider_instance_id",
                    "catalog_revision",
                    "pricing_revision",
                )
            },
            "usage_cost": {"known": known, "cost": cost, "currency": "USD"},
        },
    }


def _record_quote(
    registry: PackVMBridgeBudgetRegistry,
    outer: SimpleNamespace,
    binding: dict[str, Any] | None = None,
) -> None:
    admission = registry.admit(outer, ROUTE_QUOTE_CONTRACT, {"messages": []})
    registry.record(admission, _quote_result(binding))


def test_host_reserves_and_records_one_quoted_generation() -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    _record_quote(registry, outer)
    admission = registry.admit(
        outer, AI_GENERATE_CONTRACT, _generate_request()
    )
    assert admission is not None
    assert admission.reserved_microusd == 100
    registry.record(admission, _generate_result())


def test_quote_is_one_use_and_consumed_before_provider_result() -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    _record_quote(registry, outer)
    registry.admit(outer, AI_GENERATE_CONTRACT, _generate_request())
    with pytest.raises(PackVMBridgeBudgetError, match="unused Host quote"):
        registry.admit(outer, AI_GENERATE_CONTRACT, _generate_request())


def test_malicious_pack_claim_cannot_override_immutable_outer_budget() -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer(50)
    _record_quote(registry, outer)
    request = _generate_request(max_tokens=100)
    request["claimed_maximum_cost_microusd"] = 10**12
    with pytest.raises(PackVMBridgeBudgetError, match="aggregate cost"):
        registry.admit(outer, AI_GENERATE_CONTRACT, request)


def test_arbitrary_pack_cannot_hide_billable_input_outside_messages() -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer(500)
    binding = _binding(input_rate="0.000001", output_rate="0")
    _record_quote(registry, outer, binding)
    request = _generate_request(binding, max_tokens=1)
    request["input"] = "x" * 1_000
    with pytest.raises(PackVMBridgeBudgetError, match="aggregate cost"):
        registry.admit(outer, AI_GENERATE_CONTRACT, request)


def test_generate_before_quote_and_route_revision_change_fail_closed() -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    with pytest.raises(PackVMBridgeBudgetError, match="unused Host quote"):
        registry.admit(outer, AI_GENERATE_CONTRACT, _generate_request())

    _record_quote(registry, outer)
    changed = _binding()
    changed["pricing_revision"] = "pricing-v2"
    with pytest.raises(PackVMBridgeBudgetError, match="unused Host quote"):
        registry.admit(outer, AI_GENERATE_CONTRACT, _generate_request(changed))


@pytest.mark.parametrize(
    "pricing",
    [
        {"input": "0", "output": "1", "currency": "USD"},
        {"input": "0", "output": "1", "currency": "USD", "unit": "tokens"},
        {"input": 0.0, "output": "1", "currency": "USD", "unit": "usd_per_token"},
    ],
)
def test_missing_unknown_or_float_quote_pricing_fails_closed(
    pricing: dict[str, Any],
) -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    binding = _binding()
    binding["pricing"] = pricing
    admission = registry.admit(outer, ROUTE_QUOTE_CONTRACT, {"messages": []})
    with pytest.raises(PackVMBridgeBudgetError, match="pricing"):
        registry.record(admission, _quote_result(binding))


@pytest.mark.parametrize("max_tokens", [None, True, 0, MAX_GENERATION_TOKENS + 1])
def test_missing_or_oversized_max_tokens_fails_before_generation(
    max_tokens: Any,
) -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    _record_quote(registry, outer)
    with pytest.raises(PackVMBridgeBudgetError, match="max_tokens"):
        registry.admit(
            outer,
            AI_GENERATE_CONTRACT,
            _generate_request(max_tokens=max_tokens),
        )


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_generate_result(known=False), "unavailable"),
        (_generate_result(cost=0.0001), "invalid"),
        (_generate_result(cost="0.000101"), "exceeds"),
    ],
)
def test_unknown_float_or_under_reserved_actual_cost_fails_closed(
    result: dict[str, Any], message: str,
) -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    _record_quote(registry, outer)
    admission = registry.admit(outer, AI_GENERATE_CONTRACT, _generate_request())
    with pytest.raises(PackVMBridgeBudgetError, match=message):
        registry.record(admission, result)


def test_generation_result_must_match_quoted_route_identity() -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    _record_quote(registry, outer)
    admission = registry.admit(outer, AI_GENERATE_CONTRACT, _generate_request())
    changed = _binding()
    changed["provider_instance_id"] = "replacement"
    with pytest.raises(PackVMBridgeBudgetError, match="route changed"):
        registry.record(admission, _generate_result(changed))


def test_per_million_rates_use_byte_upper_bound_and_ceiling_microusd() -> None:
    assert _estimated_cost_microusd(
        input_units=3,
        output_units=2,
        pricing={
            "input": "0.1",
            "output": "0.2",
            "currency": "USD",
            "unit": "usd_per_million_tokens",
        },
    ) == 1


def test_non_strategy_outer_call_is_not_reclassified_as_strategy_spend() -> None:
    registry = PackVMBridgeBudgetRegistry()
    outer = _outer()
    outer.contract_id = "conversation.turn.v1"
    assert registry.admit(outer, AI_GENERATE_CONTRACT, _generate_request()) is None
