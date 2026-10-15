"""Finite input and successful dependency-output references for Workflow v4."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from .models import WorkflowValidationError

_INPUT = re.compile(r"^\$\{inputs\.([a-z][a-z0-9_.-]*)\}$")
_OUTPUT = re.compile(r"^\$\{steps\.([a-z][a-z0-9_.-]*?)\.output(?:\.([a-z][a-z0-9_.-]*))?\}$")


def references(value: Any) -> tuple[tuple[str, str, str], ...]:
    """Parse whole-value references; expression evaluation is never supported."""
    if isinstance(value, str):
        matched = _INPUT.fullmatch(value)
        if matched:
            return (("inputs", "", matched[1]),)
        matched = _OUTPUT.fullmatch(value)
        if matched:
            return (("steps", matched[1], matched[2] or ""),)
        if "${" in value:
            raise WorkflowValidationError("workflow template expression is invalid")
    if isinstance(value, Mapping):
        return tuple(item for child in value.values() for item in references(child))
    if isinstance(value, list):
        return tuple(item for child in value for item in references(child))
    return ()


def resolve(
    value: Any,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Mapping[str, Any]],
) -> Any:
    """Resolve only materialized inputs and same-run sealed dependency outcomes."""
    if isinstance(value, str):
        parsed = references(value)
        if not parsed:
            return value
        source, step_id, path = parsed[0]
        current: Any = inputs if source == "inputs" else outputs.get(step_id)
        if current is None:
            raise WorkflowValidationError("workflow dependency output is unavailable")
        for part in path.split(".") if path else ():
            if not isinstance(current, Mapping) or part not in current:
                raise WorkflowValidationError("workflow input template is unresolved")
            current = current[part]
        return deepcopy(current)
    if isinstance(value, Mapping):
        return {key: resolve(item, inputs, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, inputs, outputs) for item in value]
    return value
