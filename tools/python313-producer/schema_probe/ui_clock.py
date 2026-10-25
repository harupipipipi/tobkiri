"""Supply a bounded CI-host wall clock to the schema probe's WASI import."""

from __future__ import annotations

import math
import re
import time
from typing import Any

_IMPORT_NAME = re.compile(r"wasi:clocks/wall-clock@0\.2\.(0|[1-9][0-9]*)")
_U64_MAX = (1 << 64) - 1
_U32_MAX = (1 << 32) - 1
_NANOSECONDS = 1_000_000_000
_MAX_CALL_LIMIT = 100_000
_CLOCK_SOURCE = "python.time.time_ns"
_RESOLUTION_SOURCE = "python.time.get_clock_info('time').resolution"


def _require_import_name(import_name: str) -> None:
    """Reject every clock import except the supported wall-clock interface."""
    if not isinstance(import_name, str) or _IMPORT_NAME.fullmatch(import_name) is None:
        raise ValueError("unsupported WASI wall-clock import name")


def _require_datetime_record(
    component_types: Any,
    record_type: Any,
    name: str,
) -> None:
    """Require the exact seconds/u64 and nanoseconds/u32 record layout."""
    if not isinstance(record_type, component_types.RecordType):
        raise ValueError(f"{name} must be a component RecordType")
    fields = record_type.fields
    if not isinstance(fields, list) or len(fields) != 2:
        raise ValueError(f"{name} must have exactly two datetime fields")
    if any(not isinstance(field, tuple) or len(field) != 2 for field in fields):
        raise ValueError(f"{name} has an invalid datetime field layout")
    seconds_type = getattr(component_types, "U64", None)
    nanoseconds_type = getattr(component_types, "U32", None)
    if (
        not isinstance(seconds_type, type)
        or not isinstance(nanoseconds_type, type)
        or fields[0][0] != "seconds"
        or not isinstance(fields[0][1], seconds_type)
        or fields[1][0] != "nanoseconds"
        or not isinstance(fields[1][1], nanoseconds_type)
    ):
        raise ValueError(
            f"{name} must be {seconds_type!r} seconds and "
            f"{nanoseconds_type!r} nanoseconds"
        )


def validate_clock_import(import_name: str, item: Any, engine: Any) -> None:
    """Validate a component import before registering host clock callbacks."""
    _require_import_name(import_name)
    try:
        import wasmtime.component as component_types
    except ImportError as error:
        raise ValueError("Wasmtime component type support is unavailable") from error

    if not isinstance(item, component_types.ComponentInstanceType):
        raise ValueError("wall-clock import must be a ComponentInstanceType")
    try:
        exports = item.exports(engine)
    except Exception as error:
        raise ValueError("wall-clock component exports cannot be inspected") from error
    if not isinstance(exports, dict) or set(exports) != {
        "datetime",
        "now",
        "resolution",
    }:
        raise ValueError("wall-clock import must export only datetime, now, resolution")

    _require_datetime_record(component_types, exports["datetime"], "datetime")
    for name in ("now", "resolution"):
        function_type = exports[name]
        if not isinstance(function_type, component_types.FuncType):
            raise ValueError(f"wall-clock {name} export must be a FuncType")
        params = function_type.params
        if not isinstance(params, list) or params:
            raise ValueError(f"wall-clock {name} must have no parameters")
        _require_datetime_record(
            component_types,
            function_type.result,
            f"{name} result",
        )


