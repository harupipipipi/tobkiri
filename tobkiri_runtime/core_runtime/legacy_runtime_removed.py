"""Explicit fail-closed boundaries for deleted pre-v4 runtime services."""

from __future__ import annotations

from typing import NoReturn


def removed_authority_service() -> NoReturn:
    """Reject legacy approval/execution service access from compatibility UI."""
    raise RuntimeError(
        "legacy authority workflow is unavailable in Pack v4 production runtime"
    )


def removed_capability_executor() -> NoReturn:
    """Reject retired capability_executor access from compatibility callers."""
    raise RuntimeError(
        "capability_executor is unavailable in Pack v4 production runtime"
    )


__all__ = ["removed_authority_service", "removed_capability_executor"]
