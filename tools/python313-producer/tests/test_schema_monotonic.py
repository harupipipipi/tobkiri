"""Offline fake-Wasmtime tests for shared wall/monotonic clock budgets."""

import importlib
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

ui_clock = importlib.import_module("schema_probe.ui_clock")
ui_monotonic = importlib.import_module("schema_probe.ui_monotonic")


IMPORT_NAME = "tobkiri-ui:clock/monotonic@1.0.0"
WALL_IMPORT_NAME = "wasi:clocks/wall-clock@0.2.0"
U64_MAX = (1 << 64) - 1
DEFAULT_DEADLINE = 20.0


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


def valid_monotonic_exports():
    return {
        "now": FuncType([], U64()),
        "resolution": FuncType([], U64()),
    }


def valid_wall_exports():
    datetime = RecordType([("seconds", U64()), ("nanoseconds", U32())])
    return {
        "datetime": datetime,
        "now": FuncType([], datetime),
        "resolution": FuncType([], datetime),
    }


def fake_wasmtime_modules():
    """Match Wasmtime 48's real package/component type export layout."""
    package = types.ModuleType("wasmtime")
    package.__path__ = []
    component_types = types.ModuleType("wasmtime.component")
    component_types.ComponentInstanceType = ComponentInstanceType
    component_types.FuncType = FuncType
    component_types.RecordType = RecordType
    component_types.Record = Record
    component_types.U64 = U64
    component_types.U32 = U32
    return {"wasmtime": package, "wasmtime.component": component_types}


class _Scope:
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False


class _Clock(_Scope):
    def __init__(self):
        self.functions = {}

    def add_func(self, name, callback):
        if name in self.functions:
            raise AssertionError("duplicate function")
        self.functions[name] = callback


class _Root(_Scope):
    def __init__(self, instance):
        self.instance = instance
        self.import_name = None

    def add_instance(self, import_name):
        self.import_name = import_name
        return self.instance


class _Linker:
    def __init__(self):
        self.clock = _Clock()
        self.root_scope = _Root(self.clock)

    def root(self):
        return self.root_scope


def _installed_bridge(*, budget=None, deadline=DEFAULT_DEADLINE):
    if budget is None:
        budget = ui_clock.ClockBudget(deadline=deadline)
    bridge = ui_monotonic.MonotonicClockBridge(IMPORT_NAME, budget)
    linker = _Linker()
    with patch.dict(sys.modules, fake_wasmtime_modules()):
        bridge.install(linker)
    return bridge, linker, budget


class MonotonicImportTypeTests(unittest.TestCase):
    def test_exact_import_and_u64_exports_are_accepted(self):
        item = ComponentInstanceType(valid_monotonic_exports())
        modules = fake_wasmtime_modules()
        for name in (
            "ComponentInstanceType",
            "FuncType",
            "U64",
            "U64Type",
        ):
            self.assertFalse(hasattr(modules["wasmtime"], name))
        self.assertFalse(hasattr(modules["wasmtime.component"], "U64Type"))
        with patch.dict(sys.modules, modules):
            self.assertIsNone(
                ui_monotonic.validate_clock_import(IMPORT_NAME, item, "engine")
            )

    def test_other_names_versions_and_extra_exports_are_rejected(self):
        valid = valid_monotonic_exports()
        bad_names = (
            "tobkiri-ui:clock/monotonic@1.0",
            "tobkiri-ui:clock/monotonic@1.0.00",
            "tobkiri-ui:clock/monotonic@1.0.0/extra",
            "tobkiri-ui:clock/wall@1.0.0",
            "wasi:clocks/monotonic@1.0.0",
        )
        invalid_exports = (
            {**valid, "extra": FuncType([], U64())},
            {key: value for key, value in valid.items() if key != "now"},
            {**valid, "now": FuncType([("input", U64())], U64())},
            {**valid, "resolution": FuncType([], U32())},
            {**valid, "now": object()},
        )
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            for name in bad_names:
                with self.subTest(import_name=name):
                    with self.assertRaises(ValueError):
                        ui_monotonic.validate_clock_import(
                            name, ComponentInstanceType(valid), "engine"
                        )
            for exports in invalid_exports:
                with self.subTest(exports=set(exports)):
                    with self.assertRaises(ValueError):
                        ui_monotonic.validate_clock_import(
                            IMPORT_NAME, ComponentInstanceType(exports), "engine"
                        )
            with self.assertRaises(ValueError):
                ui_monotonic.validate_clock_import(IMPORT_NAME, object(), "engine")


