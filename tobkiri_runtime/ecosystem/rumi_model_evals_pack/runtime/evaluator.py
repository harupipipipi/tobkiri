"""Plan approved evaluation work and score supplied observations locally."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

CATALOG_HASH = "sha256:e4cf593b4abb244b809e652f9272dea7f30439d5e65b1b887d0ca8b29d4c7efb"
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "catalog" / "provider_eval_catalog.json"


def create_catalog_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the immutable evaluation catalog resource."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        del payload
        if name not in {"get", "list"}:
            raise ValueError(f"unknown evaluation catalog operation: {name}")
        return {"catalog": _catalog(), "content_hash": CATALOG_HASH}

    return operation


def create_plan_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create non-executing evaluation operation descriptors."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"plan", "prepare"}:
            raise ValueError(f"unknown evaluation plan operation: {name}")
        suite_id = _identifier(payload.get("suite_id"), "suite_id")
        targets = payload.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError("evaluation targets are required")
        attempts = max(1, int(payload.get("attempts") or 1))
        operations = []
        for target in targets:
            if not isinstance(target, Mapping):
                raise ValueError("evaluation target is invalid")
            target_id = _identifier(target.get("target_id"), "target_id")
            for attempt in range(1, attempts + 1):
                operations.append(
                    {
                        "operation_id": f"{suite_id}:{target_id}:{attempt}",
                        "kind": "ai.eval.sample",
                        "target_id": target_id,
                        "model_profile_id": target.get("model_profile_id"),
                        "fixture_id": target.get("fixture_id"),
                        "attempt": attempt,
                        "approval_required": True,
                        "authority_granted": False,
                        "network": "unknown_until_selected_provider",
                    }
                )
        return {
            "suite_id": suite_id,
            "operations": operations,
            "executes": False,
            "approval_required": True,
        }

    return operation


def create_score_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create deterministic scoring over externally supplied observations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"score", "summarize"}:
            raise ValueError(f"unknown evaluation scoring operation: {name}")
        observations = payload.get("observations")
        if not isinstance(observations, list) or not observations:
            raise ValueError("evaluation observations are required")
        statuses = []
        costs = []
        latencies = []
        for item in observations:
            if not isinstance(item, Mapping):
                raise ValueError("evaluation observation is invalid")
            status = str(item.get("status") or "unknown")
            if status not in {"passed", "failed", "unknown"}:
                raise ValueError("evaluation observation status is invalid")
            statuses.append(status)
            cost = _number(item.get("cost"))
            latency = _number(item.get("latency_ms"))
            if cost is not None:
                costs.append(cost)
            if latency is not None:
                latencies.append(latency)
        known = [item for item in statuses if item != "unknown"]
        passed = statuses.count("passed")
        transitions = sum(
            1
            for previous, current in zip(known, known[1:])
            if previous != current
        )
        thresholds = payload.get("thresholds")
        thresholds = thresholds if isinstance(thresholds, Mapping) else {}
        minimum_samples = max(1, int(thresholds.get("minimum_samples") or 1))
        minimum_pass_rate = _number(thresholds.get("minimum_pass_rate"))
        pass_rate = passed / len(known) if known else None
        promotable = (
            len(known) >= minimum_samples
            and len(known) == len(statuses)
            and minimum_pass_rate is not None
            and pass_rate is not None
            and pass_rate >= minimum_pass_rate
        )
        return {
            "sample_count": len(statuses),
            "known_count": len(known),
            "unknown_count": statuses.count("unknown"),
            "passed": passed,
            "failed": statuses.count("failed"),
            "pass_rate": pass_rate,
            "flakiness_transition_rate": (
                transitions / (len(known) - 1) if len(known) > 1 else None
            ),
            "cost": _summary(costs, len(statuses)),
            "latency_ms": _summary(latencies, len(statuses)),
            "promotion": {
                "eligible": promotable,
                "decision": "promote" if promotable else "hold",
                "complete_evidence": len(known) == len(statuses),
            },
        }

    return operation


def _catalog() -> dict[str, Any]:
    raw = _CATALOG_PATH.read_bytes()
    if "sha256:" + hashlib.sha256(raw).hexdigest() != CATALOG_HASH:
        raise RuntimeError("evaluation catalog integrity mismatch")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evaluation catalog is invalid")
    return value


def _summary(values: list[float], expected: int) -> dict[str, Any]:
    if len(values) != expected:
        return {"known": False, "mean": None, "maximum": None}
    return {
        "known": True,
        "mean": sum(values) / len(values),
        "maximum": max(values),
    }


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _identifier(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > 200 or any(
        item in result for item in ("\x00", "\r", "\n")
    ):
        raise ValueError(f"{label} is invalid")
    return result
