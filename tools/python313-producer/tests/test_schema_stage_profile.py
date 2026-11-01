"""Small source-level checks for the opt-in schema stage profiler."""

import ctypes
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "schema_probe"))

import build_schema  # noqa: E402
import stage_profile as profile  # noqa: E402


class SchemaStageProfileTests(unittest.TestCase):
    def test_derivation_is_exact_bounded_and_leaves_frozen_sources_untouched(self):
        names = ("app.py", "exercise.py", "captured_loader.py", "probe.wit")
        paths = {name: HERE / "schema_probe" / name for name in names}
        baseline = {name: path.read_bytes() for name, path in paths.items()}
        helper = (HERE / "schema_probe/stage_profile.py").read_bytes()

        derived, provenance = profile.derive_stage_profile_sources(baseline, helper)

        self.assertEqual(set(derived), set(names))
        self.assertEqual(
            {name: path.read_bytes() for name, path in paths.items()}, baseline
        )
        self.assertTrue(provenance["diagnostic_only"])
        self.assertFalse(provenance["original_acceptance_claimed"])
        self.assertFalse(provenance["production_enabled"])
        self.assertEqual(set(provenance["files"]), set(names))
        for name in names:
            record = provenance["files"][name]
            self.assertEqual(
                record["baseline"]["sha256"], hashlib.sha256(baseline[name]).hexdigest()
            )
            self.assertEqual(
                record["derived"]["sha256"], hashlib.sha256(derived[name]).hexdigest()
            )
            self.assertEqual(
                record["diff_sha256"],
                hashlib.sha256(record["diff"].encode("utf-8")).hexdigest(),
            )
        self.assertNotIn(b"diagnostic-profile", baseline["probe.wit"])
        self.assertEqual(derived["probe.wit"].count(b"diagnostic-profile"), 2)
        self.assertLess(
            derived["app.py"].index(b"import stage_profile"),
            derived["app.py"].index(b"from captured_loader import install"),
        )
        self.assertIn(
            b"finally:\n                stage_profile.disarm()", derived["app.py"]
        )
        for name in ("app.py", "exercise.py", "captured_loader.py"):
            compile(derived[name].decode("utf-8"), "derived-" + name, "exec")
        self.assertLess(provenance["bounds"]["events"], 2048)
        self.assertLessEqual(provenance["bounds"]["metadata_bytes"], 1024 * 1024)

    def test_emitter_is_disabled_until_exported_run_arms_it(self):
        spec = importlib.util.spec_from_file_location(
            "fresh_schema_stage_profile", HERE / "schema_probe/stage_profile.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        module.checkpoint("preinit", "ignored")
        module.set_callback(lambda *args: calls.append(args))
        module.checkpoint("pre-arm", "ignored")
        self.assertEqual(calls, [])
        module.arm()
        module.checkpoint("import.begin", "jsonschema.protocols")
        module.disarm()
        module.checkpoint("post-run", "ignored")
        self.assertEqual(calls, [("import.begin", "jsonschema.protocols")])
        with self.assertRaises(RuntimeError):
            module.arm()

    def test_profile_helper_loads_from_exact_path_under_isolated_python(self):
        source_path = HERE / "schema_probe/stage_profile.py"
        driver_path = HERE / "schema_probe/driver.py"
        with tempfile.TemporaryDirectory() as temporary:
            helper_path = Path(temporary) / "captured_profile.py"
            helper_path.write_bytes(source_path.read_bytes())
            script = """
import importlib.util
from pathlib import Path
import sys
driver_path, helper_path = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('captured_driver', driver_path)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
before = sys.path[:]
source = helper_path.read_bytes()
module = driver.load_stage_profile(helper_path, source)
assert module.PROFILE_IMPORT_NAME == 'tobkiri-ui:schema-probe/diagnostic-profile@1.0.0'
assert sys.path == before
"""
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    script,
                    str(driver_path),
                    str(helper_path),
                ],
                check=True,
                timeout=10,
            )

    def test_import_contract_requires_exact_component_shape(self):
        class String:
            pass

        class FuncType:
            def __init__(self, params, result):
                self.params = params
                self.result = result

        class ComponentInstanceType:
            def __init__(self, exports):
                self._exports = exports

            def exports(self, engine):
                return self._exports

        component = types.ModuleType("wasmtime.component")
        component.String = String
        component.FuncType = FuncType
        component.ComponentInstanceType = ComponentInstanceType
        package = types.ModuleType("wasmtime")
        package.__path__ = []
        package.component = component
        with patch.dict(
            sys.modules, {"wasmtime": package, "wasmtime.component": component}
        ):
            item = ComponentInstanceType(
                {"checkpoint": FuncType([("phase", String()), ("key", String())], None)}
            )
            profile.validate_import(profile.PROFILE_IMPORT_NAME, item, object())
            for bad_name in (
                "tobkiri-ui:schema-probe/other@1.0.0",
                profile.PROFILE_IMPORT_NAME,
            ):
                bad_item = ComponentInstanceType(
                    {"other": FuncType([("phase", String()), ("key", String())], None)}
                )
                with self.assertRaises(ValueError):
                    profile.validate_import(bad_name, bad_item, object())
            wrong = ComponentInstanceType(
                {"checkpoint": FuncType([("wrong", String()), ("key", String())], None)}
            )
            with self.assertRaises(ValueError):
                profile.validate_import(profile.PROFILE_IMPORT_NAME, wrong, object())

    def test_fuel_adapter_uses_borrowed_context_and_rejects_callback_overflow(self):
        observed_contexts = []
        fuel = [profile.MAX_FUEL - i for i in range(profile.MAX_EVENTS + 1)]
        ffi = types.ModuleType("wasmtime._ffi")

        def get_fuel(context, output):
            observed_contexts.append(context)
            ctypes.cast(
                output, ctypes.POINTER(ctypes.c_uint64)
            ).contents.value = fuel.pop(0)
            return None

        ffi.wasmtime_context_get_fuel = get_fuel

        class WasmtimeError(Exception):
            @classmethod
            def _from_ptr(cls, pointer):
                return cls("unexpected FFI error")

        package = types.ModuleType("wasmtime")
        package.__path__ = []
        package._ffi = ffi
        package.WasmtimeError = WasmtimeError

        class StoreContext:
            def __init__(self):
                self.context = object()

            def _context(self):
                return self.context

            def get_fuel(self):
                raise AssertionError("borrowed StoreContext has no public get_fuel API")

        store_context = StoreContext()

        class Budget:
            calls = 0
            completed = 0

            def begin(self):
                self.calls += 1

            def complete(self):
                self.completed += 1

            @property
            def healthy(self):
                return self.calls == self.completed

            def report(self):
                return {"total": self.calls, "limit": 100_000}

        budget = Budget()
        with patch.dict(sys.modules, {"wasmtime": package, "wasmtime._ffi": ffi}):
            self.assertEqual(
                profile.read_remaining_fuel(store_context), profile.MAX_FUEL
            )
            bridge = profile.StageProfileBridge(budget)
            bridge._installed = True
            for index in range(profile.MAX_EVENTS):
                bridge._checkpoint(store_context, "compile.before", str(index))
            self.assertEqual(
                bridge.report("running")["get_code_call_count"], profile.MAX_EVENTS
            )
            with self.assertRaisesRegex(ValueError, "event limit"):
                bridge._checkpoint(store_context, "compile.before", "overflow")
            fuel.append(profile.MAX_FUEL - profile.MAX_EVENTS - 1)
            metadata_limited = profile.StageProfileBridge(budget)
            metadata_limited._installed = True
            metadata_limited._metadata_bytes = profile.MAX_EVENT_BYTES - 1
            with self.assertRaisesRegex(ValueError, "metadata limit"):
                metadata_limited._checkpoint(store_context, "format.begin", "json")

        self.assertTrue(
            all(context is store_context.context for context in observed_contexts)
        )
        self.assertFalse(hasattr(bridge, "_context"))
        self.assertTrue(bridge.report("failed")["fuel_observations_monotonic"])
        self.assertLessEqual(
            bridge.report("failed")["metadata_bytes"], profile.MAX_EVENT_BYTES
        )
        encoded = json.dumps(bridge.report("failed"), separators=(",", ":")).encode()
        self.assertLessEqual(len(encoded), profile.MAX_METADATA_BYTES)

    def test_controller_gate_is_default_off_and_requires_all_candidate_inputs(self):
        common = dict(
            allow_ui_wall_clock=True,
            allow_ui_monotonic_clock=True,
            allow_inactive_ui_libc=True,
            allow_ci_link_trace=True,
            predecessor_paths=(Path("a"), Path("b"), Path("c"), Path("d")),
        )
        build_schema._validate_schema_stage_profile(False, **common)
        build_schema._validate_schema_stage_profile(True, **common)
        with self.assertRaises(ValueError):
            build_schema._validate_schema_stage_profile(
                True,
                **{
                    **common,
                    "predecessor_paths": (Path("a"), None, Path("c"), Path("d")),
                },
            )
        with self.assertRaises(ValueError):
            build_schema._validate_schema_stage_profile(
                True, **{**common, "allow_ci_link_trace": False}
            )
        driver_source = (HERE / "schema_probe/driver.py").read_text()
        controller_source = (HERE / "build_schema.py").read_text()
        self.assertIn(
            "diagnostic-only schema stage profile never accepts", driver_source
        )
        self.assertIn(
            'parser.add_argument("--schema-stage-profile", action="store_true")',
            controller_source,
        )
        workflow = (
            HERE.parents[1] / ".github/workflows/issue-1520-ui-stage-profile.yml"
        ).read_text()
        self.assertIn("--schema-stage-profile", workflow)
        self.assertIn("if: always()", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertNotIn("outer.wasm", workflow)


if __name__ == "__main__":
    unittest.main()
