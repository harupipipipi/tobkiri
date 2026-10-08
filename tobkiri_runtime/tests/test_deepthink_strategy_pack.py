"""Tests for the externally installable DeepThink PackVM strategy Pack."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

import pytest

from ecosystem.rumi_deepthink_pack.runtime import strategy
from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner,
)


_PACK_ROOT = Path(__file__).parents[1] / "ecosystem" / "rumi_deepthink_pack"


def _request(**updates: Any) -> dict[str, Any]:
    """Build the generic strategy envelope supplied by the generic dispatcher."""

    value = {
        "request_id": "request.fixture",
        "model_reference": "fixture/model",
        "messages": [{"role": "user", "content": "hello"}],
        "parameters": {"max_tokens": 99999},
        "tools": [],
        "requirements": {"request_surface": "conversation.saved"},
        "profile_id": "defaults",
        "deadline": 1_800_000_000_000,
        "idempotency_key": "fixture-idempotency",
        "maximum_cost_microusd": 50_000,
    }
    value.update(updates)
    return value


def _quote(*, input_price: str = "0.000001") -> dict[str, Any]:
    """Return an owner-issued generic route quote fixture."""

    pricing = {
        "input": input_price,
        "output": input_price,
        "currency": "USD",
        "unit": "usd_per_token",
    }
    return {
        "ready": True,
        "model_id": "fixture/model",
        "provider_instance_id": "provider.fixture",
        "catalog_provider_instance_id": "catalog.fixture",
        "catalog_revision": "fixture.v1",
        "pricing_revision": "fixture.v1",
        "pricing": pricing,
        "route_binding": {
            "model_id": "fixture/model",
            "provider_instance_id": "provider.fixture",
            "catalog_provider_instance_id": "catalog.fixture",
            "catalog_revision": "fixture.v1",
            "pricing_revision": "fixture.v1",
            "pricing": pricing,
        },
    }


def _generation(output: str, **updates: Any) -> dict[str, Any]:
    """Return a generic Gateway response that matches the fixture quote."""

    value = {
        "status": "ok",
        "model_id": "fixture/model",
        "provider_instance_id": "provider.fixture",
        "catalog_provider_instance_id": "catalog.fixture",
        "catalog_revision": "fixture.v1",
        "pricing_revision": "fixture.v1",
        "output": output,
        "tool_intents": [],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "usage_cost": {"cost": "0.01", "currency": "USD", "known": True},
    }
    value.update(updates)
    return value


def _bridge_result(
    bridge_request: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the authenticated shape a generic PackVM bridge returns."""

    continuation = bridge_request["continuation"]
    return {
        "kind": strategy.PACKVM_BRIDGE_RESULT_KIND,
        "protocol": strategy.PACKVM_BRIDGE_PROTOCOL,
        "version": strategy.PACKVM_BRIDGE_VERSION,
        "operation_id": strategy.EXECUTE_OPERATION_ID,
        "nonce": continuation["nonce"],
        "target": dict(continuation["target"]),
        "request_digest": continuation["request_digest"],
        "result": dict(result),
        "result_digest": strategy._digest(result),
    }


def _success(result: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap one successful Host capability result."""

    return {"status": "ok", "value": dict(result)}


def _run(
    request: Mapping[str, Any], *, revise: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run all generic bridge hops without a Pack-specific Host dependency."""

    value = strategy.tobkiri_packvm_invoke(strategy.EXECUTE_OPERATION_ID, request)
    bridges: list[dict[str, Any]] = []
    review_count = 0
    previous_continuation: Mapping[str, Any] | None = None
    previous_result: Mapping[str, Any] | None = None
    while value.get("kind") == strategy.PACKVM_BRIDGE_REQUEST_KIND:
        packvm_guest_runner._validate_bridge_request(
            value,
            operation_id=strategy.EXECUTE_OPERATION_ID,
            previous_continuation=previous_continuation,
            previous_result=previous_result,
        )
        bridges.append(value)
        contract = value["target"]["contract_id"]
        if contract == strategy.QUOTE_CONTRACT_ID:
            outcome = _success(_quote())
        else:
            instruction = str(value["request"]["messages"][0]["content"])
            if "Create a concise private plan" in instruction:
                outcome = _success(_generation("Check the facts, then answer directly."))
            elif (
                "Review the candidate answer" in instruction
                or "Review the revised answer" in instruction
            ):
                review_count += 1
                approved = not revise or review_count > 1
                outcome = _success(
                    _generation(
                        json.dumps({"approved": approved, "feedback": "Tighten it."})
                    )
                )
            else:
                outcome = _success(_generation("A reviewed answer."))
        bridge_result = _bridge_result(value, outcome)
        packvm_guest_runner._bounded_bridge_json(bridge_result)
        previous_continuation = value["continuation"]
        previous_result = bridge_result
        value = strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            {
                "continuation": previous_continuation,
                "bridge_result": bridge_result,
            },
        )
    packvm_guest_runner._bounded_bridge_json(value)
    return value, bridges


