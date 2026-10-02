"""Host-owned aggregate cost admission for strategy PackVM bridges.

The sandbox is allowed to propose route-quote and generation calls, but its
continuation state is not an accounting authority.  This module derives every
reservation from the immutable outer Host request, a Host-returned route
quote, and the later Host-returned usage report.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import math
import re
import threading
import time
from typing import Any, Mapping

from tobkiri_protocol.canonical import MAX_SAFE_INTEGER, canonical_digest, canonical_json


STRATEGY_EXECUTE_CONTRACT = "tobkiri.service.ai.strategy.execute.v1"
ROUTE_QUOTE_CONTRACT = "tobkiri.resource.ai.route.quote.v1"
AI_GENERATE_CONTRACT = "tobkiri.service.ai.generate.v1"
MAX_GENERATION_TOKENS = 1_000_000
MAX_ACTIVE_LEDGERS = 4_096
_DECIMAL_TEXT = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_PRICE_UNITS = frozenset({"usd_per_token", "usd_per_million_tokens"})
_IDENTITY_FIELDS = (
    "model_id",
    "provider_instance_id",
    "catalog_provider_instance_id",
    "catalog_revision",
    "pricing_revision",
)


class PackVMBridgeBudgetError(ValueError):
    """Raised when an untrusted bridge chain cannot prove its cost bound."""


@dataclass(frozen=True)
class BridgeBudgetAdmission:
    """One opaque, single-use admission passed from preflight to recording."""

    ledger_key: tuple[str, str, str]
    kind: str
    quote_digest: str | None = None
    reserved_microusd: int = 0


@dataclass
class _Quote:
    binding: dict[str, Any]
    remaining_uses: int = 1


@dataclass
class _Ledger:
    deadline_monotonic: float
    maximum_microusd: int
    reserved_microusd: int
    quotes: dict[str, _Quote]


class PackVMBridgeBudgetRegistry:
    """Maintain bounded Host accounting across fresh PackVM child processes."""

    def __init__(self, *, max_ledgers: int = MAX_ACTIVE_LEDGERS) -> None:
        if type(max_ledgers) is not int or max_ledgers <= 0:
            raise ValueError("PackVM bridge budget ledger bound is invalid")
        self._max_ledgers = max_ledgers
        self._ledgers: dict[tuple[str, str, str], _Ledger] = {}
        self._lock = threading.RLock()

    def admit(
        self,
        outer_request: object,
        target_contract_id: str,
        request: Mapping[str, Any],
    ) -> BridgeBudgetAdmission | None:
        """Reserve a bridge call before any nested Provider is invoked.

        Non-strategy outer operations and non-cost bridge contracts are outside
        this ledger.  A strategy generation always requires a prior, unused
        Host quote with an exact route binding.
        """

        if getattr(outer_request, "contract_id", None) != STRATEGY_EXECUTE_CONTRACT:
            return None
        if target_contract_id not in {ROUTE_QUOTE_CONTRACT, AI_GENERATE_CONTRACT}:
            return None
        key, deadline, maximum = _outer_budget_identity(outer_request)
        now = time.monotonic()
        if now >= deadline:
            raise PackVMBridgeBudgetError("PackVM bridge budget deadline expired")
        with self._lock:
            self._prune(now)
            ledger = self._ledgers.get(key)
            if ledger is None:
                if len(self._ledgers) >= self._max_ledgers:
                    raise PackVMBridgeBudgetError(
                        "PackVM bridge budget ledger is exhausted"
                    )
                ledger = _Ledger(deadline, maximum, 0, {})
                self._ledgers[key] = ledger
            elif (
                ledger.deadline_monotonic != deadline
                or ledger.maximum_microusd != maximum
            ):
                raise PackVMBridgeBudgetError(
                    "PackVM bridge budget identity changed"
                )
            if target_contract_id == ROUTE_QUOTE_CONTRACT:
                return BridgeBudgetAdmission(key, "quote")
            return self._admit_generation(key, ledger, request)

    def record(
        self,
        admission: BridgeBudgetAdmission | None,
        result: Mapping[str, Any],
    ) -> None:
        """Record a Host result and reject unknown or under-reserved cost."""

        if admission is None:
            return
        with self._lock:
            ledger = self._ledgers.get(admission.ledger_key)
            if ledger is None:
                raise PackVMBridgeBudgetError("PackVM bridge budget ledger is missing")
            value = _successful_value(result)
            if admission.kind == "quote":
                binding = _validated_quote_binding(value)
                digest = canonical_digest(binding)
                current = ledger.quotes.get(digest)
                if current is None:
                    ledger.quotes[digest] = _Quote(binding=binding)
                else:
                    current.remaining_uses += 1
                return
            if admission.kind != "generate" or admission.quote_digest is None:
                raise PackVMBridgeBudgetError("PackVM bridge budget admission is invalid")
            quote = ledger.quotes.get(admission.quote_digest)
            if quote is None:
                raise PackVMBridgeBudgetError("PackVM bridge route quote is missing")
            _validate_generation_identity(value, quote.binding)
            actual = _actual_cost_microusd(value)
            if actual > admission.reserved_microusd:
                raise PackVMBridgeBudgetError(
                    "PackVM bridge reported cost exceeds its reservation"
                )

    def _admit_generation(
        self,
        key: tuple[str, str, str],
        ledger: _Ledger,
        request: Mapping[str, Any],
    ) -> BridgeBudgetAdmission:
        binding = request.get("route_binding")
        if not isinstance(binding, Mapping):
            raise PackVMBridgeBudgetError("PackVM bridge route binding is missing")
        binding_value = _validated_route_binding(binding)
        digest = canonical_digest(binding_value)
        quote = ledger.quotes.get(digest)
        if quote is None or quote.remaining_uses <= 0:
            raise PackVMBridgeBudgetError(
                "PackVM bridge generation requires an unused Host quote"
            )
        parameters = request.get("parameters")
        max_tokens = parameters.get("max_tokens") if isinstance(parameters, Mapping) else None
        if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_GENERATION_TOKENS:
            raise PackVMBridgeBudgetError("PackVM bridge max_tokens is invalid")
        messages = request.get("messages")
        tools = request.get("tools", [])
        if not isinstance(messages, list) or not messages or not isinstance(tools, list):
            raise PackVMBridgeBudgetError("PackVM bridge generation content is invalid")
        try:
            # Count the complete provider request, not only messages.  This is
            # deliberately conservative and prevents an arbitrary strategy
            # Pack from hiding billable text in another schema-valid field.
            input_units = len(canonical_json(dict(request)))
        except Exception as exc:
            raise PackVMBridgeBudgetError(
                "PackVM bridge generation content is invalid"
            ) from exc
        pricing = quote.binding["pricing"]
        reserved = _estimated_cost_microusd(
            input_units=input_units,
            output_units=max_tokens,
            pricing=pricing,
        )
        if ledger.reserved_microusd + reserved > ledger.maximum_microusd:
            raise PackVMBridgeBudgetError(
                "PackVM bridge aggregate cost exceeds its Host budget"
            )
        # Consume and reserve before provider invocation.  Neither is rolled
        # back on failure, so retries cannot multiply spend after an uncertain
        # provider outcome.
        quote.remaining_uses -= 1
        ledger.reserved_microusd += reserved
        return BridgeBudgetAdmission(key, "generate", digest, reserved)

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, ledger in self._ledgers.items()
            if now >= ledger.deadline_monotonic
        ]
        for key in expired:
            self._ledgers.pop(key, None)


def _outer_budget_identity(
    outer_request: object,
) -> tuple[tuple[str, str, str], float, int]:
    context = getattr(outer_request, "context", None)
    activation = getattr(context, "activation_digest", None)
    request_id = getattr(context, "request_id", None)
    request_digest = getattr(outer_request, "request_digest", None)
    deadline = getattr(outer_request, "deadline_monotonic", None)
    payload = getattr(outer_request, "payload", None)
    maximum = payload.get("maximum_cost_microusd") if isinstance(payload, Mapping) else None
    if (
        not isinstance(activation, str)
        or not _valid_digest(activation)
        or not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 160
        or not isinstance(request_digest, str)
        or not _valid_digest(request_digest)
    ):
        raise PackVMBridgeBudgetError("PackVM bridge budget identity is invalid")
    if (
        not isinstance(deadline, (int, float))
        or isinstance(deadline, bool)
        or not math.isfinite(deadline)
    ):
        raise PackVMBridgeBudgetError("PackVM bridge budget deadline is invalid")
    if type(maximum) is not int or not 1 <= maximum <= MAX_SAFE_INTEGER:
        raise PackVMBridgeBudgetError("PackVM bridge maximum cost is invalid")
    return (activation, request_id, request_digest), float(deadline), maximum


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _successful_value(result: Mapping[str, Any]) -> Mapping[str, Any]:
    if (
        not isinstance(result, Mapping)
        or set(result) != {"status", "value"}
        or result.get("status") != "ok"
        or not isinstance(result.get("value"), Mapping)
    ):
        raise PackVMBridgeBudgetError("PackVM bridge cost result is unavailable")
    return result["value"]


def _validated_quote_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("ready") is not True or not isinstance(value.get("route_binding"), Mapping):
        raise PackVMBridgeBudgetError("PackVM bridge route quote is unavailable")
    binding = _validated_route_binding(value["route_binding"])
    if any(value.get(field) != binding[field] for field in _IDENTITY_FIELDS):
        raise PackVMBridgeBudgetError("PackVM bridge route quote identity is invalid")
    if dict(value.get("pricing") or {}) != binding["pricing"]:
        raise PackVMBridgeBudgetError("PackVM bridge route quote pricing is invalid")
    return binding


def _validated_route_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = set(_IDENTITY_FIELDS) | {"pricing"}
    if set(value) != expected:
        raise PackVMBridgeBudgetError("PackVM bridge route binding is invalid")
    result: dict[str, Any] = {}
    for field in _IDENTITY_FIELDS:
        item = value.get(field)
        if (
            not isinstance(item, str)
            or (not item and field != "catalog_provider_instance_id")
            or len(item) > 512
        ):
            raise PackVMBridgeBudgetError("PackVM bridge route binding is invalid")
        result[field] = item
    pricing = value.get("pricing")
    if not isinstance(pricing, Mapping) or set(pricing) != {
        "input", "output", "currency", "unit"
    }:
        raise PackVMBridgeBudgetError("PackVM bridge pricing is invalid")
    if pricing.get("currency") != "USD" or pricing.get("unit") not in _PRICE_UNITS:
        raise PackVMBridgeBudgetError("PackVM bridge pricing unit is invalid")
    result["pricing"] = {
        "input": _decimal_text(pricing.get("input"), "pricing input"),
        "output": _decimal_text(pricing.get("output"), "pricing output"),
        "currency": "USD",
        "unit": pricing["unit"],
    }
    return result


def _estimated_cost_microusd(
    *, input_units: int, output_units: int, pricing: Mapping[str, Any]
) -> int:
    # A process-global Decimal context may be too small for an attacker-chosen
    # bounded rate.  Use enough local precision that admission never rounds a
    # reservation downward before the final ceiling.
    with localcontext() as context:
        context.prec = 256
        input_rate = Decimal(str(pricing["input"]))
        output_rate = Decimal(str(pricing["output"]))
        amount = (
            Decimal(input_units) * input_rate
            + Decimal(output_units) * output_rate
        )
        if pricing["unit"] == "usd_per_token":
            amount *= Decimal(1_000_000)
        # USD-per-million-token rates numerically equal micro-USD per token.
        return int(amount.to_integral_value(rounding=ROUND_CEILING))


def _actual_cost_microusd(value: Mapping[str, Any]) -> int:
    usage = value.get("usage_cost")
    if (
        not isinstance(usage, Mapping)
        or usage.get("known") is not True
        or usage.get("currency") != "USD"
    ):
        raise PackVMBridgeBudgetError("PackVM bridge reported cost is unavailable")
    with localcontext() as context:
        context.prec = 256
        cost = Decimal(_decimal_text(usage.get("cost"), "reported cost"))
        return int(
            (cost * Decimal(1_000_000)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )


def _validate_generation_identity(
    value: Mapping[str, Any], binding: Mapping[str, Any]
) -> None:
    if any(value.get(field) != binding[field] for field in _IDENTITY_FIELDS):
        raise PackVMBridgeBudgetError("PackVM bridge generation route changed")


def _decimal_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or _DECIMAL_TEXT.fullmatch(value) is None:
        raise PackVMBridgeBudgetError(f"PackVM bridge {label} is invalid")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise PackVMBridgeBudgetError(f"PackVM bridge {label} is invalid") from exc
    if not number.is_finite() or number < 0:
        raise PackVMBridgeBudgetError(f"PackVM bridge {label} is invalid")
    return value