class WallClockBridge:
    """Expose only bounded, real CI-host wall-clock callbacks to a probe linker."""

    __slots__ = (
        "import_name",
        "deadline",
        "call_limit",
        "_calls",
        "_now_calls",
        "_resolution_calls",
        "_record_class",
        "_installed",
        "_resolution_seconds",
        "_resolution_nanoseconds",
        "_clock_implementation",
    )

    def __init__(
        self,
        import_name: str,
        deadline: float,
        call_limit: int = 100_000,
    ) -> None:
        """Bind one validated import name to an absolute monotonic deadline."""
        _require_import_name(import_name)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise ValueError("wall-clock deadline must be finite")
        try:
            deadline_value = float(deadline)
        except (OverflowError, ValueError) as error:
            raise ValueError("wall-clock deadline must be finite") from error
        if not math.isfinite(deadline_value):
            raise ValueError("wall-clock deadline must be finite")
        if (
            not isinstance(call_limit, int)
            or isinstance(call_limit, bool)
            or not 0 < call_limit <= _MAX_CALL_LIMIT
        ):
            raise ValueError("wall-clock call limit is outside its bound")
        self.import_name = import_name
        self.deadline = deadline_value
        self.call_limit = call_limit
        self._calls = 0
        self._now_calls = 0
        self._resolution_calls = 0
        self._record_class = None
        self._installed = False
        self._resolution_seconds = None
        self._resolution_nanoseconds = None
        self._clock_implementation = None

    def install(self, linker: Any) -> None:
        """Register exactly now and resolution as host functions on this linker."""
        if self._installed:
            raise RuntimeError("wall-clock bridge is already installed")
        try:
            import wasmtime.component as component_types
        except ImportError as error:
            raise RuntimeError("Wasmtime record support is unavailable") from error
        record_class = getattr(component_types, "Record", None)
        if not isinstance(record_class, type):
            raise RuntimeError("Wasmtime Record support is unavailable")

        with linker.root() as root:
            with root.add_instance(self.import_name) as wall_clock:
                wall_clock.add_func("now", self._now)
                wall_clock.add_func("resolution", self._resolution)
        self._record_class = record_class
        self._installed = True

    def _check_deadline(self) -> None:
        """Require time.monotonic to remain available and before the deadline."""
        try:
            current = time.monotonic()
        except Exception as error:
            raise RuntimeError("monotonic deadline clock failed") from error
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise RuntimeError("monotonic deadline clock returned an invalid value")
        try:
            current_value = float(current)
        except (OverflowError, ValueError) as error:
            raise RuntimeError(
                "monotonic deadline clock returned an invalid value"
            ) from error
        if not math.isfinite(current_value):
            raise RuntimeError("monotonic deadline clock returned an invalid value")
        if current_value >= self.deadline:
            raise TimeoutError("wall-clock callback deadline exceeded")

    def _begin_call(self, name: str) -> None:
        """Enforce the shared call cap and count only admitted callback attempts."""
        if self._calls >= self.call_limit:
            raise RuntimeError("wall-clock callback call limit exceeded")
        self._check_deadline()
        self._calls += 1
        if name == "now":
            self._now_calls += 1
        else:
            self._resolution_calls += 1

    def _make_datetime(self, seconds: int, nanoseconds: int) -> Any:
        """Build a Wasmtime record using its required empty constructor."""
        if self._record_class is None:
            raise RuntimeError("wall-clock bridge is not installed")
        if (
            not isinstance(seconds, int)
            or isinstance(seconds, bool)
            or not 0 <= seconds <= _U64_MAX
            or not isinstance(nanoseconds, int)
            or isinstance(nanoseconds, bool)
            or not 0 <= nanoseconds <= _U32_MAX
        ):
            raise RuntimeError("wall-clock datetime is outside its component type")
        try:
            result = self._record_class()
            result.seconds = seconds
            result.nanoseconds = nanoseconds
        except Exception as error:
            raise RuntimeError(
                "Wasmtime datetime record could not be created"
            ) from error
        return result

    def _now(self, store: Any, *args: Any) -> Any:
        """Return Unix wall time as a checked seconds/nanoseconds record."""
        if args:
            raise RuntimeError("wall-clock now received unexpected parameters")
        self._begin_call("now")
        try:
            timestamp = time.time_ns()
        except Exception as error:
            raise RuntimeError("host wall clock failed") from error
        if (
            not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or timestamp < 0
        ):
            raise RuntimeError("host wall clock returned an invalid Unix timestamp")
        seconds, nanoseconds = divmod(timestamp, _NANOSECONDS)
        result = self._make_datetime(seconds, nanoseconds)
        self._check_deadline()
        return result

    def _resolution(self, store: Any, *args: Any) -> Any:
        """Return host wall-clock resolution rounded up to whole nanoseconds."""
        if args:
            raise RuntimeError("wall-clock resolution received unexpected parameters")
        self._begin_call("resolution")
        try:
            clock_info = time.get_clock_info("time")
            resolution = clock_info.resolution
            implementation = clock_info.implementation
        except Exception as error:
            raise RuntimeError("host wall-clock resolution is unavailable") from error
        if (
            isinstance(resolution, bool)
            or not isinstance(resolution, (int, float))
            or not isinstance(implementation, str)
            or not implementation
        ):
            raise RuntimeError("host wall-clock resolution is invalid")
        try:
            resolution_value = float(resolution)
        except (OverflowError, ValueError) as error:
            raise RuntimeError("host wall-clock resolution is invalid") from error
        if not math.isfinite(resolution_value) or resolution_value <= 0:
            raise RuntimeError("host wall-clock resolution is invalid")
        resolution_scaled = resolution_value * _NANOSECONDS
        if not math.isfinite(resolution_scaled):
            raise RuntimeError("host wall-clock resolution is not representable")
        nanoseconds_total = math.ceil(resolution_scaled)
        if nanoseconds_total <= 0:
            raise RuntimeError("host wall-clock resolution is not representable")
        seconds, nanoseconds = divmod(nanoseconds_total, _NANOSECONDS)
        result = self._make_datetime(seconds, nanoseconds)
        self._check_deadline()
        self._resolution_seconds = resolution_value
        self._resolution_nanoseconds = nanoseconds_total
        self._clock_implementation = implementation
        return result

    def report(self) -> dict:
        """Return bounded CI-host evidence without claiming a production UI link."""
        return {
            "evidence_kind": "ci-host-clock-bridge",
            "import_name": self.import_name,
            "clock_domain": "CI host wall clock",
            "clock_source": _CLOCK_SOURCE,
            "resolution_source": _RESOLUTION_SOURCE,
            "clock_implementation": self._clock_implementation,
            "resolution_seconds": self._resolution_seconds,
            "resolution_nanoseconds_ceiling": self._resolution_nanoseconds,
            "calls": {
                "now": self._now_calls,
                "resolution": self._resolution_calls,
                "total": self._calls,
                "limit": self.call_limit,
            },
            "deadline_enforced": True,
            "linker_installed": self._installed,
            "production_ui_clock_connected": False,
            "production_enabled": False,
        }