def test_strategy_uses_only_generic_packvm_edges_and_bounds_phases() -> None:
    """The bounded algorithm asks the Host only for generic quote/generate calls."""

    result, bridges = _run(_request())

    assert result["output"] == "A reviewed answer."
    assert result["strategy_runtime"] == {
        "version": "1",
        "provider": "rumi_deepthink_pack",
        "approved": True,
        "tool_deferred": False,
        "calls": 3,
        "maximum_calls": 5,
        "maximum_cost_microusd": 50_000,
        "reserved_cost_usd": result["strategy_runtime"]["reserved_cost_usd"],
        "used_cost_usd": "0.03",
        "phases": result["strategy_runtime"]["phases"],
    }
    assert [bridge["target"] for bridge in bridges] == [
        {"contract_id": strategy.QUOTE_CONTRACT_ID},
        {"contract_id": strategy.GENERATE_CONTRACT_ID},
        {"contract_id": strategy.QUOTE_CONTRACT_ID},
        {"contract_id": strategy.GENERATE_CONTRACT_ID},
        {"contract_id": strategy.QUOTE_CONTRACT_ID},
        {"contract_id": strategy.GENERATE_CONTRACT_ID},
    ]
    assert all("operation_id" not in bridge["target"] for bridge in bridges)
    generation_payloads = [
        bridge["request"]
        for bridge in bridges
        if bridge["target"]["contract_id"] == strategy.GENERATE_CONTRACT_ID
    ]
    assert all(payload["parameters"]["max_tokens"] == 1024 for payload in generation_payloads)
    assert len({payload["idempotency_key"] for payload in generation_payloads}) == 3
    assert all(payload["allow_failover"] is False for payload in generation_payloads)
    assert all(
        payload["route_binding"]["provider_instance_id"] == "provider.fixture"
        for payload in generation_payloads
    )
    assert all(
        payload["requirements"]["preferred_model_id"] == "fixture/model"
        and payload["requirements"]["preferred_provider_instance_id"]
        == "provider.fixture"
        for payload in generation_payloads
    )


def test_strategy_supports_the_bounded_revision_path() -> None:
    """A rejected draft can use exactly the remaining two model calls."""

    result, bridges = _run(_request(), revise=True)

    assert result["strategy_runtime"]["calls"] == 5
    assert len(bridges) == 10
    assert all(
        bridge["continuation"]["max_hops"] == 10
        and bridge["continuation"]["hop"] == index
        for index, bridge in enumerate(bridges)
    )


def test_strategy_fails_before_generation_when_quote_has_no_pricing() -> None:
    """A missing owner price cannot be bypassed by an ordinary generation."""

    bridge = strategy.tobkiri_packvm_invoke(
        strategy.EXECUTE_OPERATION_ID, _request()
    )
    quote = _quote()
    quote["pricing"]["input"] = None
    quote["route_binding"]["pricing"]["input"] = None

    with pytest.raises(strategy.StrategyBudgetError, match="pricing is unavailable"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            {
                "continuation": bridge["continuation"],
                "bridge_result": _bridge_result(bridge, _success(quote)),
            },
        )


