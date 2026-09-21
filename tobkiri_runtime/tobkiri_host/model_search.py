"""Host-owned narrow port for the bounded model catalog search."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.errors import CanonicalizationError

from .ports import ModelSearchCommand


class ModelSearchController:
    """Forward one authenticated search request to its composition callback.

    The controller holds no catalog or owner capability: the verified
    Provider supplies the captured Profile identity, validated filter fields,
    registry snapshot, and a non-secret runtime-settings projection.  The
    composition callback owns catalog assembly.  Its bounded result echoes
    only the projected models and applied-filter metadata the operation schema
    admits.
    """

    def __init__(
        self,
        *,
        search_models: Callable[
            [
                Mapping[str, Any],
                list[Mapping[str, Any]],
                Mapping[str, Any],
            ],
            Mapping[str, Any],
        ],
    ) -> None:
        self._search = search_models

    def search_models(self, command: ModelSearchCommand) -> Mapping[str, Any]:
        """Search the composed model catalog for one exact request."""

        result = self._search(
            dict(command.filters),
            [dict(profile) for profile in command.profiles],
            dict(command.runtime_settings),
        )
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("models"), list)
            or not isinstance(result.get("filters_applied"), Mapping)
        ):
            raise PermissionError("model search is unavailable")
        projected = {
            "models": result["models"],
            "filters_applied": dict(result["filters_applied"]),
        }
        # Durable operation journaling digests the result as canonical JSON;
        # fail closed here rather than surfacing an opaque journal rejection.
        try:
            canonical_json(projected)
        except CanonicalizationError as error:
            raise PermissionError(
                "model search result is not canonical JSON"
            ) from error
        return projected
