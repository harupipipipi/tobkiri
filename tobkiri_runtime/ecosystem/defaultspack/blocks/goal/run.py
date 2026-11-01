"""/goal slash command implementation.

Drives a goal-pursuit loop with two roles:

* a *Worker* agent that produces the next concrete contribution toward the goal;
* a third-party *Evaluator* agent that, after each Worker turn, decides whether
  the goal has been achieved and otherwise emits the next instruction.

Default /goal runs with a bounded iteration cap. ``/goal /rich ...`` (or
``rich=true`` / ``max_iterations=rich``) switches to rich mode: the loop keeps
going until completion under a deliberately high emergency budget of 200
iterations / 30 minutes. Cancellation, deadline, and model errors stop it early.

The block is invoked via the ``pack_block`` slash command execution type and
runs model-only Worker/Evaluator turns; it does not execute tools directly.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from blocks._common import error, ok
from domain.ai_client.model_call import call_model

DEFAULT_MAX_ITERATIONS = 5
HARD_MAX_ITERATIONS = 20
RICH_EMERGENCY_MAX_ITERATIONS = 200
RICH_DEFAULT_DEADLINE_SECONDS = 30 * 60
RICH_ITERATION_SENTINELS = {"rich", "unlimited", "infinite", "forever", "none", "∞"}
_RICH_COMMAND_RE = re.compile(r"^\s*/rich(?=\s|$)", re.IGNORECASE)

WORKER_SYSTEM_PROMPT = (
    "You are a Worker agent pursuing a user-defined goal. "
    "Each turn, produce the next concrete contribution toward the goal. "
    "Be direct and produce actionable output, not meta-commentary."
)

EVALUATOR_SYSTEM_PROMPT = (
    "You are an independent third-party Evaluator. "
    "Decide whether the Worker has achieved the stated goal yet. "
    "Reply with strict JSON only matching the schema: "
    '{"achieved": bool, "reason": string, "next_instruction": string}. '
    "If achieved is true, next_instruction may be empty. "
    "If achieved is false, next_instruction must tell the Worker exactly what "
    "to do next."
)

EVALUATOR_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "achieved": {"type": "boolean"},
        "reason": {"type": "string"},
        "next_instruction": {"type": "string"},
    },
    "required": ["achieved"],
}


def run(input_data: Any = None, context: Any = None) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    raw_goal = str(data.get("goal") or "").strip()
    rich_mode = _rich_requested(data, raw_goal)
    goal = _strip_rich_command(raw_goal) if rich_mode else raw_goal
    if not goal:
        return error("goal is required", "MISSING_PARAM")

    max_iterations = None if rich_mode else _coerce_iterations(data.get("max_iterations"))
    rich_deadline_seconds = (
        _coerce_rich_deadline(data.get("rich_timeout_seconds") or data.get("timeout_seconds"))
        if rich_mode
        else None
    )
    deadline = time.monotonic() + rich_deadline_seconds if rich_deadline_seconds else None
    worker_model = str(data.get("worker_model") or data.get("model") or "").strip()
    evaluator_model = str(data.get("evaluator_model") or data.get("model") or "").strip()

    call_handler = None
    if isinstance(context, dict):
        call_handler = context.get("call_handler")

    iterations: list[dict[str, Any]] = []
    next_instruction = goal
    final_output = ""
    achieved = False
    reason = ""
    index = 0
    stopped_reason = ""

    iteration_budget = RICH_EMERGENCY_MAX_ITERATIONS if rich_mode else int(max_iterations or 0)
    while index < iteration_budget:
        stopped_reason = _stop_signal(context, deadline)
        if stopped_reason:
            break
        index += 1
        worker_response = _call_worker(
            goal,
            iterations,
            next_instruction,
            worker_model,
            call_handler,
        )
        if not _is_ok_response(worker_response):
            iterations.append(
                {
                    "iteration": index,
                    "phase": "worker_error",
                    "error": _response_error_payload(worker_response),
                }
            )
            return _failure_with_iterations(
                "worker model call failed",
                "WORKER_FAILED",
                iterations,
                goal=goal,
                rich_mode=rich_mode,
                max_iterations=max_iterations,
            )

        worker_output = _response_text(worker_response)
        final_output = worker_output

        stopped_reason = _stop_signal(context, deadline)
        if stopped_reason:
            iterations.append(
                {
                    "iteration": index,
                    "worker_output": worker_output,
                    "phase": stopped_reason,
                }
            )
            break

        evaluator_response = _call_evaluator(
            goal,
            worker_output,
            evaluator_model,
            call_handler,
        )
        if not _is_ok_response(evaluator_response):
            iterations.append(
                {
                    "iteration": index,
                    "worker_output": worker_output,
                    "phase": "evaluator_error",
                    "error": _response_error_payload(evaluator_response),
                }
            )
            return _failure_with_iterations(
                "evaluator model call failed",
                "EVALUATOR_FAILED",
                iterations,
                goal=goal,
                rich_mode=rich_mode,
                max_iterations=max_iterations,
            )

        verdict = _normalize_verdict(evaluator_response.get("output"))
        achieved = verdict["achieved"]
        reason = verdict["reason"]
        next_instruction = verdict["next_instruction"]

        iterations.append(
            {
                "iteration": index,
                "worker_output": worker_output,
                "verdict": verdict,
            }
        )

        if achieved:
            stopped_reason = "achieved"
            break
        if not next_instruction:
            next_instruction = (
                "Continue working toward the goal. Produce more concrete progress."
            )

    if not stopped_reason:
        stopped_reason = "emergency_iteration_cap_reached" if rich_mode else "max_iterations_reached"

    return ok(
        {
            "goal": goal,
            "achieved": achieved,
            "reason": reason,
            "iterations": iterations,
            "iteration_count": len(iterations),
            "final_output": final_output,
            "max_iterations": max_iterations,
            "rich": rich_mode,
            "mode": "rich" if rich_mode else "bounded",
            "hard_cap": RICH_EMERGENCY_MAX_ITERATIONS if rich_mode else HARD_MAX_ITERATIONS,
            "deadline_seconds": rich_deadline_seconds,
            "stopped_reason": stopped_reason,
        }
    )


def _coerce_iterations(value: Any) -> int:
    try:
        parsed = int(value) if value not in (None, "") else DEFAULT_MAX_ITERATIONS
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_ITERATIONS
    return max(1, min(parsed, HARD_MAX_ITERATIONS))


def _coerce_rich_deadline(value: Any) -> float:
    try:
        parsed = float(value) if value not in (None, "") else RICH_DEFAULT_DEADLINE_SECONDS
    except (TypeError, ValueError):
        parsed = RICH_DEFAULT_DEADLINE_SECONDS
    return max(1.0, min(parsed, 24 * 60 * 60))


def _is_cancelled(context: Any) -> bool:
    checker = context.get("is_cancelled") if isinstance(context, dict) else None
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def _stop_signal(context: Any, deadline: float | None) -> str:
    if _is_cancelled(context):
        return "cancelled"
    if deadline is not None and time.monotonic() >= deadline:
        return "deadline_reached"
    return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled", "rich"}


def _rich_requested(data: dict[str, Any], goal: str) -> bool:
    if _truthy(data.get("rich")):
        return True
    if str(data.get("mode") or "").strip().lower() == "rich":
        return True
    max_iterations = str(data.get("max_iterations") or "").strip().lower()
    if max_iterations in RICH_ITERATION_SENTINELS:
        return True
    return bool(_RICH_COMMAND_RE.search(str(goal or "")))


def _strip_rich_command(goal: str) -> str:
    stripped = _RICH_COMMAND_RE.sub(" ", str(goal or "")).strip()
    return re.sub(r"\s+", " ", stripped)


def _call_worker(
    goal: str,
    iterations: list[dict[str, Any]],
    instruction: str,
    model: str,
    call_handler: Any,
) -> dict[str, Any]:
    history = _format_history(iterations)
    prompt = (
        f"Goal: {goal}\n\n"
        f"History so far:\n{history if history else '(no prior iterations)'}\n\n"
        f"Next instruction from the Evaluator:\n{instruction}\n\n"
        "Produce your next contribution toward the goal."
    )
    return call_model(
        {
            "model_hint": model,
            "messages": [
                {"role": "system", "content": WORKER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 800,
            "thinking_level": "none",
        },
        {"_model_call_depth": 0},
        call_handler=call_handler,
    )


def _call_evaluator(
    goal: str,
    worker_output: str,
    model: str,
    call_handler: Any,
) -> dict[str, Any]:
    prompt = (
        f"Goal: {goal}\n\n"
        f"Worker's latest output:\n{worker_output or '(empty)'}\n\n"
        "Decide whether the goal has been achieved. Reply with strict JSON only."
    )
    return call_model(
        {
            "model_hint": model,
            "messages": [
                {"role": "system", "content": EVALUATOR_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 400,
            "thinking_level": "none",
            "output_schema": EVALUATOR_OUTPUT_SCHEMA,
        },
        {"_model_call_depth": 0},
        call_handler=call_handler,
    )


def _format_history(iterations: list[dict[str, Any]]) -> str:
    if not iterations:
        return ""
    lines: list[str] = []
    for entry in iterations:
        idx = entry.get("iteration")
        worker_output = str(entry.get("worker_output") or "").strip()
        verdict = entry.get("verdict") if isinstance(entry.get("verdict"), dict) else {}
        verdict_reason = str(verdict.get("reason") or "")
        lines.append(f"--- Iteration {idx} ---")
        if worker_output:
            lines.append(f"Worker: {worker_output[:1000]}")
        if verdict_reason:
            lines.append(f"Evaluator reason: {verdict_reason}")
    return "\n".join(lines)


def _parse_verdict(output: Any) -> dict[str, Any]:
    if isinstance(output, dict):
        return output
    text = ""
    if isinstance(output, str):
        text = output.strip()
    elif output is not None:
        text = str(output).strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {
        "achieved": False,
        "reason": "could not parse evaluator verdict",
        "next_instruction": "Continue working toward the goal.",
    }


def _normalize_verdict(output: Any) -> dict[str, Any]:
    verdict = _parse_verdict(output)
    if not isinstance(verdict.get("achieved"), bool):
        return {
            "achieved": False,
            "reason": "evaluator verdict missing boolean achieved",
            "next_instruction": "Continue working toward the goal with concrete progress.",
        }

    achieved = verdict["achieved"]
    reason = str(verdict.get("reason") or "")
    next_instruction = str(verdict.get("next_instruction") or "").strip()
    if not achieved and not next_instruction:
        next_instruction = "Continue working toward the goal with concrete progress."

    return {
        "achieved": achieved,
        "reason": reason,
        "next_instruction": next_instruction,
    }


def _is_ok_response(response: Any) -> bool:
    return isinstance(response, dict) and response.get("status") == "ok"


def _failure_with_iterations(
    message: str,
    code: str,
    iterations: list[dict[str, Any]],
    *,
    goal: str = "",
    rich_mode: bool = False,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Return an error envelope that also surfaces partial loop progress."""
    payload = error(message, code)
    payload["error"]["iterations"] = iterations
    payload["error"]["goal"] = goal
    payload["error"]["rich"] = rich_mode
    payload["error"]["mode"] = "rich" if rich_mode else "bounded"
    payload["error"]["max_iterations"] = max_iterations
    return payload


def _response_error_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return {
            "code": response.get("code"),
            "error": response.get("error"),
        }
    return {"error": str(response)}


def _response_text(response: dict[str, Any]) -> str:
    output = response.get("output")
    if isinstance(output, str):
        return output.strip()
    if isinstance(output, dict):
        # call_model returns dicts when output_schema is set; the worker call
        # never sets one, but be defensive in case the gateway returns JSON.
        try:
            return json.dumps(output, ensure_ascii=False).strip()
        except (TypeError, ValueError):
            return str(output).strip()
    if output is None:
        return ""
    return str(output).strip()