def test_strategy_requires_the_owner_issued_per_token_pricing_unit() -> None:
    """A quoted decimal rate is meaningful only with its generic unit."""

    bridge = strategy.tobkiri_packvm_invoke(
        strategy.EXECUTE_OPERATION_ID, _request()
    )
    quote = _quote()
    quote["pricing"]["unit"] = "usd_per_request"
    quote["route_binding"]["pricing"]["unit"] = "usd_per_request"

    with pytest.raises(strategy.StrategyBudgetError, match="pricing unit"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            {
                "continuation": bridge["continuation"],
                "bridge_result": _bridge_result(bridge, _success(quote)),
            },
        )


def test_strategy_rejects_noncanonical_numbers_at_packvm_boundaries() -> None:
    """Neither a client nor a generic provider can smuggle floats over PackVM."""

    with pytest.raises(ValueError, match="floating point"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            _request(deadline=1_800_000_000_000.0),
        )

    missing_budget = _request()
    missing_budget.pop("maximum_cost_microusd")
    with pytest.raises(ValueError, match="maximum_cost_microusd"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID, missing_budget
        )

    bridge = strategy.tobkiri_packvm_invoke(
        strategy.EXECUTE_OPERATION_ID, _request()
    )
    floating_quote = _quote(input_price="0.000001")
    floating_quote["pricing"]["input"] = 0.000001
    floating_quote["route_binding"]["pricing"]["input"] = 0.000001
    outcome = _success(floating_quote)
    with pytest.raises(ValueError, match="JSON-compatible"):
        packvm_guest_runner._bounded_bridge_json(_bridge_result(bridge, outcome))


def test_strategy_rejects_credentials_and_legacy_selection_flags() -> None:
    """Selection and credentials stay outside an independent strategy Pack."""

    with pytest.raises(ValueError, match="credentials"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            _request(parameters={"api_key": "not-accepted"}),
        )

    with pytest.raises(ValueError, match="owner media reference"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            _request(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,AA=="},
                            }
                        ],
                    }
                ]
            ),
        )
    with pytest.raises(ValueError, match="selection"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            _request(requirements={"deepthink": True}),
        )


def test_strategy_preserves_structured_model_reference_and_image_content() -> None:
    """Generic multimodal messages remain available to a strategy Pack."""

    _result, bridges = _run(
        _request(
            model_reference={"profile_id": "model-profile.fixture"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/image.png"},
                        },
                    ],
                }
            ],
        )
    )

    payloads = [bridge["request"] for bridge in bridges]
    assert all(
        payload["model_reference"] == {"profile_id": "model-profile.fixture"}
        for payload in payloads
    )
    assert all(
        any(
            isinstance(message.get("content"), list)
            and any(block.get("type") == "image_url" for block in message["content"])
            for message in payload["messages"]
        )
        for payload in payloads
    )


def test_strategy_rejects_tampered_state_and_cross_target_bridge_result() -> None:
    """The fresh guest child accepts only its exact sealed bridge continuation."""

    bridge = strategy.tobkiri_packvm_invoke(
        strategy.EXECUTE_OPERATION_ID, _request()
    )
    tampered = copy.deepcopy(bridge)
    tampered["continuation"]["state"]["request"]["request_id"] = "other"
    with pytest.raises(ValueError, match="state binding"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            {
                "continuation": tampered["continuation"],
                "bridge_result": _bridge_result(tampered, _success(_quote())),
            },
        )
    cross_target = _bridge_result(bridge, _success(_quote()))
    cross_target["target"] = {"contract_id": strategy.GENERATE_CONTRACT_ID}
    with pytest.raises(ValueError, match="bridge result"):
        strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            {"continuation": bridge["continuation"], "bridge_result": cross_target},
        )


def test_strategy_ui_contribution_is_declarative_and_uses_full_reference() -> None:
    """The UI catalog may discover the strategy without a built-in whitelist."""

    descriptor = json.loads(
        (_PACK_ROOT / "frontend/contributions/ai-strategy.json").read_text(
            encoding="utf-8"
        )
    )
    assert descriptor["kind"] == "ai_strategy"
    assert descriptor["mode"] == "declarative"
    assert descriptor["strategy_reference"] == strategy.EXECUTE_OPERATION_ID
    assert descriptor["command"]["name"] == "deepthink"


