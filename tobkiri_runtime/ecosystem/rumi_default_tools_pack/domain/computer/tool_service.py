"""Pack-owned Computer Use semantics built on the native host contract."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core_runtime.host_broker.computer_contract import (
    ComputerHost,
    ComputerHostActionResult,
    ComputerHostExecutionOptions,
    ComputerHostTarget,
)

from .models import ComputerTarget


class ComputerToolService:
    """Compatibility service that keeps public tool semantics above a host."""

    def __init__(self, host: ComputerHost) -> None:
        self._host = host

    def observe(self, target: ComputerTarget | dict[str, Any]) -> dict[str, Any]:
        """Return a legacy observation enriched with host binding metadata."""
        host_target = self._host_target(target)
        observation = self._host.observe(host_target)
        result = dict(observation.data)
        result["surface_id"] = observation.surface_id
        result["observation_revision"] = observation.observation_revision
        result["coordinate_space"] = observation.coordinate_space
        return result

    def list_surfaces(self) -> list[dict[str, Any]]:
        """Return native surfaces without exposing the host implementation."""

        return self._host.list_surfaces()

    def click(
        self,
        target: ComputerTarget | dict[str, Any],
        x: int = 0,
        y: int = 0,
        button: str = "left",
    ) -> dict[str, Any]:
        """Execute a native click primitive."""
        return self._execute(target, "click", {"x": x, "y": y, "button": button})

    def type_text(
        self,
        target: ComputerTarget | dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        """Execute a native text-input primitive."""
        return self._execute(target, "type_text", {"text": text})

    def key(
        self,
        target: ComputerTarget | dict[str, Any],
        key_combo: str,
    ) -> dict[str, Any]:
        """Execute a native key primitive."""
        return self._execute(target, "key", {"key_combo": key_combo})

    def scroll(
        self,
        target: ComputerTarget | dict[str, Any],
        x: int = 0,
        y: int = 0,
        direction: str = "down",
        clicks: int = 3,
    ) -> dict[str, Any]:
        """Execute a native scroll primitive."""
        return self._execute(
            target,
            "scroll",
            {"x": x, "y": y, "direction": direction, "clicks": clicks},
        )

    def move(
        self,
        target: ComputerTarget | dict[str, Any],
        x: int = 0,
        y: int = 0,
    ) -> dict[str, Any]:
        """Execute a native pointer-move primitive."""
        return self._execute(target, "move", {"x": x, "y": y})

    def drag(
        self,
        target: ComputerTarget | dict[str, Any],
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
    ) -> dict[str, Any]:
        """Execute a native drag primitive."""
        return self._execute(
            target,
            "drag",
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        )

    def semantic_action(
        self,
        target: ComputerTarget | dict[str, Any],
        intent: str = "",
        element_or_point: Any = None,
    ) -> dict[str, Any]:
        """Translate a pack-level semantic action to a host accessibility primitive."""
        return self._execute(
            target,
            "accessibility_action",
            {"intent": intent, "element_or_point": element_or_point},
        )

    def background_action(
        self,
        action: str,
        target: ComputerTarget | dict[str, Any],
        payload: dict[str, Any],
        *,
        verified_only: bool = False,
    ) -> dict[str, Any]:
        """Require a background-safe native transport."""
        return self._execute(
            target,
            self._primitive(action),
            payload,
            options=ComputerHostExecutionOptions(
                background_only=True,
                verified_only=verified_only,
            ),
        )

    def pid_event(
        self,
        action: str,
        target: ComputerTarget | dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Require a PID-bound native transport."""
        return self._execute(
            target,
            self._primitive(action),
            payload,
            options=ComputerHostExecutionOptions(pid_only=True),
        )

    def doctor(self) -> dict[str, Any]:
        """Return native host diagnostics without provider or tool assumptions."""
        capabilities = self._host.probe()
        if capabilities.metadata:
            return dict(capabilities.metadata)
        return {
            "platform": capabilities.platform,
            "host_id": capabilities.host_id,
            "primitives": list(capabilities.primitives),
        }

    def _execute(
        self,
        target: ComputerTarget | dict[str, Any],
        primitive: str,
        args: dict[str, Any],
        *,
        options: ComputerHostExecutionOptions | None = None,
    ) -> dict[str, Any]:
        host_target = self._host_target(target)
        result = self._host.execute_primitive(host_target, primitive, args, options)
        return self._legacy_action_result(result)

    @staticmethod
    def _primitive(action: str) -> str:
        return "accessibility_action" if action == "semantic_action" else action

    @staticmethod
    def _legacy_action_result(result: ComputerHostActionResult) -> dict[str, Any]:
        legacy = dict(result.data)
        legacy.update(
            {
                "executed": result.delivered,
                "delivered": result.delivered,
                "effect_observed": result.effect_observed,
                "postcondition_verified": result.postcondition_verified,
                "driver": result.transport,
                "transport": result.transport,
                "requires_foreground": result.foreground_required,
                "uses_physical_input": result.physical_input,
                "can_parallel_user_work": result.parallel_user_work_safe,
                "surface_id": result.surface_id,
                "observation_revision": result.observation_revision,
                "coordinate_space": result.coordinate_space,
            }
        )
        return legacy

    @staticmethod
    def _host_target(
        target: ComputerTarget | dict[str, Any],
    ) -> ComputerHostTarget:
        selectors = asdict(target) if isinstance(target, ComputerTarget) else dict(target)
        surface_id = str(selectors.pop("surface_id", "") or "").strip()
        revision = str(selectors.pop("observation_revision", "") or "").strip()
        coordinate_space = str(selectors.get("coordinate_space") or "window")
        if not surface_id:
            surface_id = ComputerToolService._derived_surface_id(selectors)
        return ComputerHostTarget(
            surface_id=surface_id,
            observation_revision=revision,
            coordinate_space=coordinate_space,
            selectors=selectors,
        )

    @staticmethod
    def _derived_surface_id(selectors: dict[str, Any]) -> str:
        for key in ("browser_tab_id", "hwnd", "window_id", "pid"):
            value = selectors.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
        return "desktop"
