"""Explicit fail-closed boundaries for deleted pre-v4 runtime services."""

from __future__ import annotations


def removed_authority_service() -> None:
    """Reject legacy approval/execution service access from compatibility UI."""
    raise RuntimeError(
        "legacy authority workflow is unavailable in Pack v4 production runtime"
    )


__all__ = ["removed_authority_service"]