def test_staged_strategy_exports_only_the_packvm_abi() -> None:
    """An external Pack runs without repository imports or site packages."""

    source = _PACK_ROOT / "runtime/strategy.py"
    script = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location('staged_strategy', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.tobkiri_packvm_invoke(
    module.EXECUTE_OPERATION_ID,
    {'request_id':'isolated', 'model_reference':'fixture/model',
     'messages':[{'role':'user', 'content':'hello'}], 'deadline':1,
     'idempotency_key':'isolated', 'maximum_cost_microusd': 50000}), sort_keys=True))
"""
    process = subprocess.run(
        (sys.executable, "-B", "-I", "-S", "-c", script, str(source)),
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert process.returncode == 0, process.stderr
    initial = json.loads(process.stdout)
    assert initial["kind"] == strategy.PACKVM_BRIDGE_REQUEST_KIND
    assert initial["target"] == {"contract_id": strategy.QUOTE_CONTRACT_ID}
    assert "core_runtime" not in source.read_text(encoding="utf-8")
    assert "rumi_ai_gateway_pack" not in source.read_text(encoding="utf-8")


def _scripted_run(outputs: list[str], **request_updates: Any) -> dict[str, Any]:
    """Drive the actual sealed strategy with a deterministic provider transcript."""
    value = strategy.tobkiri_packvm_invoke(
        strategy.EXECUTE_OPERATION_ID, _request(**request_updates)
    )
    while value.get("kind") == strategy.PACKVM_BRIDGE_REQUEST_KIND:
        if value["target"]["contract_id"] == strategy.QUOTE_CONTRACT_ID:
            outcome = _success(_quote())
        else:
            outcome = _success(_generation(outputs.pop(0)))
        value = strategy.tobkiri_packvm_invoke(
            strategy.EXECUTE_OPERATION_ID,
            {"continuation": value["continuation"],
             "bridge_result": _bridge_result(value, outcome)},
        )
    return value


@pytest.mark.parametrize("review", ["not JSON", "[]", '{"approved":"yes","feedback":""}', '{"approved":true,"feedback":"","extra":"execute"}'])
def test_strategy_malformed_reviews_never_publish_candidate(review: str) -> None:
    """Malformed review output cannot become a successful final answer."""
    with pytest.raises(strategy.StrategyBudgetError, match="reviewer"):
        _scripted_run(["Plan", "Unverified draft", review])


def test_strategy_empty_repair_and_repeated_rejection_fail_closed() -> None:
    """The only repair must contain text and pass the bounded final review."""
    rejected = '{"approved":false,"feedback":"Correct the answer"}'
    with pytest.raises(strategy.StrategyBudgetError, match="revision returned no text"):
        _scripted_run(["Plan", "Draft", rejected, "  "])
    with pytest.raises(strategy.StrategyBudgetError, match="rejected the bounded"):
        _scripted_run(["Plan", "Draft", rejected, "Repaired", rejected])


def test_strategy_cancelled_bridge_stops_before_any_further_generation() -> None:
    """Cancellation is propagated by the generic bridge rather than completion."""
    bridge = strategy.tobkiri_packvm_invoke(strategy.EXECUTE_OPERATION_ID, _request())
    outcome = {"status": "error", "error": {"code": "CANCELLED", "message": "Cancelled by Host"}}
    result = strategy.tobkiri_packvm_invoke(strategy.EXECUTE_OPERATION_ID, {
        "continuation": bridge["continuation"],
        "bridge_result": _bridge_result(bridge, outcome),
    })
    assert result == outcome
    assert "output" not in result


def test_strategy_tool_phases_require_native_tool_calling_at_quote_and_generate() -> None:
    """The Gateway must reject non-tool models before a tool-bearing generation."""
    _result, bridges = _run(_request(tools=[{"name": "fixture_tool"}], requirements={"tool_calling": False}))
    draft = [bridge for bridge in bridges if bridge["continuation"]["state"]["pending"]["phase"] == "draft"]
    assert len(draft) == 2
    assert all(bridge["request"]["requirements"]["tool_calling"] is True for bridge in draft)
    plan = [bridge for bridge in bridges if bridge["continuation"]["state"]["pending"]["phase"] == "plan"]
    assert all(bridge["request"]["requirements"]["tool_calling"] is False for bridge in plan)
