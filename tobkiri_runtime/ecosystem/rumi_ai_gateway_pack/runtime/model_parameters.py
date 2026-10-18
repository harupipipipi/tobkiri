"""Apply a saved model's output ceiling before routing or credential access."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError


def bounded_model_parameters(
    saved: Mapping[str, Any], requested: Mapping[str, Any]
) -> dict[str, Any]:
    """Allow narrower caller limits while retaining the registry-owned ceiling."""
    parameters = {**dict(saved), **dict(requested)}
    ceilings = []
    if "max_tokens" in saved:
        cap = saved["max_tokens"]
        if type(cap) is not int or not 1 <= cap <= 131072:
            raise GlobalContractInvocationError(
                "unresolved_profile", "saved model output limit is invalid"
            )
        ceilings.append(cap)
    for key in ("n", "best_of"):
        if key in parameters and (type(parameters[key]) is not int or parameters[key] != 1):
            raise GlobalContractInvocationError(
                "invalid_request", "multiple model completions are not supported"
            )
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if key in requested:
            value = requested[key]
            if type(value) is not int or value <= 0:
                raise GlobalContractInvocationError(
                    "invalid_request", "model output limit is invalid"
                )
            ceilings.append(value)
        parameters.pop(key, None)
    if ceilings:
        parameters["max_tokens"] = min(ceilings)
    return parameters
