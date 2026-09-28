"""Bounded DeepThink orchestration for the Pack v4 AI Gateway."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


_MAX_CALLS = 5
_MAX_OUTPUT_TOKENS = 1024
_DEFAULT_MAX_COST_USD = 0.05


class DeepThinkBudgetError(RuntimeError):
    """Raised when the Host-owned DeepThink budget cannot admit another call."""


@dataclass
class _Ledger:
    deadline: float
    maximum_cost_usd: float
    calls: int = 0
    cost_usd: float = 0.0
    budgeted_cost_usd: float = 0.0
    cost_known: bool = True
    usage: dict[str, float] = field(default_factory=dict)
    phases: list[dict[str, Any]] = field(default_factory=list)

    def admit(self, estimated_cost_usd: float | None) -> None:
        if self.calls >= _MAX_CALLS:
            raise DeepThinkBudgetError("DeepThink call budget exhausted")
        if time.time() >= self.deadline:
            raise DeepThinkBudgetError("DeepThink deadline elapsed")
        if estimated_cost_usd is None:
            raise DeepThinkBudgetError("DeepThink pricing is unavailable")
        if self.budgeted_cost_usd >= self.maximum_cost_usd:
            raise DeepThinkBudgetError("DeepThink cost budget exhausted")
        if (
            estimated_cost_usd is not None
            and self.budgeted_cost_usd + estimated_cost_usd > self.maximum_cost_usd
        ):
            raise DeepThinkBudgetError("DeepThink projected cost exceeds budget")

    def record(
        self,
        phase: str,
        result: Mapping[str, Any],
        estimated_cost_usd: float | None,
    ) -> None:
        self.calls += 1
        phase_cost: float | None = None
        usage = result.get("usage")
        if isinstance(usage, Mapping):
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    number = float(value)
                    if math.isfinite(number) and number >= 0:
                        self.usage[str(key)] = self.usage.get(str(key), 0.0) + number
        usage_cost = result.get("usage_cost")
        cost = usage_cost.get("cost") if isinstance(usage_cost, Mapping) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            number = float(cost)
            if math.isfinite(number) and number >= 0:
                self.cost_usd += number
                phase_cost = number
                self.budgeted_cost_usd += number
            else:
                self.cost_known = False
        else:
            self.cost_known = False
            self.budgeted_cost_usd += estimated_cost_usd
        self.phases.append(
            {
                "phase": phase,
                "cost_usd": phase_cost,
            }
        )
        if self.budgeted_cost_usd > self.maximum_cost_usd:
            raise DeepThinkBudgetError("DeepThink cost budget exceeded")


def run_deepthink(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    parameters: Mapping[str, Any],
    deadline: float,
    maximum_cost_usd: float | None,
    input_cost_per_token: float | None,
    output_cost_per_token: float | None,
    complete: Callable[
        [str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]],
        dict[str, Any],
    ],
) -> dict[str, Any]:
    """Run a plan, draft, and review loop through a captured provider edge.

    Every model call is delegated to ``complete`` so the caller retains the
    selected provider principal, credential handle, usage normalizer, and tool
    intent bridge. The loop is hard-capped at five calls and 1,024 output
    tokens per call.
    """

    limit = _DEFAULT_MAX_COST_USD
    if (
        isinstance(maximum_cost_usd, (int, float))
        and not isinstance(maximum_cost_usd, bool)
        and math.isfinite(float(maximum_cost_usd))
        and float(maximum_cost_usd) > 0
    ):
        limit = min(limit, float(maximum_cost_usd))
    ledger = _Ledger(deadline=deadline, maximum_cost_usd=limit)
    bounded_parameters = dict(parameters)
    configured_tokens = bounded_parameters.get("max_tokens")
    if isinstance(configured_tokens, int) and not isinstance(configured_tokens, bool):
        bounded_parameters["max_tokens"] = max(
            1, min(configured_tokens, _MAX_OUTPUT_TOKENS)
        )
    else:
        bounded_parameters["max_tokens"] = _MAX_OUTPUT_TOKENS

    def call(
        phase: str,
        phase_messages: list[dict[str, Any]],
        phase_tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        estimated_cost = _estimated_call_cost(
            phase_messages,
            phase_tools,
            bounded_parameters["max_tokens"],
            input_cost_per_token,
            output_cost_per_token,
        )
        ledger.admit(estimated_cost)
        result = complete(
            phase,
            phase_messages,
            phase_tools,
            bounded_parameters,
        )
        ledger.record(phase, result, estimated_cost)
        return result

    plan_result = call(
        "plan",
        _with_instruction(
            messages,
            "Create a concise plan for answering the request. Identify the "
            "main claims, missing assumptions, and risks. Return plain text.",
        ),
        [],
    )
    plan = _required_text(plan_result, "plan")
    draft_result = call(
        "draft",
        _with_instruction(
            messages,
            "Use the following private planning artifact to produce the best "
            f"answer or required tool call. Do not mention the plan.\n\n{plan}",
        ),
        tools,
    )
    if draft_result.get("tool_intents"):
        return _finish(draft_result, ledger, approved=False, deferred=True)
    draft = _required_text(draft_result, "draft")
    review = _review(
        call(
            "review",
            _with_instruction(
                messages,
                "Review the candidate answer for correctness, completeness, "
                "and alignment with the request. Return only JSON shaped as "
                '{"approved": boolean, "feedback": string}.\n\n'
                f"Candidate answer:\n{draft}",
            ),
            [],
        )
    )
    if review["approved"]:
        return _finish(draft_result, ledger, approved=True, deferred=False)

    revision_result = call(
        "revision",
        _with_instruction(
            messages,
            "Rewrite the candidate answer using the review feedback. Return "
            "only the improved answer or a required tool call.\n\n"
            f"Candidate answer:\n{draft}\n\nFeedback:\n{review['feedback']}",
        ),
        tools,
    )
    if revision_result.get("tool_intents"):
        return _finish(revision_result, ledger, approved=False, deferred=True)
    revision = _required_text(revision_result, "revision")
    final_review = _review(
        call(
            "final_review",
            _with_instruction(
                messages,
                "Review the revised answer. Return only JSON shaped as "
                '{"approved": boolean, "feedback": string}.\n\n'
                f"Revised answer:\n{revision}",
            ),
            [],
        )
    )
    if not final_review["approved"]:
        raise DeepThinkBudgetError("DeepThink reviewer rejected the bounded revision")
    return _finish(revision_result, ledger, approved=True, deferred=False)


def _with_instruction(
    messages: list[dict[str, Any]], instruction: str
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": f"DeepThink phase instruction: {instruction}"},
        *[dict(message) for message in messages],
    ]


def _estimated_call_cost(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_output_tokens: int,
    input_cost_per_token: float | None,
    output_cost_per_token: float | None,
) -> float | None:
    """Return a conservative catalog-price estimate for admission control."""

    rates = (input_cost_per_token, output_cost_per_token)
    if any(
        not isinstance(rate, (int, float))
        or isinstance(rate, bool)
        or not math.isfinite(float(rate))
        or float(rate) < 0
        for rate in rates
    ):
        return None
    encoded = json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # One UTF-8 byte per token is deliberately conservative for admission.
    return len(encoded) * float(input_cost_per_token) + max_output_tokens * float(
        output_cost_per_token
    )


def _required_text(result: Mapping[str, Any], phase: str) -> str:
    value = result.get("output")
    if not isinstance(value, str) or not value.strip():
        raise DeepThinkBudgetError(f"DeepThink {phase} returned no text")
    return value.strip()


def _review(result: Mapping[str, Any]) -> dict[str, Any]:
    text = _required_text(result, "review")
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise DeepThinkBudgetError("DeepThink reviewer returned invalid JSON") from exc
    if (
        not isinstance(value, Mapping)
        or type(value.get("approved")) is not bool
        or not isinstance(value.get("feedback"), str)
    ):
        raise DeepThinkBudgetError("DeepThink reviewer response is invalid")
    return {"approved": value["approved"], "feedback": value["feedback"]}


def _finish(
    result: Mapping[str, Any],
    ledger: _Ledger,
    *,
    approved: bool,
    deferred: bool,
) -> dict[str, Any]:
    value = dict(result)
    value["usage"] = {
        key: int(number) if number.is_integer() else number
        for key, number in ledger.usage.items()
    }
    value["usage_provenance"] = "gateway_deepthink_aggregate"
    value["usage_cost"] = {
        "cost": ledger.cost_usd if ledger.cost_known else None,
        "currency": "USD" if ledger.cost_known else None,
        "known": ledger.cost_known,
        "usage_provenance": "gateway_deepthink_aggregate",
    }
    value["deepthink_runtime"] = {
        "version": "gateway-v1",
        "approved": approved,
        "tool_deferred": deferred,
        "calls": ledger.calls,
        "maximum_calls": _MAX_CALLS,
        "maximum_cost_usd": ledger.maximum_cost_usd,
        "used_cost_usd": ledger.cost_usd if ledger.cost_known else None,
        "phases": list(ledger.phases),
    }
    return value
