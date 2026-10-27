"""A two-function genuine monotonic clock, without subscriptions or polling."""

from __future__ import annotations

import math
import time
from typing import Any

try:
    from .ui_clock import ClockBudget
except ImportError:
    from ui_clock import ClockBudget

IMPORT_NAME = "tobkiri-ui:clock/monotonic@1.0.0"
_U64_MAX = (1 << 64) - 1
_NANOSECONDS = 1_000_000_000


def validate_clock_import(import_name: str, item: Any, engine: Any) -> None:
    """Require exactly two no-argument/u64 functions in the explicit interface."""
    if import_name != IMPORT_NAME:
        raise ValueError("unsupported UI monotonic-clock import name")
    import wasmtime.component as component_types

    if not isinstance(item, component_types.ComponentInstanceType):
        raise ValueError("monotonic-clock import must be a ComponentInstanceType")
    exports = item.exports(engine)
    if not isinstance(exports, dict) or set(exports) != {"now", "resolution"}:
        raise ValueError("monotonic-clock import must export only now and resolution")
    for name, function in exports.items():
        if (
            not isinstance(function, component_types.FuncType)
            or not isinstance(function.params, list)
            or function.params
            or not isinstance(function.result, component_types.U64)
        ):
            raise ValueError(
                f"monotonic-clock {name} must have no parameters and a u64 result"
            )


class MonotonicClockBridge:
    """Bind genuine nanosecond observations to the Store's shared clock budget."""

    __slots__ = (
        "import_name",
        "_budget",
        "_installed",
        "_now_calls",
        "_resolution_calls",
        "_last_ns",
        "_resolution_ns",
        "_implementation",
    )

    def __init__(self, import_name: str, budget: ClockBudget) -> None:
        if import_name != IMPORT_NAME or not isinstance(budget, ClockBudget):
            raise ValueError(
                "explicit monotonic interface and shared clock budget required"
            )
        self.import_name = import_name
        self._budget = budget
        self._installed = False
        self._now_calls = 0
        self._resolution_calls = 0
        self._last_ns = None
        self._resolution_ns = None
        self._implementation = None

    def install(self, linker: Any) -> None:
        if self._installed:
            raise RuntimeError("monotonic-clock bridge is already installed")
        with linker.root() as root:
            with root.add_instance(self.import_name) as instance:
                instance.add_func("now", self._now)
                instance.add_func("resolution", self._resolution)
        self._installed = True

    def _begin(self, args: tuple) -> None:
        if not self._installed or args:
            raise RuntimeError(
                "monotonic-clock callback is unavailable or has unexpected parameters"
            )
        self._budget.begin()

    def _now(self, store: Any, *args: Any) -> int:
        self._begin(args)
        self._now_calls += 1
        try:
            value = time.monotonic_ns()
        except Exception as error:
            raise RuntimeError("host monotonic clock failed") from error
        if (
            type(value) is not int
            or not 0 <= value <= _U64_MAX
            or (self._last_ns is not None and value < self._last_ns)
        ):
            raise RuntimeError(
                "host monotonic clock returned an invalid or decreasing value"
            )
        self._budget.check()
        self._last_ns = value
        self._budget.complete()
        return value

    def _resolution(self, store: Any, *args: Any) -> int:
        self._begin(args)
        self._resolution_calls += 1
        try:
            info = time.get_clock_info("monotonic")
            resolution = info.resolution
            implementation = info.implementation
            monotonic = info.monotonic
            adjustable = info.adjustable
        except Exception as error:
            raise RuntimeError("host monotonic resolution is unavailable") from error
        if (
            monotonic is not True
            or adjustable is not False
            or not isinstance(implementation, str)
            or not implementation
            or isinstance(resolution, bool)
            or not isinstance(resolution, (int, float))
        ):
            raise RuntimeError("host monotonic clock information is invalid")
        try:
            scaled = float(resolution) * _NANOSECONDS
        except (OverflowError, ValueError) as error:
            raise RuntimeError(
                "host monotonic resolution is not representable"
            ) from error
        if not math.isfinite(scaled) or scaled <= 0:
            raise RuntimeError("host monotonic resolution is invalid")
        value = math.ceil(scaled)
        if not 0 < value <= _U64_MAX:
            raise RuntimeError("host monotonic resolution is outside u64")
        self._budget.check()
        self._resolution_ns = value
        self._implementation = implementation
        self._budget.complete()
        return value

    def report(self) -> dict:
        return {
            "evidence_kind": "ci-host-clock-bridge",
            "import_name": self.import_name,
            "clock_domain": "CI host monotonic clock",
            "clock_source": "python.time.monotonic_ns",
            "resolution_source": "python.time.get_clock_info('monotonic').resolution",
            "clock_implementation": self._implementation,
            "resolution_nanoseconds_ceiling": self._resolution_ns,
            "calls": {
                "now": self._now_calls,
                "resolution": self._resolution_calls,
                "total": self._now_calls + self._resolution_calls,
            },
            "shared_budget": self._budget.report(),
            "deadline_enforced": True,
            "linker_installed": self._installed,
            "production_ui_clock_connected": False,
            "production_enabled": False,
        }
