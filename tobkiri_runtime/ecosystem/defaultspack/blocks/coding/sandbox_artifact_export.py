"""Finite compatibility response for the retired sandbox export path."""

from __future__ import annotations

from typing import Any

from blocks._common import error


def run(
    input_data: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed until an artifact owner provides an export contract."""

    del input_data, context
    return error(
        "sandbox artifact export is unavailable; no selected pack owns a safe "
        "sandbox-to-artifact transfer contract",
        code="UNAVAILABLE",
    )
