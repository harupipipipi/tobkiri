"""Adapter from the existing ComputerSeat drivers to the native host contract."""

from __future__ import annotations

import sys
from dataclasses import asdict
from typing import Any

from core_runtime.host_broker.computer_contract import (
    ComputerHostActionResult,
    ComputerHostCapabilities,
    ComputerHostExecutionOptions,
    ComputerHostObservation,
    ComputerHostTarget,
)

from .models import ComputerTarget
from .service import ComputerSeatService


_PRIMITIVES = (
    "accessibility_action",
    "click",
    "drag",
    "key",
    "move",
    "scroll",
    "type_text",
)


class ComputerSeatHostAdapter:
    """Expose existing native driver chains through ``ComputerHost``.

    Approval, retry, and model-facing action semantics remain outside this
    adapter. It only normalizes native observation and primitive execution.
    """

    def __init__(self, service: ComputerSeatService) -> None:
        self._service = service

    def probe(self) -> ComputerHostCapabilities:
        """Return aggregate native capabilities for the current platform."""
        doctor = self._service.doctor()
        drivers = doctor.get("available_drivers", [])
        return ComputerHostCapabilities(
            host_id="rumi.computer_seat",
            platform=str(doctor.get("platform") or sys.platform),
            primitives=_PRIMITIVES,
            can_observe=True,
            can_verify=any(
                bool((driver.get("capabilities") or {}).get("can_semantic_action"))
                for driver in drivers
                if isinstance(driver, dict)
            ),
            metadata=doctor,
        )

    def list_surfaces(self) -> list[dict[str, Any]]:
        """Return surfaces when a driver exposes a model-agnostic listing API."""
        surfaces: list[dict[str, Any]] = []
        for driver in self._service.driver_chain():
            list_surfaces = getattr(driver, "list_surfaces", None)
            if not callable(list_surfaces):
                continue
            result = list_surfaces()
            if isinstance(result, list):
                surfaces.extend(item for item in result if isinstance(item, dict))
        return surfaces

    def observe(self, target: ComputerHostTarget) -> ComputerHostObservation:
        """Observe a revision-bound target through ComputerSeat."""
        result = self._service.observe(self._legacy_target(target))
        target_window = result.get("target_window") or {}
        surface_id = str(target_window.get("surface_id") or target.surface_id)
        revision = str(
            target_window.get("observation_revision")
            or result.get("observation_revision")
            or target.observation_revision
        )
        return ComputerHostObservation(
            surface_id=surface_id,
            observation_revision=revision,
            coordinate_space=target.coordinate_space,
            data=result,
        )

    def execute_primitive(
        self,
        target: ComputerHostTarget,
        primitive: str,
        args: dict[str, Any],
        options: ComputerHostExecutionOptions | None = None,
    ) -> ComputerHostActionResult:
        """Execute an allowlisted native primitive through ComputerSeat."""
        normalized = str(primitive or "").strip()
        if normalized not in _PRIMITIVES:
            return ComputerHostActionResult(
                transport="none",
                delivered=False,
                surface_id=target.surface_id,
                observation_revision=target.observation_revision,
                coordinate_space=target.coordinate_space,
                data={"error": f"Unsupported computer primitive: {normalized}"},
            )

        binding_error = self._binding_error(target)
        if binding_error:
            return ComputerHostActionResult(
                transport="none",
                delivered=False,
                surface_id=target.surface_id,
                observation_revision=target.observation_revision,
                coordinate_space=target.coordinate_space,
                data={
                    "error_code": "STALE_COMPUTER_OBSERVATION",
                    "error": binding_error,
                },
            )

        action = "semantic_action" if normalized == "accessibility_action" else normalized
        execution_options = options or ComputerHostExecutionOptions()
        legacy_target = self._legacy_target(target)
        if execution_options.pid_only:
            result = self._service.pid_event(action, legacy_target, dict(args or {}))
        elif execution_options.background_only:
            result = self._service.background_action(
                action,
                legacy_target,
                dict(args or {}),
                verified_only=execution_options.verified_only,
            )
        else:
            method = getattr(self._service, action)
            result = method(legacy_target, **dict(args or {}))
        delivered = bool(result.get("delivered", result.get("executed", False)))
        # ``ComputerSeatService`` predates the host contract and reports
        # driver verification through ``confidence``. Preserve that evidence
        # when normalizing the legacy result so a verified delivery is not
        # incorrectly downgraded to an unobserved effect. An explicit
        # postcondition remains a separate, stricter result state.
        effect_observed = bool(result.get("effect_observed", False)) or (
            delivered and str(result.get("confidence") or "").lower() == "verified"
        )
        postcondition_verified = bool(result.get("postcondition_verified", False))
        return ComputerHostActionResult(
            transport=str(result.get("transport") or result.get("driver") or "none"),
            delivered=delivered,
            effect_observed=effect_observed,
            postcondition_verified=postcondition_verified,
            foreground_required=bool(result.get("requires_foreground", False)),
            physical_input=bool(result.get("uses_physical_input", False)),
            parallel_user_work_safe=bool(result.get("can_parallel_user_work", False)),
            surface_id=str(result.get("surface_id") or target.surface_id),
            observation_revision=str(
                result.get("observation_revision") or target.observation_revision
            ),
            coordinate_space=str(result.get("coordinate_space") or target.coordinate_space),
            data=result,
        )

    def _binding_error(self, target: ComputerHostTarget) -> str:
        if not target.observation_revision:
            return ""
        current = self.observe(target)
        if current.surface_id != target.surface_id:
            return "The target surface changed after it was observed."
        if (
            current.observation_revision
            and current.observation_revision != target.observation_revision
        ):
            return "The target observation revision is stale."
        return ""

    def verify(
        self,
        target: ComputerHostTarget,
        expected_postcondition: dict[str, Any],
    ) -> ComputerHostActionResult:
        """Verify explicit observation fields without inventing semantics."""
        observation = self.observe(target)
        observed = observation.data
        verified = all(observed.get(key) == value for key, value in expected_postcondition.items())
        return ComputerHostActionResult(
            transport="observe",
            delivered=True,
            effect_observed=verified,
            postcondition_verified=verified,
            parallel_user_work_safe=True,
            surface_id=observation.surface_id,
            observation_revision=observation.observation_revision,
            coordinate_space=observation.coordinate_space,
            data={"observation": observed, "expected_postcondition": expected_postcondition},
        )

    @staticmethod
    def _legacy_target(target: ComputerHostTarget) -> ComputerTarget:
        selectors = dict(target.selectors)
        selectors.setdefault("coordinate_space", target.coordinate_space)
        return ComputerSeatService._normalize_target(selectors)


def host_action_result_dict(result: ComputerHostActionResult) -> dict[str, Any]:
    """Serialize a host result for pack/tool boundaries."""
    return asdict(result)
