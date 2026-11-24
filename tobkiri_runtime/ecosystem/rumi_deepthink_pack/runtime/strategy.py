"""A bounded, provider-neutral deliberation strategy PackVM entrypoint.

This artifact owns only the strategy state machine.  Each model route quote
and generation is requested through the PackVM capability bridge.  The Host
resolves the generic contract against the signed Profile edges, so this Pack
never imports Host code, opens a network connection, accepts credentials, or
names another Pack's implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Mapping


PACK_ID = "rumi_deepthink_pack"
EXECUTE_CONTRACT_ID = "tobkiri.service.ai.strategy.execute.v1"
EXECUTE_OPERATION_ID = "rumi_deepthink_pack.deepthink.execute"
GENERATE_CONTRACT_ID = "tobkiri.service.ai.generate.v1"
QUOTE_CONTRACT_ID = "tobkiri.resource.ai.route.quote.v1"

PACKVM_BRIDGE_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
PACKVM_BRIDGE_VERSION = 1
PACKVM_BRIDGE_REQUEST_KIND = "tobkiri.packvm.bridge.request.v1"
PACKVM_BRIDGE_RESULT_KIND = "tobkiri.packvm.bridge.result.v1"
PACKVM_CONTINUATION_KIND = "tobkiri.packvm.continuation.v1"

_QUOTE_TARGET = {"contract_id": QUOTE_CONTRACT_ID}
_GENERATE_TARGET = {"contract_id": GENERATE_CONTRACT_ID}
_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "model_profile_id",
        "model_reference",
        "messages",
        "input",
        "parameters",
        "tools",
        "requirements",
        "profile_id",
        "deadline",
        "idempotency_key",
        "maximum_cost_microusd",
        "policy_revision",
        "allow_failover",
        "system_prompt_digest",
    }
)
_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credential_handle",
        "password",
        "secret",
        "token",
    }
)
_PHASES = frozenset({"plan", "draft", "review", "revision", "final_review"})
_PENDING_KINDS = frozenset({"quote", "generate"})
_MAX_CALLS = 5
_MAX_HOPS = 10
_MAX_OUTPUT_TOKENS = 1024
_USD_MICROS = 1_000_000
_MAXIMUM_COST_MICROUSD = 1_000_000
_PRICING_UNIT = "usd_per_token"
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_STATE_BYTES = 16 * 1024
_MAX_BRIDGE_FRAME_BYTES = 64 * 1024
_MAX_BRIDGE_RESULT_BYTES = 512 * 1024
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class StrategyBudgetError(ValueError):
    """Raised when the Pack cannot safely request another model call."""


def tobkiri_packvm_invoke(
    operation_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Start or resume one sealed, bounded DeepThink strategy invocation.

    Initial input is the generic strategy envelope.  A resume accepts only a
    Host-provided bridge result paired with the exact continuation previously
    emitted by this Pack.  The PackVM supervisor retains that continuation,
    so a caller cannot substitute a state snapshot between fresh sandbox
    children.
    """

    if operation_id != EXECUTE_OPERATION_ID or not isinstance(payload, Mapping):
        raise ValueError("AI strategy operation is invalid")
    if set(payload) == {"continuation", "bridge_result"}:
        continuation = _validate_continuation(payload["continuation"])
        bridge_result = _validate_bridge_result(
            payload["bridge_result"], continuation
        )
        state = _validate_state(continuation["state"])
        return _resume(state, continuation, bridge_result)
    return _start(_validate_request(payload))


def _start(request: dict[str, Any]) -> dict[str, Any]:
    state = {
        "version": 1,
        "request": request,
        "ledger": {
            "calls": 0,
            "maximum_cost_microusd": _maximum_cost_microusd(
                request.get("maximum_cost_microusd")
            ),
            "reserved_cost_usd": "0",
            "actual_cost_usd": "0",
            "actual_cost_known": True,
            "usage": {},
            "phases": [],
        },
        "pending": {
            "phase": "plan",
            "kind": "quote",
            "messages": _with_instruction(
                request["messages"],
                "Create a concise private plan for answering the request. "
                "Identify the main claims, missing assumptions, and risks. "
                "Return plain text.",
            ),
            "tools": [],
        },
    }
    return _bridge_request(state, hop=0, previous_result_digest=None)


