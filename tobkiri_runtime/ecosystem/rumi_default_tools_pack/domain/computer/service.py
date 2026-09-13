"""ComputerSeatService – orchestrates driver selection and fallback.

This is the main entry point for all computer actions. It selects the
best available driver, attempts the action, and falls back through the
driver chain on failure.
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from time import monotonic as _trace_monotonic
from typing import Any

from .audit import AuditLogger
from .models import ActionResult, ComputerTarget, ObserveResult
from .permissions import requires_approval
from .registry import DriverRegistry
from .trace import emit_computer_trace, result_trace_facts


class ComputerSeatService:
    """Orchestrates computer actions through the driver chain.

    Provides observe, click, type_text, key, scroll, and semantic_action
    methods that automatically select the best driver and fall back on
    failure.
    """

    def __init__(
        self,
        registry: DriverRegistry,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        """Initialize the service.

        Args:
            registry: The driver registry to use for driver selection.
            audit_logger: Optional audit logger. Creates a default one if None.
        """
        self._registry = registry
        self._audit = audit_logger or AuditLogger()
        self._platform = sys.platform

    @property
    def platform(self) -> str:
        """Return the native platform identifier used for driver selection."""
        return self._platform

    def driver_chain(self) -> list[Any]:
        """Return the currently available native driver chain."""
        return self._registry.get_driver_chain(self._platform)

    def observe(self, target: ComputerTarget | dict[str, Any]) -> dict[str, Any]:
        """Observe the target – returns screenshot + AX tree + capabilities.

        Aggregates results from multiple drivers: screenshot from the best
        screenshot-capable driver, ax_tree from the best AX-capable driver,
        and merged capabilities from all available drivers.

        Args:
            target: The target to observe.

        Returns:
            Dict with platform, target_window, screenshot, ax_tree,
            capabilities, recommended_next_actions, fallback_available.
        """
        target = self._normalize_target(target)
        chain = self._registry.get_driver_chain(self._platform)

        if not chain:
            return asdict(ObserveResult(platform=self._platform))

        merged = ObserveResult(platform=self._platform, fallback_available=len(chain) > 1)
        merged_caps: dict[str, bool] = {}
        drivers_used: list[str] = []

        for driver in chain:
            try:
                result = driver.observe(target)
            except Exception:
                continue

            # Take screenshot from first driver that provides one
            if not merged.screenshot and result.screenshot:
                merged.screenshot = result.screenshot
                drivers_used.append(driver.name)

            # Take ax_tree/dom_tree from first drivers that provide them
            if not merged.ax_tree and result.ax_tree:
                merged.ax_tree = result.ax_tree
                if driver.name not in drivers_used:
                    drivers_used.append(driver.name)
            if not merged.dom_tree and result.dom_tree:
                merged.dom_tree = result.dom_tree
                if driver.name not in drivers_used:
                    drivers_used.append(driver.name)

            # Take target_window from first driver that provides one
            if not merged.target_window and result.target_window:
                merged.target_window = result.target_window

            # Merge capabilities
            caps = driver.capabilities()
            for k, v in asdict(caps).items():
                if v:
                    merged_caps[k] = True

        merged.capabilities = merged_caps
        merged.recommended_next_actions = self._recommended_next_actions(merged_caps)

        for name in drivers_used:
            self._audit.record(
                action="observe",
                driver=name,
                target_app=target.app or "",
                target_pid=target.pid,
            )

        return asdict(merged)

    def click(
        self,
        target: ComputerTarget | dict[str, Any],
        x: int = 0,
        y: int = 0,
        button: str = "left",
    ) -> dict[str, Any]:
        """Click at coordinates on the target.

        Tries AX semantic click first, then postToPid, then foreground.

        Args:
            target: The target application/window.
            x: X coordinate.
            y: Y coordinate.
            button: Mouse button ("left", "right", "middle").

        Returns:
            ActionResult as dict.
        """
        target = self._normalize_target(target)
        payload = {"x": x, "y": y, "button": button}
        return self._fallback_chain("click", target, payload)

    def background_action(
        self,
        action: str,
        target: ComputerTarget | dict[str, Any],
        payload: dict[str, Any],
        *,
        verified_only: bool = False,
    ) -> dict[str, Any]:
        """Run an action only through non-physical background-capable drivers."""
        target = self._normalize_target(target)
        return self._fallback_chain(action, target, payload, background_only=True, verified_only=verified_only)

    def pid_event(
        self,
        action: str,
        target: ComputerTarget | dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run an action only through PID/PostMessage-capable transports."""
        target = self._normalize_target(target)
        normalized_action = str(action or "").strip()
        if normalized_action not in {"click", "type_text", "key", "scroll"}:
            return self._pid_event_failure(normalized_action, target, [f"Unsupported pid_event action: {normalized_action}"])

        errors: list[str] = []
        for driver in self._registry.get_driver_chain(self._platform):
            if not self._driver_supports_pid_event(driver, normalized_action):
                continue
            method = getattr(driver, normalized_action, None)
            if method is None:
                continue
            try:
                result: ActionResult = self._dispatch(method, target, payload)
            except Exception as exc:
                errors.append(f"{driver.name}: {exc}")
                continue
            if not result.executed:
                errors.extend(result.notes or [f"{driver.name}: not executed"])
                continue
            if result.uses_physical_input or result.requires_foreground or not result.can_parallel_user_work:
                errors.append(f"{driver.name}: result was not PID/background safe")
                continue
            result.is_fallback = bool(errors)
            self._audit.record(
                action=f"pid_event.{normalized_action}",
                driver=driver.name,
                target_app=target.app or "",
                target_pid=target.pid,
                approval_required=requires_approval("computer.pid_event"),
                result=asdict(result),
            )
            return asdict(result)

        return self._pid_event_failure(normalized_action, target, errors or ["No PID/PostMessage driver accepted the action"])

    def type_text(
        self,
        target: ComputerTarget | dict[str, Any],
        text: str,
    ) -> dict[str, Any]:
        """Type text into the target.

        Tries AXSetValue first, then postToPid, then foreground.

        Args:
            target: The target application/window.
            text: The text to type.

        Returns:
            ActionResult as dict.
        """
        target = self._normalize_target(target)
        payload = {"text": text}
        return self._fallback_chain("type_text", target, payload)

    def set_text_control(
        self,
        target: ComputerTarget | dict[str, Any],
        text: str,
        selector: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace an exactly bound semantic text control in the background."""
        target = self._normalize_target(target)
        return self._fallback_chain(
            "set_text_control",
            target,
            {"text": text, "selector": dict(selector)},
            background_only=True,
            verified_only=True,
        )

    def probe_text_control(
        self,
        target: ComputerTarget | dict[str, Any],
        selector: dict[str, Any],
    ) -> dict[str, Any]:
        """Probe an exact semantic text control through Swift only, without fallback."""
        target = self._normalize_target(target)
        action = "probe_text_control"
        started = _trace_monotonic()
        emit_computer_trace(
            "seat.start",
            action,
            requested_delivery_mode="background_read_only",
            target_app_present=bool(target.app),
            target_bundle_present=bool(target.bundle_id),
            target_pid_present=target.pid is not None,
            target_window_present=target.window_id is not None or bool(target.window_title),
        )
        driver = self._registry.get_driver("mac_swift_host")
        method = getattr(driver, action, None) if driver is not None else None
        try:
            available = bool(driver is not None and driver.is_available())
        except Exception:
            available = False
        if not available or method is None:
            result = ActionResult(
                action=action,
                driver="none",
                executed=False,
                confidence="failed",
                can_parallel_user_work=True,
                requires_foreground=False,
                uses_physical_input=False,
                data={
                    "probe_completed": False,
                    "semantic_control_ready": False,
                    "input_dispatched": False,
                    "mutation_attempted": False,
                    "error_code": "TYPE_SEMANTIC_PROBE_UNAVAILABLE",
                },
                notes=["The native semantic probe driver is unavailable."],
            )
        else:
            try:
                result = self._dispatch(method, target, {"selector": dict(selector)})
            except Exception:
                result = ActionResult(
                    action=action,
                    driver="mac_swift_host",
                    executed=False,
                    confidence="failed",
                    can_parallel_user_work=True,
                    requires_foreground=False,
                    uses_physical_input=False,
                    data={
                        "probe_completed": False,
                        "semantic_control_ready": False,
                        "input_dispatched": False,
                        "mutation_attempted": False,
                        "error_code": "TYPE_SEMANTIC_PROBE_FAILED",
                    },
                    notes=["The native semantic probe failed."],
                )
            if result.uses_physical_input or result.requires_foreground:
                result = ActionResult(
                    action=action,
                    driver="mac_swift_host",
                    executed=False,
                    confidence="failed",
                    can_parallel_user_work=True,
                    requires_foreground=False,
                    uses_physical_input=False,
                    data={
                        "probe_completed": False,
                        "semantic_control_ready": False,
                        "input_dispatched": False,
                        "mutation_attempted": False,
                        "error_code": "TYPE_SEMANTIC_PROBE_UNSAFE_RESULT",
                    },
                    notes=["The native semantic probe returned an unsafe result."],
                )
        self._audit.record(
            action="computer.probe_text_control",
            driver=result.driver,
            target_app=target.app or "",
            target_pid=target.pid,
            approval_required=requires_approval("computer.probe_text_control"),
            result=asdict(result),
        )
        emit_computer_trace(
            "seat.result",
            action,
            duration_ms=(_trace_monotonic() - started) * 1000,
            **result_trace_facts(asdict(result)),
        )
        return asdict(result)

    def key(
        self,
        target: ComputerTarget | dict[str, Any],
        key_combo: str,
    ) -> dict[str, Any]:
        """Send a key combination to the target.

        Args:
            target: The target application/window.
            key_combo: Key combination (e.g. "cmd+s", "enter").

        Returns:
            ActionResult as dict.
        """
        target = self._normalize_target(target)
        payload = {"key_combo": key_combo}
        return self._fallback_chain("key", target, payload)

    def scroll(
        self,
        target: ComputerTarget | dict[str, Any],
        x: int = 0,
        y: int = 0,
        direction: str = "down",
        clicks: int = 3,
    ) -> dict[str, Any]:
        """Scroll at a position on the target.

        Args:
            target: The target application/window.
            x: X coordinate.
            y: Y coordinate.
            direction: Scroll direction ("up", "down", "left", "right").
            clicks: Number of scroll clicks.

        Returns:
            ActionResult as dict.
        """
        target = self._normalize_target(target)
        payload = {"x": x, "y": y, "direction": direction, "clicks": clicks}
        return self._fallback_chain("scroll", target, payload)

    def move(
        self,
        target: ComputerTarget | dict[str, Any],
        x: int = 0,
        y: int = 0,
    ) -> dict[str, Any]:
        """Move cursor to coordinates on the target.

        Args:
            target: The target application/window.
            x: X coordinate.
            y: Y coordinate.

        Returns:
            ActionResult as dict.
        """
        target = self._normalize_target(target)
        payload = {"x": x, "y": y}
        return self._fallback_chain("move", target, payload)

    def drag(
        self,
        target: ComputerTarget | dict[str, Any],
        x1: int = 0,
        y1: int = 0,
        x2: int = 0,
        y2: int = 0,
    ) -> dict[str, Any]:
        """Drag from one point to another on the target.

        Args:
            target: The target application/window.
            x1: Start X coordinate.
            y1: Start Y coordinate.
            x2: End X coordinate.
            y2: End Y coordinate.

        Returns:
            ActionResult as dict.
        """
        target = self._normalize_target(target)
        payload = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        return self._fallback_chain("drag", target, payload)

    def semantic_action(
        self,
        target: ComputerTarget | dict[str, Any],
        intent: str = "",
        element_or_point: Any = None,
    ) -> dict[str, Any]:
        """Execute a semantic action (e.g. 'press the Save button').

        Args:
            target: The target application/window.
            intent: Natural language intent description.
            element_or_point: The AX element or coordinate to act on.

        Returns:
            ActionResult as dict.
        """
        target = self._normalize_target(target)
        payload = {"intent": intent, "element_or_point": element_or_point}
        return self._fallback_chain("semantic_action", target, payload)

    def doctor(self) -> dict[str, Any]:
        """Check platform capabilities and driver availability.

        Returns:
            Dict with platform info, available drivers, and their capabilities.
        """
        chain = self._registry.get_driver_chain(self._platform)
        drivers_info = []
        for driver in chain:
            caps = driver.capabilities()
            drivers_info.append({
                "name": driver.name,
                "platform": driver.platform,
                "available": driver.is_available(),
                "capabilities": asdict(caps),
            })

        all_drivers = self._registry.all_drivers
        unavailable = [
            {"name": name, "platform": d.platform, "available": False}
            for name, d in all_drivers.items()
            if not d.is_available()
        ]

        return {
            "platform": self._platform,
            "available_drivers": drivers_info,
            "unavailable_drivers": unavailable,
            "driver_chain_order": [d.name for d in chain],
        }

    def _fallback_chain(
        self,
        action: str,
        target: ComputerTarget,
        payload: dict[str, Any],
        *,
        background_only: bool = False,
        verified_only: bool = False,
    ) -> dict[str, Any]:
        """Try each driver in the chain until one succeeds.

        Args:
            action: The action name.
            target: The target.
            payload: Action-specific parameters.

        Returns:
            ActionResult as dict from the first successful driver,
            or a failure result if all drivers fail.
        """
        chain = self._registry.get_driver_chain(self._platform)
        ax_candidate = (
            self._registry.safe_ax_candidate_diagnostics({driver.name for driver in chain})
            if action == "type_text"
            else None
        )
        if ax_candidate is not None:
            ax_driver = self._registry.get_driver("mac_accessibility")
            candidate_diagnostics = getattr(ax_driver, "safe_type_candidate_diagnostics", None)
            if callable(candidate_diagnostics):
                try:
                    target_diagnostics = candidate_diagnostics(target)
                except Exception:
                    target_diagnostics = {
                        "pyobjc_ax_import_available": False,
                        "ax_process_trusted": False,
                        "ax_set_value_unsafe_app": False,
                        "attempted": False,
                        "result_code": "AX_DIAGNOSTICS_UNAVAILABLE",
                    }
                if isinstance(target_diagnostics, dict):
                    ax_candidate.update(target_diagnostics)
        errors: list[str] = []
        is_fallback = False
        started = _trace_monotonic()
        emit_computer_trace(
            "seat.start",
            action,
            requested_delivery_mode="background" if background_only else "auto",
            target_app_present=bool(target.app),
            target_bundle_present=bool(target.bundle_id),
            target_pid_present=target.pid is not None,
            target_window_present=target.window_id is not None or target.hwnd is not None or bool(target.window_title),
        )

        for driver in chain:
            if not background_only and self._driver_is_explicit_background_transport(driver):
                continue
            if background_only and verified_only and self._driver_is_explicit_background_transport(driver):
                continue
            if background_only and not self._driver_supports_background_action(driver, action):
                continue
            try:
                method = getattr(driver, action, None)
                if method is None:
                    continue

                # Call the appropriate method with the right arguments
                driver_started = _trace_monotonic()
                if ax_candidate is not None and driver.name == "mac_accessibility":
                    ax_candidate["attempted"] = True
                result: ActionResult = self._dispatch(method, target, payload)
                if ax_candidate is not None and driver.name == "mac_accessibility":
                    if result.executed and self._result_completion_verified(result):
                        ax_candidate["result_code"] = "AX_TYPE_VERIFIED"
                    elif result.executed:
                        ax_candidate["result_code"] = "AX_TYPE_POSTED_UNVERIFIED"
                    elif ax_candidate.get("result_code") not in {
                        "AX_IMPORT_UNAVAILABLE",
                        "AX_NOT_TRUSTED",
                        "AX_SET_VALUE_UNSAFE_APP",
                        "AX_TARGET_MISSING",
                    }:
                        ax_candidate["result_code"] = "AX_TYPE_NOT_EXECUTED"
                self._attach_ax_candidate_diagnostics(result, ax_candidate)
                emit_computer_trace(
                    "seat.driver_result",
                    action,
                    duration_ms=(_trace_monotonic() - driver_started) * 1000,
                    **result_trace_facts(asdict(result)),
                )

                if result.executed:
                    if action == "type_text" and not self._result_completion_verified(result):
                        # AX/background delivery may have happened even though no
                        # observable postcondition was proved.  Return the posted
                        # result without trying another driver, which could
                        # duplicate text, and let the controller/helper request
                        # an observation.
                        result.data.setdefault("outcome", "posted_unverified")
                        result.data.setdefault("verification_required", "screenshot")
                        result.data.setdefault("completion_verified", False)
                        result.is_fallback = is_fallback
                        self._audit.record(
                            action=action,
                            driver=driver.name,
                            target_app=target.app or "",
                            target_pid=target.pid,
                            approval_required=requires_approval(action),
                            result=asdict(result),
                        )
                        emit_computer_trace(
                            "seat.result",
                            action,
                            duration_ms=(_trace_monotonic() - started) * 1000,
                            **result_trace_facts(asdict(result)),
                        )
                        return asdict(result)
                    if (
                        action == "key"
                        and self._result_is_post_only(result)
                        and not self._result_completion_verified(result)
                    ):
                        # A posted key event may already have submitted a form
                        # or shortcut. Never replay it through another driver.
                        result.data.setdefault("outcome", "posted_unverified")
                        result.data.setdefault("verification_required", "focus_state")
                        result.data.setdefault("completion_verified", False)
                        result.data.setdefault("input_dispatched", True)
                        result.is_fallback = is_fallback
                        self._audit.record(
                            action=action,
                            driver=driver.name,
                            target_app=target.app or "",
                            target_pid=target.pid,
                            approval_required=requires_approval(action),
                            result=asdict(result),
                        )
                        emit_computer_trace(
                            "seat.result",
                            action,
                            duration_ms=(_trace_monotonic() - started) * 1000,
                            **result_trace_facts(asdict(result)),
                        )
                        return asdict(result)
                    if background_only and verified_only and self._result_is_post_only(result):
                        errors.append(f"{driver.name}: background transport is not effect-verified")
                        is_fallback = True
                        continue
                    if background_only and result.uses_physical_input:
                        errors.append(f"{driver.name}: physical input is not background-safe")
                        is_fallback = True
                        continue
                    result.is_fallback = is_fallback
                    self._audit.record(
                        action=action,
                        driver=driver.name,
                        target_app=target.app or "",
                        target_pid=target.pid,
                        intent=payload.get("intent", ""),
                        approval_required=requires_approval(action),
                        result=asdict(result),
                    )
                    emit_computer_trace(
                        "seat.result",
                        action,
                        duration_ms=(_trace_monotonic() - started) * 1000,
                        **result_trace_facts(asdict(result)),
                    )
                    return asdict(result)
                if action in {"type_text", "set_text_control"} and self._terminal_native_type_failure(result):
                    # A physical text event may already have reached the target.
                    # Never replay it through another driver when completion is
                    # uncertain, because doing so can duplicate a partial input.
                    result.is_fallback = is_fallback
                    self._audit.record(
                        action=action,
                        driver=driver.name,
                        target_app=target.app or "",
                        target_pid=target.pid,
                        approval_required=requires_approval(action),
                        result=asdict(result),
                    )
                    emit_computer_trace(
                        "seat.result",
                        action,
                        duration_ms=(_trace_monotonic() - started) * 1000,
                        **result_trace_facts(asdict(result)),
                    )
                    return asdict(result)
                # Driver returned executed=False – mark next attempt as fallback
                is_fallback = True

            except Exception as e:
                errors.append(f"{driver.name}: {e}")
                if ax_candidate is not None and driver.name == "mac_accessibility":
                    ax_candidate["attempted"] = True
                    ax_candidate["result_code"] = "AX_DRIVER_ERROR"
                emit_computer_trace(
                    "seat.driver_result",
                    action,
                    selected_driver=driver.name,
                    duration_ms=(_trace_monotonic() - driver_started) * 1000 if "driver_started" in locals() else 0,
                    result_ok=False,
                    error_code="DRIVER_EXCEPTION",
                )
                is_fallback = True
                continue

        # All drivers failed
        failure = ActionResult(
            action=action,
            driver="none",
            executed=False,
            confidence="failed",
            notes=errors or ["No available driver for this action"],
        )
        self._attach_ax_candidate_diagnostics(failure, ax_candidate)
        self._audit.record(
            action=action,
            driver="none",
            target_app=target.app or "",
            target_pid=target.pid,
            approval_required=requires_approval(action),
            result=asdict(failure),
        )
        emit_computer_trace(
            "seat.result",
            action,
            duration_ms=(_trace_monotonic() - started) * 1000,
            **result_trace_facts(asdict(failure)),
        )
        return asdict(failure)

    @staticmethod
    def _terminal_native_type_failure(result: ActionResult) -> bool:
        diagnostics = result.data.get("diagnostics")
        if not isinstance(diagnostics, dict):
            diagnostics = result.data
        return (
            diagnostics.get("input_dispatched") is True
            or diagnostics.get("direct_ax_attempted") is True
            or bool(diagnostics.get("failure_stage"))
            or bool(diagnostics.get("error_code"))
        )

    @staticmethod
    def _result_completion_verified(result: ActionResult) -> bool:
        if result.data.get("completion_verified") is True:
            return True
        diagnostics = result.data.get("diagnostics")
        return isinstance(diagnostics, dict) and diagnostics.get("completion_verified") is True

    @staticmethod
    def _attach_ax_candidate_diagnostics(
        result: ActionResult,
        diagnostics: dict[str, object] | None,
    ) -> None:
        if diagnostics is not None:
            result.data["ax_candidate"] = dict(diagnostics)

    @staticmethod
    def _driver_is_explicit_background_transport(driver: Any) -> bool:
        return str(getattr(driver, "name", "") or "") in {"mac_cgevent_pid", "windows_postmessage"}

    @staticmethod
    def _result_is_post_only(result: ActionResult) -> bool:
        confidence = str(result.confidence or "").strip().lower()
        if confidence in {"experimental", "best_effort", "posted_only", "posted only"}:
            return True
        return result.driver in {"mac_cgevent_pid", "windows_postmessage"}

    @staticmethod
    def _driver_supports_background_action(driver: Any, action: str) -> bool:
        try:
            caps = driver.capabilities()
        except Exception:
            return False
        if bool(getattr(caps, "requires_foreground_for_capture", False)):
            return False
        if action != "set_text_control" and not bool(getattr(caps, "can_parallel_user_work", False)):
            return False
        if action == "click":
            return bool(getattr(caps, "can_background_click", False))
        if action == "type_text":
            return bool(getattr(caps, "can_background_type", False))
        if action == "set_text_control":
            return bool(getattr(caps, "can_background_set_text_control", False))
        if action == "key":
            return bool(getattr(caps, "can_background_key", False))
        if action == "scroll":
            return bool(getattr(caps, "can_background_scroll", False))
        if action == "semantic_action":
            return bool(getattr(caps, "can_semantic_action", False))
        return False

    @staticmethod
    def _driver_supports_pid_event(driver: Any, action: str) -> bool:
        try:
            caps = driver.capabilities()
        except Exception:
            return False
        if not bool(getattr(caps, "can_pid_event", False)):
            return False
        if bool(getattr(caps, "can_foreground_action", False)):
            return False
        if not bool(getattr(caps, "can_parallel_user_work", False)):
            return False
        if action == "click":
            return bool(getattr(caps, "can_background_click", False))
        if action == "type_text":
            return bool(getattr(caps, "can_background_type", False))
        if action == "key":
            return bool(getattr(caps, "can_background_key", False))
        if action == "scroll":
            return bool(getattr(caps, "can_background_scroll", False))
        return False

    def _pid_event_failure(self, action: str, target: ComputerTarget, notes: list[str]) -> dict[str, Any]:
        failure = ActionResult(
            action=action,
            driver="none",
            executed=False,
            confidence="failed",
            can_parallel_user_work=True,
            requires_foreground=False,
            uses_physical_input=False,
            notes=notes,
        )
        self._audit.record(
            action=f"pid_event.{action}",
            driver="none",
            target_app=target.app or "",
            target_pid=target.pid,
            approval_required=requires_approval("computer.pid_event"),
            result=asdict(failure),
        )
        return asdict(failure)

    def _dispatch(
        self,
        method: Any,
        target: ComputerTarget,
        payload: dict[str, Any],
    ) -> ActionResult:
        """Dispatch an action method with the correct arguments.

        Args:
            method: The driver method to call.
            target: The target.
            payload: Action-specific parameters.

        Returns:
            ActionResult from the driver method.
        """
        # Determine which arguments the method expects based on action name
        method_name = method.__func__.__name__ if hasattr(method, "__func__") else ""

        if method_name == "click":
            return method(
                target,
                x=payload.get("x", 0),
                y=payload.get("y", 0),
                button=payload.get("button", "left"),
            )
        elif method_name == "type_text":
            return method(target, text=payload.get("text", ""))
        elif method_name == "key":
            return method(target, key_combo=payload.get("key_combo", ""))
        elif method_name == "scroll":
            return method(
                target,
                x=payload.get("x", 0),
                y=payload.get("y", 0),
                direction=payload.get("direction", "down"),
                clicks=payload.get("clicks", 3),
            )
        elif method_name == "semantic_action":
            return method(
                target,
                intent=payload.get("intent", ""),
                element_or_point=payload.get("element_or_point"),
            )
        elif method_name == "move":
            return method(target, x=payload.get("x", 0), y=payload.get("y", 0))
        elif method_name == "drag":
            return method(
                target,
                x1=payload.get("x1", 0),
                y1=payload.get("y1", 0),
                x2=payload.get("x2", 0),
                y2=payload.get("y2", 0),
            )
        else:
            # Generic fallback – pass target and payload
            return method(target, **payload)

    @staticmethod
    def _normalize_target(
        target: ComputerTarget | dict[str, Any],
    ) -> ComputerTarget:
        """Normalize a target to a ComputerTarget instance.

        Args:
            target: Either a ComputerTarget or a dict.

        Returns:
            A ComputerTarget instance.
        """
        if isinstance(target, ComputerTarget):
            return target
        return ComputerTarget(
            kind=target.get("kind", "desktop"),
            app=target.get("app"),
            pid=target.get("pid"),
            window_id=target.get("window_id"),
            window_title=target.get("window_title") or target.get("title"),
            window_bounds=dict(target.get("window_bounds") or {}),
            hwnd=target.get("hwnd"),
            bundle_id=target.get("bundle_id"),
            browser_client_id=target.get("browser_client_id") or target.get("client_id"),
            browser_tab_id=target.get("browser_tab_id") or target.get("tab_id"),
            url=target.get("url"),
            coordinate_space=target.get("coordinate_space", "window"),
        )

    @staticmethod
    def _recommended_next_actions(capabilities: dict[str, bool]) -> list[dict[str, Any]]:
        """Build model-facing hints from merged driver capabilities."""
        actions: list[dict[str, Any]] = []
        if capabilities.get("can_dom_action"):
            actions.append({
                "action": "browser_cdp.click",
                "confidence": "high",
                "reason": "Target exposes DOM actions.",
            })
        if capabilities.get("can_semantic_action"):
            actions.append({
                "action": "computer.semantic_action",
                "confidence": "high",
                "reason": "Target exposes accessibility elements.",
            })
        if capabilities.get("can_background_click"):
            actions.append({
                "action": "computer.click",
                "confidence": "best_effort",
                "reason": "Coordinate click may be converted to a background-safe semantic action.",
            })
        if capabilities.get("can_foreground_action"):
            actions.append({
                "action": "computer.click",
                "confidence": "best_effort",
                "requires_foreground": True,
                "reason": "Foreground fallback is available if background-safe drivers fail.",
            })
        return actions or [
            {"action": "computer.click", "description": "Click at coordinates"},
            {"action": "computer.semantic_action", "description": "Press an AX element by intent"},
        ]
