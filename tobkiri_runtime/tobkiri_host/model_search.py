"""Host-owned narrow port for the bounded model catalog search."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .ports import ModelSearchCommand


class ModelSearchController:
    """Forward one authenticated search request to its composition callback.

    The controller holds no catalog, profile, or settings state: the verified
    Provider supplies only the captured Profile identity and validated filter
    fields, and the composition callback owns settings-owner binding and
    catalog assembly.  Its bounded result echoes only the projected models
    and applied-filter metadata the operation schema admits.
    """

    def __init__(
        self,
        *,
        search_models: Callable[
            [Mapping[str, Any], list[Mapping[str, Any]]], Mapping[str, Any]
        ],
    ) -> None:
        self._search = search_models

    def search_models(self, command: ModelSearchCommand) -> Mapping[str, Any]:
        """Search the composed model catalog for one exact request."""

        result = self._search(
            dict(command.filters),
            [dict(profile) for profile in command.profiles],
        )
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("models"), list)
            or not isinstance(result.get("filters_applied"), Mapping)
        ):
            raise PermissionError("model search is unavailable")
        return {
            "models": result["models"],
            "filters_applied": dict(result["filters_applied"]),
        }