def _resume(
    state: dict[str, Any],
    continuation: Mapping[str, Any],
    bridge_result: Mapping[str, Any],
) -> dict[str, Any]:
    pending = state["pending"]
    request = state["request"]
    ledger = state["ledger"]
    result = bridge_result["result"]
    if result["status"] == "error":
        return _bridge_error(result["error"])

    if pending["kind"] == "quote":
        quote = _validated_quote(result["value"])
        estimated = _estimate_call_cost(
            pending["messages"],
            pending["tools"],
            _bounded_parameters(request.get("parameters"))["max_tokens"],
            quote,
        )
        _admit(ledger, estimated)
        reserved = _microusd(ledger["reserved_cost_usd"], "reserved_cost_usd")
        ledger["reserved_cost_usd"] = _usd_text(reserved + estimated)
        state["pending"] = {
            **pending,
            "kind": "generate",
            "quote": quote,
            "estimated_cost_usd": _usd_text(estimated),
        }
        return _bridge_request(
            state,
            hop=int(continuation["hop"]) + 1,
            previous_result_digest=bridge_result["result_digest"],
        )

    generation = _validated_generation(result["value"], pending["quote"])
    _record_generation(
        ledger,
        pending["phase"],
        generation,
        _microusd(pending["estimated_cost_usd"], "estimated_cost_usd"),
    )
    return _advance_after_generation(
        state,
        generation,
        hop=int(continuation["hop"]) + 1,
        previous_result_digest=bridge_result["result_digest"],
    )


def _advance_after_generation(
    state: dict[str, Any],
    generation: dict[str, Any],
    *,
    hop: int,
    previous_result_digest: str,
) -> dict[str, Any]:
    request = state["request"]
    phase = state["pending"]["phase"]
    if phase == "plan":
        plan = _required_text(generation, "plan")
        return _next_quote(
            state,
            phase="draft",
            messages=_with_instruction(
                request["messages"],
                "Use the following private planning artifact to produce the best "
                "answer or required tool call. Do not mention the plan.\n\n"
                f"{plan}",
            ),
            tools=request.get("tools", []),
            hop=hop,
            previous_result_digest=previous_result_digest,
        )
    if phase == "draft":
        if generation.get("tool_intents"):
            return _finish(state["ledger"], generation, approved=False, deferred=True)
        draft = _required_text(generation, "draft")
        state["candidate"] = generation
        return _next_quote(
            state,
            phase="review",
            messages=_with_instruction(
                request["messages"],
                "Review the candidate answer for correctness, completeness, and "
                "alignment with the request. Return only JSON shaped as "
                '{"approved": boolean, "feedback": string}.\n\n'
                f"Candidate answer:\n{draft}",
            ),
            tools=[],
            hop=hop,
            previous_result_digest=previous_result_digest,
        )
    if phase == "review":
        review = _review(generation)
        candidate = _candidate(state)
        if review["approved"]:
            return _finish(state["ledger"], candidate, approved=True, deferred=False)
        draft = _required_text(candidate, "draft")
        return _next_quote(
            state,
            phase="revision",
            messages=_with_instruction(
                request["messages"],
                "Rewrite the candidate answer using the review feedback. Return "
                "only the improved answer or a required tool call.\n\n"
                f"Candidate answer:\n{draft}\n\nFeedback:\n{review['feedback']}",
            ),
            tools=request.get("tools", []),
            hop=hop,
            previous_result_digest=previous_result_digest,
        )
    if phase == "revision":
        if generation.get("tool_intents"):
            return _finish(state["ledger"], generation, approved=False, deferred=True)
        revision = _required_text(generation, "revision")
        state["candidate"] = generation
        return _next_quote(
            state,
            phase="final_review",
            messages=_with_instruction(
                request["messages"],
                "Review the revised answer. Return only JSON shaped as "
                '{"approved": boolean, "feedback": string}.\n\n'
                f"Revised answer:\n{revision}",
            ),
            tools=[],
            hop=hop,
            previous_result_digest=previous_result_digest,
        )
    if phase == "final_review":
        if not _review(generation)["approved"]:
            raise StrategyBudgetError("strategy reviewer rejected the bounded revision")
        return _finish(
            state["ledger"], _candidate(state), approved=True, deferred=False
        )
    raise ValueError("AI strategy phase is invalid")


def _next_quote(
    state: dict[str, Any],
    *,
    phase: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    hop: int,
    previous_result_digest: str,
) -> dict[str, Any]:
    state["pending"] = {
        "phase": phase,
        "kind": "quote",
        "messages": messages,
        "tools": tools,
    }
    return _bridge_request(
        state,
        hop=hop,
        previous_result_digest=previous_result_digest,
    )


