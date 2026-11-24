"""Exact USD policies through the formal prepare/snapshot/provider boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from ecosystem.rumi_provider_registry_pack.domain.ai_client.provider_compiler.native_filters import (
    CAPABILITY_REVISION,
    normalize_native_filters,
)
from ecosystem.rumi_provider_registry_pack.runtime.model_access import (
    compile_connection_parameters,
)
from ecosystem.rumi_provider_registry_pack.runtime.model_access_configuration import (
    EXECUTE_OPERATION,
    prepare_model_access,
)
from tobkiri_host.broker import PreparedInvocationSnapshot
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.errors import CanonicalizationError

MODEL = "google/gemma-4-31b-it:free"
CONNECTION = {
    "provider_instance_id": "provider.main",
    "adapter_id": "openai-compatible",
    "endpoint": "https://openrouter.ai/api/v1",
    "metadata": {"catalog_provider_id": "openrouter"},
}


class Registry:
    """Only the public registry reads consumed by formal policy preparation."""

    profile_id = "defaults"

    def snapshot(self) -> dict[str, Any]:
        """Supply no credential or secret-bearing state."""
        return {"revision": 4, "providers": [deepcopy(CONNECTION)]}


def filters(price: Any) -> dict[str, Any]:
    """Build the exact existing public zero-price routing policy."""
    return {
        "revision": CAPABILITY_REVISION,
        "discovery": {},
        "routing": {"max_price": {"prompt": price, "completion": price}},
    }


def request(price: Any = 0) -> dict[str, Any]:
    """Use the same integer-zero shape produced by Native JSON.stringify."""
    return {
        "profile_id": "defaults",
        "provider_instance_id": "provider.main",
        "expected_revision": 4,
        "model_access": {
            "version": "tobkiri.connection-model-access.v1",
            "mode": "explicit",
            "model_ids": [MODEL],
            "native_filters": filters(price),
        },
    }


def snapshot(payload: dict[str, Any]) -> PreparedInvocationSnapshot:
    """Exercise the existing production snapshot strict JSON validator only."""
    return PreparedInvocationSnapshot(
        contract_id="tobkiri.action.ai.provider.registry.manage.v1",
        contract_version="1.0.0",
        operation_id=EXECUTE_OPERATION,
        normalized_payload=payload,
        request_digest="sha256:" + "a" * 64,
        timeout_ms=1000,
        idempotency_key=None,
        binding_fingerprint={},
        context_fingerprint={"profile_id": "defaults"},
        allow_lossy_adapters=False,
    )


def test_legacy_zero_prices_remain_canonical_integers() -> None:
    zero = 0
    original = filters(zero)
    normalized = normalize_native_filters(original, verified_openrouter=True)
    prices = normalized["routing"]["max_price"]
    assert prices == {"prompt": 0, "completion": 0}
    assert all(type(value) is int for value in prices.values())
    assert type(original["routing"]["max_price"]["prompt"]) is type(zero)
    canonical_json(normalized)


def test_formal_integer_zero_prepare_and_frozen_snapshot_pass() -> None:
    public_request = request()
    plan = prepare_model_access(
        Registry(),
        public_request,
        native_capability_revision=CAPABILITY_REVISION,
    )
    assert plan["request_digest"] == canonical_digest(public_request)
    assert plan["plan_digest"] == canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    policy = plan["model_access"]
    prices = policy["native_filters"]["routing"]["max_price"]
    assert all(type(value) is int for value in prices.values())
    frozen = snapshot({"request": public_request, "plan": plan})
    assert frozen.to_dict()["normalized_payload"]["plan"] == plan
    connection = {**CONNECTION, "model_access": policy}
    compiled = compile_connection_parameters(
        connection,
        MODEL,
        {"max_tokens": 2048},
    )
    assert compiled["provider"] == {
        "max_price": {"prompt": 0, "completion": 0},
        "allow_fallbacks": False,
    }
    assert all(
        type(value) is int for value in compiled["provider"]["max_price"].values()
    )
    canonical_json(compiled)


@pytest.mark.parametrize("raw_float_zero", [0.0, -0.0])
def test_raw_float_zero_request_remains_noncanonical(raw_float_zero: float) -> None:
    public_request = request(raw_float_zero)
    with pytest.raises(ValueError, match="plain-decimal USD"):
        prepare_model_access(
            Registry(),
            public_request,
            native_capability_revision=CAPABILITY_REVISION,
        )
    with pytest.raises(ValueError, match="bounded canonical JSON"):
        snapshot({"request": public_request})


@pytest.mark.parametrize(
    "price,expected",
    [("0.000000", "0"), ("0.375000", "0.375"), ("2.700000", "2.7"),
     ("0.000001", "0.000001"), ("9007199254.740991", "9007199254.740991"),
     (0, 0), (1, 1), (9007199254, 9007199254)],
)
def test_exact_prices_survive_policy_plan_and_frozen_snapshot(
    price: str | int, expected: str | int,
) -> None:
    original = filters(price)
    normalized = normalize_native_filters(original, verified_openrouter=True)
    prices = normalized["routing"]["max_price"]
    assert prices == {"prompt": expected, "completion": expected}
    assert all(type(value) is type(expected) for value in prices.values())
    assert original == filters(price)
    canonical_json(normalized)
    public_request = request(price)
    plan = prepare_model_access(
        Registry(), public_request, native_capability_revision=CAPABILITY_REVISION,
    )
    assert plan["request_digest"] == canonical_digest(public_request)
    assert plan["model_access"]["native_filters"] == normalized
    assert snapshot({"request": public_request, "plan": plan}).to_dict()[
        "normalized_payload"
    ]["plan"] == plan


@pytest.mark.parametrize("raw_float", [0.0, -0.0, 0.375, 1.0, 2.7, 1e-12])
def test_raw_float_prices_remain_noncanonical(raw_float: float) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(filters(raw_float))
    with pytest.raises(ValueError, match="plain-decimal USD"):
        normalize_native_filters(filters(raw_float), verified_openrouter=True)
    with pytest.raises(ValueError, match="plain-decimal USD"):
        prepare_model_access(
            Registry(), request(raw_float),
            native_capability_revision=CAPABILITY_REVISION,
        )


def test_outbound_decimal_limit_cannot_round_up_or_down() -> None:
    """Large exact policies fail closed if the numeric wire format rounds them."""
    for price in ("9007199254.740991", "9007199254.740989"):
        connection = {
            **CONNECTION,
            "model_access": request(price)["model_access"],
        }
        with pytest.raises(ValueError, match="represented exactly for transport"):
            compile_connection_parameters(connection, MODEL, {})


@pytest.mark.parametrize(
    "invalid", [
        -0.375, -1, True, False, float("nan"), float("inf"), float("-inf"),
        "-0", "-1", "+1", "1e0", "NaN", "Infinity", " 1", "1 ", "",
        ".1", "1.", "00", "01.2", "１", "1.0000000", "0.0000001",
        "9007199254.740992", "9007199255", 9007199255, 2**53,
        "9" * 5000,
    ]
)
def test_invalid_prices_still_fail_original_validation(invalid: Any) -> None:
    with pytest.raises(ValueError, match="plain-decimal USD"):
        normalize_native_filters(filters(invalid), verified_openrouter=True)


def test_unverified_provider_and_unknown_price_unit_still_deny() -> None:
    with pytest.raises(ValueError, match="unverified"):
        normalize_native_filters(filters(0), verified_openrouter=False)
    value = filters(0)
    value["routing"]["max_price"] = {"unknown": 0}
    with pytest.raises(ValueError, match="price fields"):
        normalize_native_filters(value, verified_openrouter=True)
