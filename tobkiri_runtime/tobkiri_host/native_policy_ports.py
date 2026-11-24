"""Narrow Host-only native policy effect port."""

from typing import Any, Protocol


class NativePolicySelectionPort(Protocol):
    """Retained selection staging; no arbitrary operation or authority API."""

    def select_prepared(self, selection_id: str, invocation: Any) -> None:
        """Validate native snapshot/owner and stage the exact selection effect."""