def _bridge_request(
    state: Mapping[str, Any],
    *,
    hop: int,
    previous_result_digest: str | None,
) -> dict[str, Any]:
    _reject_floats(state, label="AI strategy state")
    checked_state = _validate_state(state)
    if hop < 0 or hop >= _MAX_HOPS:
        raise StrategyBudgetError("strategy bridge hop budget exhausted")
    pending = checked_state["pending"]
    request = checked_state["request"]
    if pending["kind"] == "quote":
        target = _QUOTE_TARGET
        bridge_payload = _quote_payload(request, pending["messages"], pending["tools"])
    else:
        target = _GENERATE_TARGET
        bridge_payload = _generate_payload(request, pending)
    request_digest = _digest(bridge_payload)
    state_digest = _digest(checked_state)
    continuation = {
        "kind": PACKVM_CONTINUATION_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "operation_id": EXECUTE_OPERATION_ID,
        "nonce": secrets.token_hex(24),
        "target": dict(target),
        "request_digest": request_digest,
        "hop": hop,
        "max_hops": _MAX_HOPS,
        "previous_result_digest": previous_result_digest,
        "state": checked_state,
        "state_digest": state_digest,
    }
    frame = {
        "kind": PACKVM_BRIDGE_REQUEST_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "target": dict(target),
        "request": bridge_payload,
        "request_digest": request_digest,
        "continuation": continuation,
    }
    _reject_floats(frame, label="AI strategy bridge request")
    _assert_json(frame, _MAX_BRIDGE_FRAME_BYTES, "AI strategy bridge request")
    return frame


