"""Offline fake-Wasmtime tests for the bounded host wall-clock bridge."""

import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from schema_probe import ui_clock


IMPORT_NAME = "wasi:clocks/wall-clock@0.2.0"


class U64:
    """Fake component u64 type marker."""


class U32:
    """Fake component u32 type marker."""


class RecordType:
    def __init__(self, fields):
        self.fields = fields


class FuncType:
    def __init__(self, params, result):
        self.params = params
        self.result = result


class ComponentInstanceType:
    def __init__(self, exports):
        self._exports = exports

    def exports(self, engine):
        if engine != "engine":
            raise AssertionError("unexpected engine")
        return self._exports


class Record:
    __slots__ = ("seconds", "nanoseconds")


def valid_record_type():
    return RecordType(
        [
            ("seconds", U64()),
            ("nanoseconds", U32()),
        ]
    )


def valid_exports():
    datetime = valid_record_type()
    return {
        "datetime": datetime,
        "now": FuncType([], valid_record_type()),
        "resolution": FuncType([], valid_record_type()),
    }


def fake_wasmtime_modules():
    """Model the Wasmtime package with component types only in its submodule."""
    package = types.ModuleType("wasmtime")
    package.__path__ = []
    component_types = types.ModuleType("wasmtime.component")
    component_types.ComponentInstanceType = ComponentInstanceType
    component_types.FuncType = FuncType
    component_types.RecordType = RecordType
    component_types.Record = Record
    component_types.U64 = U64
    component_types.U32 = U32
    return {
        "wasmtime": package,
        "wasmtime.component": component_types,
    }


class _Scope:
    def __init__(self, callback=None):
        self.callback = callback

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False


class _WallClock(_Scope):
    def __init__(self):
        super().__init__()
        self.functions = {}

    def add_func(self, name, callback):
        if name in self.functions:
            raise AssertionError("duplicate function")
        self.functions[name] = callback


class _Root(_Scope):
    def __init__(self, instance):
        super().__init__()
        self.instance = instance
        self.import_name = None

    def add_instance(self, import_name):
        self.import_name = import_name
        return self.instance


class _Linker:
    def __init__(self):
        self.wall_clock = _WallClock()
        self.root_scope = _Root(self.wall_clock)

    def root(self):
        return self.root_scope