class MonotonicClockBridgeTests(unittest.TestCase):
    def test_linker_gets_only_now_and_resolution(self):
        bridge, linker, budget = _installed_bridge()
        self.assertEqual(linker.root_scope.import_name, IMPORT_NAME)
        self.assertEqual(set(linker.clock.functions), {"now", "resolution"})
        report = bridge.report()
        self.assertEqual(report["clock_source"], "python.time.monotonic_ns")
        self.assertFalse(report["production_ui_clock_connected"])
        self.assertEqual(report["calls"], {"now": 0, "resolution": 0, "total": 0})
        self.assertEqual(
            report["shared_budget"],
            {"total": 0, "limit": 100_000, "deadline_enforced": True},
        )
        self.assertEqual(
            budget.report(),
            {"total": 0, "limit": 100_000, "deadline_enforced": True},
        )
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            with self.assertRaisesRegex(RuntimeError, "already installed"):
                bridge.install(linker)

    def test_now_returns_real_monotonic_nanoseconds_as_u64(self):
        bridge, linker, budget = _installed_bridge()
        with (
            patch.object(ui_monotonic.time, "monotonic", side_effect=[1.0, 1.1]),
            patch.object(ui_monotonic.time, "monotonic_ns", return_value=1_234_567_890),
        ):
            value = linker.clock.functions["now"]("store")
        self.assertEqual(value, 1_234_567_890)
        self.assertIsInstance(value, int)
        self.assertEqual(
            bridge.report()["calls"],
            {"now": 1, "resolution": 0, "total": 1},
        )
        self.assertEqual(budget.report()["total"], 1)

    def test_resolution_ceil_and_source_properties_are_reported(self):
        bridge, linker, budget = _installed_bridge()
        info = types.SimpleNamespace(
            resolution=1.1e-9,
            monotonic=True,
            adjustable=False,
            implementation="fixture monotonic clock",
        )
        with (
            patch.object(ui_monotonic.time, "monotonic", side_effect=[1.0, 1.1]),
            patch.object(
                ui_monotonic.time, "get_clock_info", return_value=info
            ) as get_info,
        ):
            value = linker.clock.functions["resolution"]("store")
        get_info.assert_called_once_with("monotonic")
        self.assertEqual(value, 2)
        self.assertEqual(
            bridge.report()["resolution_nanoseconds_ceiling"],
            2,
        )
        self.assertEqual(
            bridge.report()["resolution_source"],
            "python.time.get_clock_info('monotonic').resolution",
        )
        self.assertEqual(
            bridge.report()["clock_implementation"],
            "fixture monotonic clock",
        )
        self.assertEqual(
            bridge.report()["calls"],
            {"now": 0, "resolution": 1, "total": 1},
        )
        self.assertEqual(budget.report()["total"], 1)

    def test_valid_now_values_never_regress(self):
        bridge, linker, budget = _installed_bridge()
        with (
            patch.object(
                ui_monotonic.time,
                "monotonic",
                side_effect=[1.0, 1.1, 1.2, 1.3, 1.4],
            ),
            patch.object(ui_monotonic.time, "monotonic_ns", side_effect=[10, 10, 9]),
        ):
            self.assertEqual(linker.clock.functions["now"]("store"), 10)
            self.assertEqual(linker.clock.functions["now"]("store"), 10)
            with self.assertRaisesRegex(RuntimeError, "decreasing"):
                linker.clock.functions["now"]("store")
        self.assertEqual(bridge.report()["calls"]["total"], 3)
        self.assertEqual(budget.report()["total"], 3)

    def test_invalid_now_values_and_os_failure_have_no_fallback(self):
        invalid_values = (-1, True, U64_MAX + 1, 1.0, None, "time")
        for sample in invalid_values:
            with self.subTest(value=sample):
                _, linker, _ = _installed_bridge()
                with (
                    patch.object(ui_monotonic.time, "monotonic", return_value=1.0),
                    patch.object(
                        ui_monotonic.time, "monotonic_ns", return_value=sample
                    ),
                ):
                    with self.assertRaises(RuntimeError):
                        linker.clock.functions["now"]("store")

        bridge, linker, budget = _installed_bridge()
        with (
            patch.object(ui_monotonic.time, "monotonic", return_value=1.0),
            patch.object(
                ui_monotonic.time,
                "monotonic_ns",
                side_effect=OSError("monotonic clock unavailable"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                linker.clock.functions["now"]("store")
        self.assertEqual(bridge.report()["calls"]["now"], 1)
        self.assertEqual(budget.report()["total"], 1)

    def test_invalid_clock_info_and_os_failure_have_no_fallback(self):
        invalid_infos = (
            types.SimpleNamespace(
                resolution=0.0,
                monotonic=True,
                adjustable=False,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=-1.0,
                monotonic=True,
                adjustable=False,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=math.inf,
                monotonic=True,
                adjustable=False,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=math.nan,
                monotonic=True,
                adjustable=False,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=20_000_000_000.0,
                monotonic=True,
                adjustable=False,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=True,
                monotonic=True,
                adjustable=False,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=1e-9,
                monotonic=False,
                adjustable=False,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=1e-9,
                monotonic=True,
                adjustable=True,
                implementation="clock",
            ),
            types.SimpleNamespace(
                resolution=1e-9,
                monotonic=True,
                adjustable=False,
                implementation="",
            ),
        )
        for info in invalid_infos:
            with self.subTest(info=info):
                _, linker, _ = _installed_bridge()
                with (
                    patch.object(ui_monotonic.time, "monotonic", return_value=1.0),
                    patch.object(
                        ui_monotonic.time, "get_clock_info", return_value=info
                    ),
                ):
                    with self.assertRaises(RuntimeError):
                        linker.clock.functions["resolution"]("store")

        _, linker, _ = _installed_bridge()
        with (
            patch.object(ui_monotonic.time, "monotonic", return_value=1.0),
            patch.object(
                ui_monotonic.time,
                "get_clock_info",
                side_effect=OSError("clock metadata unavailable"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                linker.clock.functions["resolution"]("store")

    def test_deadline_is_checked_before_and_after_callback(self):
        _, linker, _ = _installed_bridge(deadline=5.0)
        with (
            patch.object(ui_monotonic.time, "monotonic", return_value=5.0),
            patch.object(ui_monotonic.time, "monotonic_ns", side_effect=AssertionError),
        ):
            with self.assertRaises(TimeoutError):
                linker.clock.functions["now"]("store")

        bridge, linker, budget = _installed_bridge(deadline=5.0)
        with (
            patch.object(ui_monotonic.time, "monotonic", side_effect=[4.0, 5.0]),
            patch.object(ui_monotonic.time, "monotonic_ns", return_value=123),
        ):
            with self.assertRaises(TimeoutError):
                linker.clock.functions["now"]("store")
        self.assertEqual(bridge.report()["calls"]["total"], 1)
        self.assertEqual(budget.report()["total"], 1)

    def test_shared_call_limit_applies_across_wall_and_monotonic_imports(self):
        deadline = 10.0
        budget = ui_clock.ClockBudget(deadline=deadline, call_limit=1)
        wall = ui_clock.WallClockBridge(
            WALL_IMPORT_NAME,
            deadline=deadline,
            call_limit=1,
            budget=budget,
        )
        monotonic = ui_monotonic.MonotonicClockBridge(IMPORT_NAME, budget)
        wall_linker = _Linker()
        monotonic_linker = _Linker()
        with patch.dict(sys.modules, fake_wasmtime_modules()):
            wall.install(wall_linker)
            monotonic.install(monotonic_linker)

        with (
            patch.object(ui_clock.time, "monotonic", side_effect=[1.0, 1.1]),
            patch.object(ui_clock.time, "time_ns", return_value=2_000_000_000),
        ):
            wall_value = wall_linker.clock.functions["now"]("store")
        self.assertEqual((wall_value.seconds, wall_value.nanoseconds), (2, 0))

        with (
            patch.object(ui_monotonic.time, "monotonic", side_effect=AssertionError),
            patch.object(ui_monotonic.time, "monotonic_ns", side_effect=AssertionError),
        ):
            with self.assertRaisesRegex(RuntimeError, "call limit"):
                monotonic_linker.clock.functions["now"]("store")

        self.assertEqual(
            budget.report(),
            {"total": 1, "limit": 1, "deadline_enforced": True},
        )
        self.assertEqual(wall.report()["shared_budget"]["total"], 1)
        self.assertEqual(monotonic.report()["shared_budget"]["total"], 1)

    def test_unexpected_callback_arguments_and_invalid_budget_are_rejected(self):
        _, linker, _ = _installed_bridge()
        with self.assertRaises(RuntimeError):
            linker.clock.functions["now"]("store", 1)
        with self.assertRaises(RuntimeError):
            linker.clock.functions["resolution"]("store", 1)

        for deadline, call_limit in (
            (math.inf, 1),
            (True, 1),
            (10.0, 0),
            (10.0, True),
            (10.0, 100_001),
        ):
            with self.subTest(deadline=deadline, call_limit=call_limit):
                with self.assertRaises(ValueError):
                    ui_clock.ClockBudget(deadline=deadline, call_limit=call_limit)


if __name__ == "__main__":
    unittest.main()