def _quote_payload(
    request: Mapping[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": messages,
        "requirements": dict(request.get("requirements") or {}),
    }
    if tools:
        payload["requirements"]["tool_calling"] = True
    for key in ("model_profile_id", "model_reference"):
        if request.get(key) is not None:
            payload[key] = request[key]
    return payload


def _generate_payload(
    request: Mapping[str, Any], pending: Mapping[str, Any]
) -> dict[str, Any]:
    quote = pending.get("quote")
    if not isinstance(quote, Mapping):
        raise ValueError("AI strategy quote state is invalid")
    requirements = dict(request.get("requirements") or {})
    if pending["tools"]:
        requirements["tool_calling"] = True
    requirements["preferred_model_id"] = quote["model_id"]
    requirements["preferred_provider_instance_id"] = quote[
        "provider_instance_id"
    ]
    payload: dict[str, Any] = {
        "request_id": _phase_identifier(
            str(request["request_id"]), str(pending["phase"])
        ),
        "messages": pending["messages"],
        "parameters": _bounded_parameters(request.get("parameters")),
        "tools": pending["tools"],
        "requirements": requirements,
        "route_binding": dict(quote["route_binding"]),
        "deadline": request["deadline"],
        "idempotency_key": _phase_identifier(
            str(request["idempotency_key"]), str(pending["phase"])
        ),
        "allow_failover": False,
    }
    for key in (
        "input",
        "model_profile_id",
        "model_reference",
        "profile_id",
        "policy_revision",
    ):
        if request.get(key) is not None:
            payload[key] = request[key]
    return payload


def _validated_quote(value: Any) -> dict[str, Any]:
    _reject_floats(value, label="AI strategy route quote")
    if not isinstance(value, Mapping) or value.get("ready") is not True:
        raise StrategyBudgetError("strategy route quote is unavailable")
    pricing = value.get("pricing")
    if not isinstance(pricing, Mapping):
        raise StrategyBudgetError("strategy pricing is unavailable")
    try:
        input_rate = _decimal_usd(pricing.get("input"), "pricing input")
        output_rate = _decimal_usd(pricing.get("output"), "pricing output")
    except ValueError as exc:
        raise StrategyBudgetError("strategy pricing is unavailable") from exc
    if input_rate is None or output_rate is None:
        raise StrategyBudgetError("strategy pricing is unavailable")
    if str(pricing.get("currency") or "").upper() != "USD":
        raise StrategyBudgetError("strategy pricing currency is unsupported")
    if pricing.get("unit") != _PRICING_UNIT:
        raise StrategyBudgetError("strategy pricing unit is unsupported")
    quote = {
        "input": input_rate,
        "output": output_rate,
        "currency": "USD",
        "unit": _PRICING_UNIT,
        "model_id": _quote_identifier(value, "model_id"),
        "provider_instance_id": _quote_identifier(value, "provider_instance_id"),
        "catalog_provider_instance_id": _quote_identifier(
            value, "catalog_provider_instance_id"
        ),
        "catalog_revision": _quote_identifier(value, "catalog_revision"),
        "pricing_revision": _quote_identifier(value, "pricing_revision"),
        "route_binding": value.get("route_binding"),
    }
    binding = quote["route_binding"]
    if not isinstance(binding, Mapping):
        raise StrategyBudgetError("strategy route binding is unavailable")
    binding_value = dict(binding)
    for key in (
        "model_id",
        "provider_instance_id",
        "catalog_provider_instance_id",
        "catalog_revision",
        "pricing_revision",
    ):
        if binding_value.get(key) != value.get(key):
            raise StrategyBudgetError("strategy route binding is invalid")
    if dict(binding_value.get("pricing") or {}) != dict(pricing):
        raise StrategyBudgetError("strategy route binding is invalid")
    quote["route_binding"] = binding_value
    return quote


def _validated_generation(value: Any, quote: Mapping[str, Any]) -> dict[str, Any]:
    _reject_floats(value, label="AI gateway generation")
    if not isinstance(value, Mapping):
        raise ValueError("AI gateway returned an invalid strategy response")
    if value.get("status") not in {None, "ok"}:
        raise ValueError("AI gateway generation failed")
    result = dict(value)
    for key in (
        "model_id",
        "provider_instance_id",
        "catalog_provider_instance_id",
    ):
        if result.get(key) != quote[key]:
            raise ValueError("AI gateway route did not match the owner-bound quote")
    for key in ("catalog_revision", "pricing_revision"):
        if str(result.get(key) or "") != str(quote[key]):
            raise ValueError("AI gateway pricing revision did not match the quote")
    return result


def _validated_quote_state(value: Any) -> dict[str, Any]:
    """Validate the normalized quote stored in a fresh-child continuation."""

    expected = {
        "input",
        "output",
        "currency",
        "unit",
        "model_id",
        "provider_instance_id",
        "catalog_provider_instance_id",
        "catalog_revision",
        "pricing_revision",
        "route_binding",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("AI strategy quote state is invalid")
    return _validated_quote(
        {
            "ready": True,
            "model_id": value["model_id"],
            "provider_instance_id": value["provider_instance_id"],
            "catalog_provider_instance_id": value["catalog_provider_instance_id"],
            "catalog_revision": value["catalog_revision"],
            "pricing_revision": value["pricing_revision"],
            "pricing": {
                "input": value["input"],
                "output": value["output"],
                "currency": value["currency"],
                "unit": value["unit"],
            },
            "route_binding": value["route_binding"],
        }
    )


def _admit(ledger: Mapping[str, Any], estimated: int) -> None:
    if int(ledger["calls"]) >= _MAX_CALLS:
        raise StrategyBudgetError("strategy call budget exhausted")
    reserved = _microusd(ledger["reserved_cost_usd"], "reserved_cost_usd")
    maximum = int(ledger["maximum_cost_microusd"])
    if reserved + estimated > maximum:
        raise StrategyBudgetError("strategy projected cost exceeds budget")


def _record_generation(
    ledger: dict[str, Any],
    phase: str,
    result: Mapping[str, Any],
    estimated: int,
) -> None:
    ledger["calls"] = int(ledger["calls"]) + 1
    cost = _reported_cost(result)
    if cost is None:
        ledger["actual_cost_known"] = False
    else:
        actual = _microusd(ledger["actual_cost_usd"], "actual_cost_usd")
        updated = actual + cost
        if updated > int(ledger["maximum_cost_microusd"]):
            raise StrategyBudgetError("strategy reported cost exceeds budget")
        ledger["actual_cost_usd"] = _usd_text(updated)
    usage = result.get("usage")
    if isinstance(usage, Mapping):
        aggregate = ledger["usage"]
        for key, value in usage.items():
            if type(value) is int and value >= 0:
                aggregate[str(key)] = int(aggregate.get(str(key), 0)) + value
    ledger["phases"].append(
        {
            "phase": phase,
            "cost_usd": _usd_text(cost) if cost is not None else None,
            "reserved_cost_usd": _usd_text(estimated),
        }
    )


def _finish(
    ledger: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    approved: bool,
    deferred: bool,
) -> dict[str, Any]:
    value = dict(result)
    usage = ledger["usage"]
    value["usage"] = {key: int(number) for key, number in usage.items()}
    actual_known = bool(ledger["actual_cost_known"])
    actual_cost = _microusd(ledger["actual_cost_usd"], "actual_cost_usd")
    value["usage_provenance"] = "strategy_aggregate"
    value["usage_cost"] = {
        "cost": _usd_text(actual_cost) if actual_known else None,
        "currency": "USD" if actual_known else None,
        "known": actual_known,
        "usage_provenance": "strategy_aggregate",
    }
    value["strategy_runtime"] = {
        "version": "1",
        "provider": PACK_ID,
        "approved": approved,
        "tool_deferred": deferred,
        "calls": int(ledger["calls"]),
        "maximum_calls": _MAX_CALLS,
        "maximum_cost_microusd": ledger["maximum_cost_microusd"],
        "reserved_cost_usd": ledger["reserved_cost_usd"],
        "used_cost_usd": _usd_text(actual_cost) if actual_known else None,
        "phases": list(ledger["phases"]),
    }
    return value


def _bridge_error(error: Any) -> dict[str, Any]:
    if not isinstance(error, Mapping):
        raise ValueError("AI strategy bridge error is invalid")
    code = error.get("code")
    message = error.get("message")
    if (
        not isinstance(code, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None
        or not isinstance(message, str)
        or not message.strip()
        or len(message) > 512
    ):
        raise ValueError("AI strategy bridge error is invalid")
    return {"status": "error", "error": {"code": code, "message": message}}


def _validate_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) - _REQUEST_FIELDS:
        raise ValueError("AI strategy request fields are invalid")
    _reject_floats(payload, label="AI strategy request")
    value = dict(payload)
    _identifier(value.get("request_id"), "request_id")
    _identifier(value.get("idempotency_key"), "idempotency_key")
    deadline = value.get("deadline")
    if type(deadline) is not int or deadline <= 0:
        raise ValueError("AI strategy deadline is invalid")
    messages = value.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 2048:
        raise ValueError("AI strategy messages are invalid")
    if any(not isinstance(item, Mapping) for item in messages):
        raise ValueError("AI strategy messages are invalid")
    value["messages"] = [dict(item) for item in messages]
    for key in ("model_profile_id", "profile_id", "policy_revision"):
        if value.get(key) is not None:
            _identifier(value[key], key)
    if value.get("model_reference") is not None:
        value["model_reference"] = _model_reference(value["model_reference"])
    if value.get("input") is not None and not isinstance(value["input"], str):
        raise ValueError("AI strategy input is invalid")
    for key in ("parameters", "requirements"):
        if value.get(key) is not None and not isinstance(value[key], Mapping):
            raise ValueError(f"AI strategy {key} is invalid")
        if isinstance(value.get(key), Mapping):
            value[key] = dict(value[key])
    if value.get("tools") is not None:
        if not isinstance(value["tools"], list) or any(
            not isinstance(item, Mapping) for item in value["tools"]
        ):
            raise ValueError("AI strategy tools are invalid")
        value["tools"] = [dict(item) for item in value["tools"]]
    if value.get("allow_failover") is not None and type(value["allow_failover"]) is not bool:
        raise ValueError("AI strategy failover policy is invalid")
    if value.get("system_prompt_digest") is not None and (
        not isinstance(value["system_prompt_digest"], str)
        or _DIGEST_RE.fullmatch(value["system_prompt_digest"]) is None
    ):
        raise ValueError("AI strategy system prompt binding is invalid")
    value["maximum_cost_microusd"] = _maximum_cost_microusd(
        value.get("maximum_cost_microusd")
    )
    _assert_json(value, _MAX_REQUEST_BYTES, "AI strategy request")
    _reject_credentials(value)
    _reject_inline_media(value)
    requirements = value.get("requirements")
    if isinstance(requirements, Mapping) and any(
        key in requirements for key in ("deepthink", "strategy_reference")
    ):
        raise ValueError("AI strategy selection must use the dispatcher")
    return value


def _validate_continuation(value: Any) -> dict[str, Any]:
    expected = {
        "kind",
        "protocol",
        "version",
        "operation_id",
        "nonce",
        "target",
        "request_digest",
        "hop",
        "max_hops",
        "previous_result_digest",
        "state",
        "state_digest",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("AI strategy continuation is invalid")
    target = _target(value.get("target"))
    nonce = value.get("nonce")
    hop = value.get("hop")
    max_hops = value.get("max_hops")
    previous = value.get("previous_result_digest")
    if (
        value.get("kind") != PACKVM_CONTINUATION_KIND
        or value.get("protocol") != PACKVM_BRIDGE_PROTOCOL
        or value.get("version") != PACKVM_BRIDGE_VERSION
        or value.get("operation_id") != EXECUTE_OPERATION_ID
        or not isinstance(nonce, str)
        or re.fullmatch(r"[0-9a-f]{48}", nonce) is None
        or not _is_digest(value.get("request_digest"))
        or type(hop) is not int
        or hop < 0
        or type(max_hops) is not int
        or max_hops != _MAX_HOPS
        or hop >= max_hops
        or (previous is not None and not _is_digest(previous))
        or (hop == 0 and previous is not None)
        or (hop > 0 and previous is None)
        or not _is_digest(value.get("state_digest"))
    ):
        raise ValueError("AI strategy continuation is invalid")
    state = _validate_state(value["state"])
    if _digest(state) != value["state_digest"]:
        raise ValueError("AI strategy continuation state binding is invalid")
    expected_target = (
        _QUOTE_TARGET if state["pending"]["kind"] == "quote" else _GENERATE_TARGET
    )
    if target != expected_target:
        raise ValueError("AI strategy continuation target is invalid")
    return {**dict(value), "target": target, "state": state}


def _validate_bridge_result(
    value: Any, continuation: Mapping[str, Any]
) -> dict[str, Any]:
    expected = {
        "kind",
        "protocol",
        "version",
        "operation_id",
        "nonce",
        "target",
        "request_digest",
        "result",
        "result_digest",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("AI strategy bridge result is invalid")
    if (
        value.get("kind") != PACKVM_BRIDGE_RESULT_KIND
        or value.get("protocol") != PACKVM_BRIDGE_PROTOCOL
        or value.get("version") != PACKVM_BRIDGE_VERSION
        or value.get("operation_id") != EXECUTE_OPERATION_ID
        or value.get("nonce") != continuation["nonce"]
        or _target(value.get("target")) != continuation["target"]
        or value.get("request_digest") != continuation["request_digest"]
        or not isinstance(value.get("result"), Mapping)
        or not _is_digest(value.get("result_digest"))
    ):
        raise ValueError("AI strategy bridge result is invalid")
    _reject_floats(value, label="AI strategy bridge result")
    result = _validated_bridge_outcome(value["result"])
    _assert_json(result, _MAX_BRIDGE_RESULT_BYTES, "AI strategy bridge result")
    if _digest(result) != value["result_digest"]:
        raise ValueError("AI strategy bridge result binding is invalid")
    return {**dict(value), "result": result}


def _validate_state(value: Any) -> dict[str, Any]:
    _reject_floats(value, label="AI strategy state")
    if not isinstance(value, Mapping) or set(value) - {
        "version", "request", "ledger", "pending", "candidate"
    }:
        raise ValueError("AI strategy state is invalid")
    if value.get("version") != 1:
        raise ValueError("AI strategy state is invalid")
    request = _validate_request(_mapping(value.get("request"), "request"))
    ledger = _validate_ledger(value.get("ledger"))
    pending = _validate_pending(value.get("pending"))
    candidate = value.get("candidate")
    if candidate is not None and not isinstance(candidate, Mapping):
        raise ValueError("AI strategy candidate state is invalid")
    result: dict[str, Any] = {
        "version": 1,
        "request": request,
        "ledger": ledger,
        "pending": pending,
    }
    if candidate is not None:
        result["candidate"] = dict(candidate)
    _assert_json(result, _MAX_STATE_BYTES, "AI strategy state")
    return result


def _validate_ledger(value: Any) -> dict[str, Any]:
    expected = {
        "calls",
        "maximum_cost_microusd",
        "reserved_cost_usd",
        "actual_cost_usd",
        "actual_cost_known",
        "usage",
        "phases",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("AI strategy ledger is invalid")
    calls = value.get("calls")
    maximum = value.get("maximum_cost_microusd")
    try:
        reserved = _microusd(value.get("reserved_cost_usd"), "reserved_cost_usd")
        actual = _microusd(value.get("actual_cost_usd"), "actual_cost_usd")
    except ValueError as exc:
        raise ValueError("AI strategy ledger is invalid") from exc
    usage = value.get("usage")
    phases = value.get("phases")
    if (
        type(calls) is not int
        or calls < 0
        or calls > _MAX_CALLS
        or type(maximum) is not int
        or maximum <= 0
        or maximum > _MAXIMUM_COST_MICROUSD
        or type(value.get("actual_cost_known")) is not bool
        or not isinstance(usage, Mapping)
        or not isinstance(phases, list)
        or len(phases) != calls
    ):
        raise ValueError("AI strategy ledger is invalid")
    if reserved > maximum or actual > maximum:
        raise ValueError("AI strategy ledger is invalid")
    normalized_usage: dict[str, int] = {}
    for key, number in usage.items():
        if not isinstance(key, str) or type(number) is not int or number < 0:
            raise ValueError("AI strategy ledger is invalid")
        normalized_usage[key] = number
    normalized_phases = []
    for item in phases:
        if not isinstance(item, Mapping) or set(item) != {
            "phase", "cost_usd", "reserved_cost_usd"
        }:
            raise ValueError("AI strategy ledger is invalid")
        phase = item.get("phase")
        cost = item.get("cost_usd")
        try:
            estimate = _microusd(item.get("reserved_cost_usd"), "reserved_cost_usd")
            parsed_cost = (
                _microusd(cost, "cost_usd") if cost is not None else None
            )
        except ValueError as exc:
            raise ValueError("AI strategy ledger is invalid") from exc
        if phase not in _PHASES:
            raise ValueError("AI strategy ledger is invalid")
        normalized_phases.append(
            {
                "phase": phase,
                "cost_usd": _usd_text(parsed_cost) if parsed_cost is not None else None,
                "reserved_cost_usd": _usd_text(estimate),
            }
        )
    return {
        "calls": calls,
        "maximum_cost_microusd": maximum,
        "reserved_cost_usd": _usd_text(reserved),
        "actual_cost_usd": _usd_text(actual),
        "actual_cost_known": value["actual_cost_known"],
        "usage": normalized_usage,
        "phases": normalized_phases,
    }


def _validate_pending(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("AI strategy pending state is invalid")
    kind = value.get("kind")
    expected = {"phase", "kind", "messages", "tools"}
    if kind == "generate":
        expected |= {"quote", "estimated_cost_usd"}
    if set(value) != expected or value.get("phase") not in _PHASES or kind not in _PENDING_KINDS:
        raise ValueError("AI strategy pending state is invalid")
    messages = value.get("messages")
    tools = value.get("tools")
    if (
        not isinstance(messages, list)
        or not messages
        or any(not isinstance(item, Mapping) for item in messages)
        or not isinstance(tools, list)
        or any(not isinstance(item, Mapping) for item in tools)
    ):
        raise ValueError("AI strategy pending state is invalid")
    result: dict[str, Any] = {
        "phase": value["phase"],
        "kind": kind,
        "messages": [dict(item) for item in messages],
        "tools": [dict(item) for item in tools],
    }
    if kind == "generate":
        result["quote"] = _validated_quote_state(value.get("quote"))
        try:
            estimated = _microusd(
                value.get("estimated_cost_usd"), "estimated_cost_usd"
            )
        except ValueError as exc:
            raise ValueError("AI strategy pending state is invalid") from exc
        if estimated < 0:
            raise ValueError("AI strategy pending state is invalid")
        result["estimated_cost_usd"] = _usd_text(estimated)
    return result


def _validated_bridge_outcome(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("AI strategy bridge outcome is invalid")
    if value.get("status") == "ok" and set(value) == {"status", "value"} and isinstance(value.get("value"), Mapping):
        return {"status": "ok", "value": dict(value["value"])}
    if value.get("status") == "error" and set(value) == {"status", "error"}:
        _bridge_error(value["error"])
        return {"status": "error", "error": dict(value["error"])}
    raise ValueError("AI strategy bridge outcome is invalid")


def _candidate(state: Mapping[str, Any]) -> dict[str, Any]:
    candidate = state.get("candidate")
    if not isinstance(candidate, Mapping):
        raise ValueError("AI strategy candidate is unavailable")
    return dict(candidate)


def _required_text(result: Mapping[str, Any], phase: str) -> str:
    value = result.get("output")
    if not isinstance(value, str) or not value.strip():
        raise StrategyBudgetError(f"strategy {phase} returned no text")
    return value.strip()


def _review(result: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(_required_text(result, "review"))
    except (TypeError, ValueError) as exc:
        raise StrategyBudgetError("strategy reviewer returned invalid JSON") from exc
    if (
        not isinstance(value, Mapping)
        or set(value) != {"approved", "feedback"}
        or type(value.get("approved")) is not bool
        or not isinstance(value.get("feedback"), str)
    ):
        raise StrategyBudgetError("strategy reviewer response is invalid")
    return {"approved": value["approved"], "feedback": value["feedback"]}


def _bounded_parameters(value: Any) -> dict[str, Any]:
    parameters = dict(value) if isinstance(value, Mapping) else {}
    configured = parameters.get("max_tokens")
    parameters["max_tokens"] = (
        max(1, min(configured, _MAX_OUTPUT_TOKENS))
        if type(configured) is int
        else _MAX_OUTPUT_TOKENS
    )
    return parameters


def _estimate_call_cost(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
    quote: Mapping[str, Any],
) -> int:
    encoded = _canonical_json({"messages": messages, "tools": tools}).encode("utf-8")
    return (
        _rate_cost_microusd(quote["input"], len(encoded), "pricing input")
        + _rate_cost_microusd(
            quote["output"], max_tokens, "pricing output"
        )
    )


def _with_instruction(messages: list[dict[str, Any]], instruction: str) -> list[dict[str, Any]]:
    return [{"role": "system", "content": f"Strategy phase instruction: {instruction}"}, *[dict(message) for message in messages]]


def _maximum_cost_microusd(value: Any) -> int:
    """Validate the integer budget representation used across PackVM frames."""

    if type(value) is not int or value <= 0 or value > _MAXIMUM_COST_MICROUSD:
        raise ValueError("AI strategy maximum_cost_microusd is invalid")
    return value


def _reported_cost(result: Mapping[str, Any]) -> int | None:
    usage_cost = result.get("usage_cost")
    if not isinstance(usage_cost, Mapping) or usage_cost.get("known") is False:
        return None
    try:
        return _microusd(usage_cost.get("cost"), "usage cost")
    except ValueError:
        return None


def _quote_identifier(value: Mapping[str, Any], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate or len(candidate) > 512:
        raise StrategyBudgetError("strategy route quote is invalid")
    return candidate


def _model_reference(value: Any) -> str | dict[str, str]:
    if isinstance(value, str):
        return _identifier(value, "model_reference")
    if isinstance(value, Mapping) and set(value) == {"profile_id"}:
        return {"profile_id": _identifier(value.get("profile_id"), "model_reference")}
    raise ValueError("AI strategy model_reference is invalid")


def _identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"AI strategy {field} is invalid")
    return value


def _target(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"contract_id"}:
        raise ValueError("AI strategy bridge target is invalid")
    contract_id = value.get("contract_id")
    if contract_id not in {QUOTE_CONTRACT_ID, GENERATE_CONTRACT_ID}:
        raise ValueError("AI strategy bridge target is invalid")
    return {"contract_id": contract_id}


def _microusd(value: Any, field: str) -> int:
    """Convert a nonnegative canonical decimal dollar value to micro-USD.

    PackVM canonical JSON does not permit floating point.  Rates and reported
    cost therefore cross the bridge as decimal strings and are rounded upward
    here before budget admission so a low-precision provider cannot understate
    an admitted call.
    """

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"AI strategy {field} is invalid")
    if isinstance(value, str) and (
        not value
        or value != value.strip()
        or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is None
    ):
        raise ValueError(f"AI strategy {field} is invalid")
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"AI strategy {field} is invalid") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError(f"AI strategy {field} is invalid")
    micros = (decimal * _USD_MICROS).to_integral_value(rounding=ROUND_CEILING)
    if micros > 2**53 - 1:
        raise ValueError(f"AI strategy {field} is invalid")
    return int(micros)


def _decimal_usd(value: Any, field: str) -> str:
    """Validate an exact canonical USD decimal without reducing precision."""

    if (
        not isinstance(value, str)
        or len(value) > 128
        or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is None
    ):
        raise ValueError(f"AI strategy {field} is invalid")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"AI strategy {field} is invalid") from exc
    if not decimal.is_finite() or decimal < 0:
        raise ValueError(f"AI strategy {field} is invalid")
    return value


def _rate_cost_microusd(rate: Any, units: int, field: str) -> int:
    """Reserve the exact `usd_per_token` rate times token count, rounded up."""

    if type(units) is not int or units < 0:
        raise ValueError("AI strategy token count is invalid")
    decimal_rate = Decimal(_decimal_usd(rate, field))
    micros = (decimal_rate * units * _USD_MICROS).to_integral_value(
        rounding=ROUND_CEILING
    )
    if micros > 2**53 - 1:
        raise StrategyBudgetError("strategy projected cost exceeds budget")
    return int(micros)


def _usd_text(microusd: int) -> str:
    """Render exact micro-USD as a canonical non-exponent decimal string."""

    if type(microusd) is not int or microusd < 0:
        raise ValueError("AI strategy cost is invalid")
    whole, fraction = divmod(microusd, _USD_MICROS)
    if fraction == 0:
        return str(whole)
    return f"{whole}.{fraction:06d}".rstrip("0")


def _reject_floats(value: Any, *, label: str, depth: int = 0) -> None:
    """Reject noncanonical numeric values at every PackVM trust boundary."""

    if depth > 16:
        raise ValueError(f"{label} nesting exceeds the limit")
    if type(value) is float:
        raise ValueError(f"{label} must not contain floating point")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_floats(item, label=label, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_floats(item, label=label, depth=depth + 1)


def _phase_identifier(root: str, phase: str) -> str:
    return f"strategy:{hashlib.sha256(root.encode('utf-8')).hexdigest()[:32]}:{phase}"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("AI strategy values must be JSON") from exc


def _assert_json(value: Any, maximum: int, label: str) -> None:
    if len(_canonical_json(value).encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds the size limit")


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"AI strategy {label} is invalid")
    return value


def _reject_credentials(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("AI strategy request nesting exceeds the limit")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_FIELDS:
                raise ValueError("AI strategy does not accept credentials")
            _reject_credentials(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item, depth=depth + 1)


def _reject_inline_media(value: Any, *, depth: int = 0) -> None:
    """Keep large media owned by the Host instead of continuation state."""

    if depth > 16:
        raise ValueError("AI strategy request nesting exceeds the limit")
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"image_base64", "inline_data", "data_url"}:
                raise ValueError("AI strategy requires an owner media reference")
            if normalized == "url" and isinstance(item, str) and item.startswith("data:"):
                raise ValueError("AI strategy requires an owner media reference")
            _reject_inline_media(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_inline_media(item, depth=depth + 1)


__all__ = [
    "EXECUTE_CONTRACT_ID",
    "EXECUTE_OPERATION_ID",
    "GENERATE_CONTRACT_ID",
    "PACKVM_BRIDGE_PROTOCOL",
    "QUOTE_CONTRACT_ID",
    "StrategyBudgetError",
    "tobkiri_packvm_invoke",
]