class WallClockBridgeTests(unittest.TestCase):
    def test_clock_failure_cannot_be_counted_as_a_nonclock_denial(self):
        budget = ui_clock.ClockBudget(20.0, 1)
        bridge = ui_clock.WallClockBridge(IMPORT_NAME, 20.0, 1, budget=budget)
        linker = _Linker()
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            bridge.install(linker)
        with (
            patch.object(ui_clock.time, "monotonic", return_value=1.0),
            patch.object(
                ui_clock.time, "time_ns", side_effect=OSError("clock unavailable")
            ),
        ):
            with self.assertRaises(RuntimeError):
                linker.wall_clock.functions["now"]("store")
        self.assertFalse(budget.healthy)

        budget = ui_clock.ClockBudget(20.0, 1)
        bridge = ui_clock.WallClockBridge(IMPORT_NAME, 20.0, 1, budget=budget)
        linker = _Linker()
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            bridge.install(linker)
        with (
            patch.object(ui_clock.time, "monotonic", return_value=1.0),
            patch.object(ui_clock.time, "time_ns", return_value=1),
        ):
            linker.wall_clock.functions["now"]("store")
            self.assertTrue(budget.healthy)
            with self.assertRaisesRegex(RuntimeError, "call limit"):
                linker.wall_clock.functions["now"]("store")
            self.assertFalse(budget.healthy)

    def test_valid_import_type_is_accepted_exactly(self):
        item = ComponentInstanceType(valid_exports())
        fake_modules = fake_wasmtime_modules()
        for name in (
            "ComponentInstanceType",
            "FuncType",
            "RecordType",
            "Record",
            "U64",
            "U32",
            "U64Type",
            "U32Type",
        ):
            self.assertFalse(hasattr(fake_modules["wasmtime"], name))
        self.assertFalse(hasattr(fake_modules["wasmtime.component"], "U64Type"))
        self.assertFalse(hasattr(fake_modules["wasmtime.component"], "U32Type"))
        with patch.dict(sys.modules, fake_modules):
            self.assertIsNone(
                ui_clock.validate_clock_import(IMPORT_NAME, item, "engine")
            )

    def test_import_name_is_strict_and_accepts_only_supported_versions(self):
        item = ComponentInstanceType(valid_exports())
        allowed = (
            "wasi:clocks/wall-clock@0.2.0",
            "wasi:clocks/wall-clock@0.2.1",
            "wasi:clocks/wall-clock@0.2.10",
        )
        rejected = (
            "wasi:clocks/wall-clock@0.2.00",
            "wasi:clocks/wall-clock@0.2.",
            "wasi:clocks/monotonic-clock@0.2.0",
            "wasi:clocks/wall-clock@0.3.0",
            "wasi:clocks/wall-clock@0.2.0/extra",
        )
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            for name in allowed:
                with self.subTest(name=name):
                    ui_clock.validate_clock_import(name, item, "engine")
            for name in rejected:
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "import name"):
                        ui_clock.validate_clock_import(name, item, "engine")

    def test_component_exports_and_datetime_field_types_are_allowlisted(self):
        valid = valid_exports()
        cases = [
            {**valid, "extra": FuncType([], valid_record_type())},
            {key: value for key, value in valid.items() if key != "datetime"},
            {
                **valid,
                "datetime": RecordType([("nanoseconds", U32()), ("seconds", U64())]),
            },
            {
                **valid,
                "datetime": RecordType(
                    [
                        ("seconds", U32()),
                        ("nanoseconds", U32()),
                    ]
                ),
            },
            {
                **valid,
                "now": FuncType([("input", U32())], valid_record_type()),
            },
            {
                **valid,
                "resolution": FuncType([], RecordType([("seconds", U64())])),
            },
        ]
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            for exports in cases:
                with self.subTest(exports=set(exports)):
                    item = ComponentInstanceType(exports)
                    with self.assertRaises(ValueError):
                        ui_clock.validate_clock_import(IMPORT_NAME, item, "engine")
            with self.assertRaisesRegex(ValueError, "ComponentInstanceType"):
                ui_clock.validate_clock_import(IMPORT_NAME, object(), "engine")

    def _installed_bridge(self, *, deadline=20.0, call_limit=100_000):
        bridge = ui_clock.WallClockBridge(
            IMPORT_NAME,
            deadline=deadline,
            call_limit=call_limit,
        )
        linker = _Linker()
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            bridge.install(linker)
        return bridge, linker

    def test_linker_receives_only_now_and_resolution(self):
        bridge, linker = self._installed_bridge()
        self.assertEqual(linker.root_scope.import_name, IMPORT_NAME)
        self.assertEqual(set(linker.wall_clock.functions), {"now", "resolution"})
        report = bridge.report()
        self.assertEqual(report["evidence_kind"], "ci-host-clock-bridge")
        self.assertEqual(report["clock_source"], "python.time.time_ns")
        self.assertEqual(report["clock_domain"], "CI host wall clock")
        self.assertFalse(report["production_ui_clock_connected"])
        self.assertFalse(report["production_enabled"])
        self.assertEqual(report["calls"]["total"], 0)
        self.assertNotIn("samples", report)
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            with self.assertRaisesRegex(RuntimeError, "already installed"):
                bridge.install(linker)

    def test_now_converts_real_nanoseconds_to_seconds_and_nanoseconds(self):
        bridge, linker = self._installed_bridge(deadline=10.0, call_limit=3)
        with (
            patch.object(ui_clock.time, "monotonic", side_effect=[1.0, 1.1]),
            patch.object(ui_clock.time, "time_ns", return_value=2_345_000_007),
        ):
            value = linker.wall_clock.functions["now"]("store")
        self.assertEqual((value.seconds, value.nanoseconds), (2, 345_000_007))
        self.assertEqual(
            bridge.report()["calls"],
            {
                "now": 1,
                "resolution": 0,
                "total": 1,
                "limit": 3,
            },
        )

    def test_resolution_rounds_up_to_nanoseconds_and_reports_precision(self):
        bridge, linker = self._installed_bridge(deadline=10.0)
        clock_info = types.SimpleNamespace(
            resolution=1.1e-9,
            implementation="fixture host clock",
        )
        with (
            patch.object(ui_clock.time, "monotonic", side_effect=[1.0, 1.1]),
            patch.object(
                ui_clock.time,
                "get_clock_info",
                return_value=clock_info,
            ) as info,
        ):
            value = linker.wall_clock.functions["resolution"]("store")
        info.assert_called_once_with("time")
        self.assertEqual((value.seconds, value.nanoseconds), (0, 2))
        report = bridge.report()
        self.assertEqual(
            report["resolution_source"],
            "python.time.get_clock_info('time').resolution",
        )
        self.assertEqual(report["resolution_seconds"], 1.1e-9)
        self.assertEqual(report["resolution_nanoseconds_ceiling"], 2)
        self.assertEqual(report["clock_implementation"], "fixture host clock")

    def test_invalid_wall_clock_values_and_failures_raise_without_fallback(self):
        cases = (
            -1,
            True,
            None,
            "now",
            ((_UINT64_MAX + 1) * 1_000_000_000),
        )
        for sample in cases:
            with self.subTest(sample=sample):
                bridge, linker = self._installed_bridge(deadline=10.0)
                with (
                    patch.object(ui_clock.time, "monotonic", return_value=1.0),
                    patch.object(ui_clock.time, "time_ns", return_value=sample),
                ):
                    with self.assertRaises(RuntimeError):
                        linker.wall_clock.functions["now"]("store")
        bridge, linker = self._installed_bridge(deadline=10.0)
        with (
            patch.object(ui_clock.time, "monotonic", return_value=1.0),
            patch.object(
                ui_clock.time,
                "time_ns",
                side_effect=OSError("clock failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "host wall clock failed"):
                linker.wall_clock.functions["now"]("store")
        self.assertEqual(bridge.report()["calls"]["now"], 1)

    def test_invalid_resolutions_and_clock_info_fail_without_zero_result(self):
        values = (0.0, -1.0, math.inf, math.nan, True, None)
        for resolution in values:
            with self.subTest(resolution=resolution):
                _, linker = self._installed_bridge(deadline=10.0)
                clock_info = types.SimpleNamespace(
                    resolution=resolution,
                    implementation="fixture host clock",
                )
                with (
                    patch.object(ui_clock.time, "monotonic", return_value=1.0),
                    patch.object(
                        ui_clock.time,
                        "get_clock_info",
                        return_value=clock_info,
                    ),
                ):
                    with self.assertRaises(RuntimeError):
                        linker.wall_clock.functions["resolution"]("store")
        _, linker = self._installed_bridge(deadline=10.0)
        with (
            patch.object(ui_clock.time, "monotonic", return_value=1.0),
            patch.object(
                ui_clock.time,
                "get_clock_info",
                side_effect=OSError("no clock info"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "resolution is unavailable"):
                linker.wall_clock.functions["resolution"]("store")

    def test_deadline_is_checked_before_and_after_each_callback(self):
        _, linker = self._installed_bridge(deadline=5.0)
        with (
            patch.object(ui_clock.time, "monotonic", return_value=5.0),
            patch.object(ui_clock.time, "time_ns", side_effect=AssertionError),
        ):
            with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
                linker.wall_clock.functions["now"]("store")

        bridge, linker = self._installed_bridge(deadline=5.0)
        with (
            patch.object(ui_clock.time, "monotonic", side_effect=[1.0, 5.0]),
            patch.object(ui_clock.time, "time_ns", return_value=1_000_000_000),
        ):
            with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
                linker.wall_clock.functions["now"]("store")
        self.assertEqual(bridge.report()["calls"]["now"], 1)

    def test_call_limit_and_constructor_bounds_are_enforced(self):
        bridge, linker = self._installed_bridge(deadline=10.0, call_limit=1)
        with (
            patch.object(ui_clock.time, "monotonic", side_effect=[1.0, 1.1]),
            patch.object(ui_clock.time, "time_ns", return_value=1),
        ):
            linker.wall_clock.functions["now"]("store")
        with patch.object(ui_clock.time, "get_clock_info", side_effect=AssertionError):
            with self.assertRaisesRegex(RuntimeError, "call limit"):
                linker.wall_clock.functions["resolution"]("store")
        self.assertEqual(bridge.report()["calls"]["total"], 1)

        for deadline, call_limit in (
            (math.inf, 1),
            (True, 1),
            (10.0, 0),
            (10.0, True),
            (10.0, 100_001),
        ):
            with self.subTest(deadline=deadline, call_limit=call_limit):
                with self.assertRaises(ValueError):
                    ui_clock.WallClockBridge(
                        IMPORT_NAME,
                        deadline=deadline,
                        call_limit=call_limit,
                    )


_UINT64_MAX = (1 << 64) - 1


if __name__ == "__main__":
    unittest.main()
