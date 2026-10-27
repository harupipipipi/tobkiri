"""Public counters from a typed Host refusal before PackVM execution."""

from __future__ import annotations

from typing import Mapping

from tobkiri_host.errors import SavedTurnRejectedError


PACKVM_HOST_CAPACITY_INSUFFICIENT = "PACKVM_HOST_CAPACITY_INSUFFICIENT"
_MAX_PUBLIC_CAPACITY_BYTES = (1 << 53) - 1


def is_saved_turn_capacity_refusal(error: BaseException) -> bool:
    """Recognize the Host's definitive allocation refusal, never a code alone."""

    return (
        isinstance(error, SavedTurnRejectedError)
        and error.saved_terminal_error_code == PACKVM_HOST_CAPACITY_INSUFFICIENT
    )


def bounded_capacity_details(value: object) -> dict[str, int]:
    """Revalidate the finite counter pair before public output or journal replay."""

    if not isinstance(value, Mapping) or set(value) != {
        "required_bytes",
        "available_bytes",
    }:
        return {}
    required = value["required_bytes"]
    available = value["available_bytes"]
    if (
        type(required) is not int
        or type(available) is not int
        or not 0 <= available < required <= _MAX_PUBLIC_CAPACITY_BYTES
    ):
        return {}
    return {"required_bytes": required, "available_bytes": available}


def saved_turn_capacity_details(error: BaseException) -> dict[str, int]:
    """Read only validated owner counters from the same bounded cause chain."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if is_saved_turn_capacity_refusal(current):
            return bounded_capacity_details(getattr(current, "saved_terminal_details"))
        current = current.__cause__ or current.__context__
    return {}
