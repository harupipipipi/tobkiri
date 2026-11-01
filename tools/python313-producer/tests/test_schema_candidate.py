"""Offline candidate envelopes; no compiler, real wheel, or guest execution."""

import ast
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import build_schema
import fetch_schema_stdlib

before = sys.path[:]
spec = importlib.util.spec_from_file_location(
    "schema_driver", HERE / "schema_probe/driver.py"
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
sys.path[:] = before


class SchemaCandidateTests(unittest.TestCase):
    def test_monotonic_composition_preserves_the_probe_and_closes_both_imports(self):
        original = (HERE / "schema_probe/probe.wit").read_bytes()
        from schema_probe import ui_monotonic

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(sys.modules, {"ui_monotonic": ui_monotonic}),
        ):
            stage = Path(directory)
            (stage / "probe.wit").write_bytes(original)
            wit = driver.clock_composition_wit(
                stage, "wasi:clocks/wall-clock@0.2.4", allow_monotonic=True
            )
            self.assertEqual((stage / "probe.wit").read_bytes(), original)
            self.assertIn(
                "  import " + ui_monotonic.IMPORT_NAME + ";",
                (wit / "probe.wit").read_text(),
            )
            dependency = (wit / "deps/tobkiri-ui-clock/monotonic.wit").read_text()
            self.assertIn("package tobkiri-ui:clock@1.0.0;", dependency)
            self.assertNotIn("subscribe", dependency)
            self.assertNotIn("poll", dependency)

    def test_monotonic_boundary_requires_both_explicit_modes_and_only_two_imports(self):
        wall = "wasi:clocks/wall-clock@0.2.4"
        mono = "tobkiri-ui:clock/monotonic@1.0.0"
        observed = []

        def component(imports):
            return SimpleNamespace(
                type=SimpleNamespace(
                    imports=lambda engine: imports,
                    exports=lambda engine: {driver.INTERFACE: object()},
                )
            )

        with patch.dict(
            sys.modules,
            {
                "ui_clock": SimpleNamespace(
                    validate_clock_import=lambda name, *_: observed.append(name)
                ),
                "ui_monotonic": SimpleNamespace(
                    IMPORT_NAME=mono,
                    validate_clock_import=lambda name, *_: observed.append(name),
                ),
            },
        ):
            result = driver.boundary(
                component({wall: object(), mono: object()}),
                None,
                inner=False,
                allow_clock=True,
                allow_monotonic=True,
            )
            self.assertEqual(result["imports"], sorted([wall, mono]))
            self.assertEqual(set(observed), {wall, mono})
            for imports in (
                {wall: object()},
                {wall: object(), mono: object(), "extra": object()},
            ):
                with self.assertRaisesRegex(ValueError, "two explicit"):
                    driver.boundary(
                        component(imports),
                        None,
                        inner=False,
                        allow_clock=True,
                        allow_monotonic=True,
                    )
            with self.assertRaisesRegex(ValueError, "explicit wall-clock"):
                driver.boundary(
                    component({mono: object()}),
                    None,
                    inner=False,
                    allow_monotonic=True,
                )

    def test_clock_composition_declares_actual_interface_without_rewriting_probe(self):
        spec = importlib.util.spec_from_file_location(
            "schema_composition", HERE.parents[1] / "sdk/python/composition.py"
        )
        composition = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(composition)
        original = (HERE / "schema_probe/probe.wit").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "probe.wit").write_bytes(original)
            wit = driver.clock_composition_wit(stage, "wasi:clocks/wall-clock@0.2.6")
            self.assertEqual((stage / "probe.wit").read_bytes(), original)
            self.assertIn(
                "import wasi:clocks/wall-clock@0.2.6;", (wit / "probe.wit").read_text()
            )
            names, _ = composition.world_export_names_from_path(wit, "ui-schema-probe")
            self.assertEqual(names, [driver.INTERFACE])

    def test_clock_mode_never_accepts_extra_authority_or_private_exports(self):
        def component(imports, exports):
            return SimpleNamespace(
                type=SimpleNamespace(
                    imports=lambda engine: imports, exports=lambda engine: exports
                )
            )

        self.assertEqual(
            driver.boundary(
                component({}, {driver.INTERFACE: object()}), None, inner=False
            )["imports"],
            [],
        )
        with self.assertRaisesRegex(ValueError, "authority"):
            driver.boundary(
                component(
                    {"wasi:filesystem/types@0.2.6": object()},
                    {driver.INTERFACE: object()},
                ),
                None,
                inner=False,
            )
        with self.assertRaisesRegex(ValueError, "exports"):
            driver.boundary(
                component({}, {driver.INTERFACE: object(), "exports": object()}),
                None,
                inner=False,
            )
        with patch.dict(
            sys.modules,
            {
                "ui_clock": SimpleNamespace(
                    validate_clock_import=lambda *args: self.fail(
                        "must reject extra imports first"
                    )
                )
            },
        ):
            for imports in (
                {},
                {"wasi:clocks/wall-clock@0.2.6": object(), "extra": object()},
            ):
                with (
                    self.subTest(imports=list(imports)),
                    self.assertRaisesRegex(ValueError, "only one"),
                ):
                    driver.boundary(
                        component(imports, {driver.INTERFACE: object()}),
                        None,
                        inner=False,
                        allow_clock=True,
                    )

    def test_controller_and_guest_source_selection_are_identical(self):
        self.assertEqual(driver.STDLIB_FILES, set(fetch_schema_stdlib.SELECTED_FILES))

    def test_non_ci_refusal_precedes_tools_and_destination_access(self):
        missing = Path("/must-not-be-created")
        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict("os.environ", {}, clear=True),
        ):
            with self.assertRaisesRegex(ValueError, "CI"):
                build_schema.run(missing, missing, missing, missing, missing, missing)

    def test_empty_members_are_preserved_but_empty_components_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "py.typed"
            path.write_bytes(b"")
            self.assertEqual(driver.read(path, allow_empty=True), b"")
            with self.assertRaises(ValueError):
                driver.read(path)

    def test_stage_keeps_literal_originals_without_copying_native_binary(self):
        captured, pins = {}, []
        for index in range(6):
            native = index == 5
            distribution = "rpds-py" if native else f"package{index}"
            prefix = "rpds" if native else distribution
            root = "rpds-py-native" if native else distribution
            data = {f"{prefix}/__init__.py": b"# original\n", f"{prefix}/py.typed": b""}
            if native:
                data["rpds/native.so"] = b"native fixture; never executed"
            else:
                data[f"{distribution}-1.dist-info/METADATA"] = b"Name: fixture\n"
            files = {}
            for name, raw in data.items():
                captured[f"/inputs/packages/{root}/{name}"] = raw
                files[name] = {
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            pins.append(
                {
                    "distribution": distribution,
                    "role": "native-oracle" if native else "pure",
                    "files": files,
                    "sha256": "ab" * 32,
                }
            )
        extension = b"Wasm fixture; never executed"
        captured["/rpds.abi3.so"] = extension
        for name in ("app.py", "exercise.py", "captured_loader.py", "probe.wit"):
            captured["/probe-source/" + name] = b"# probe\n"
        stdlib_files = {}
        for name in sorted(driver.STDLIB_FILES):
            raw = b"# original CPython " + name.encode() + b"\n"
            captured["/stdlib/source/" + name] = raw
            stdlib_files[name] = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": "0o644",
            }
        control = {
            "inputs": pins,
            "extension_sha256": hashlib.sha256(extension).hexdigest(),
            "input_limit": 64 * 1024 * 1024,
            "stdlib": {
                "format": "tobkiri.python313-stdlib-inputs/1",
                "outcome": "success",
                "source_pin": {"sha256": "cd" * 32},
                "archive": {"sha256": "cd" * 32, "bytes": 1},
                "files": stdlib_files,
            },
        }
        original_read = driver.read

        def read(path, *args, **kwargs):
            return (
                captured[str(path)]
                if str(path) in captured
                else original_read(path, *args, **kwargs)
            )

        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            with patch.object(driver, "read", side_effect=read):
                receipt = driver.stage_inputs(stage, control)
            self.assertFalse((stage / "rpds/native.so").exists())
            self.assertEqual((stage / "rpds/rpds.abi3.so").read_bytes(), extension)
            self.assertEqual(receipt["wrapper"]["wheel_sha256"], "ab" * 32)
            source = ast.parse((stage / "wheel_data.py").read_bytes())
            self.assertEqual(len(source.body), 2)
            values = ast.literal_eval(source.body[0].value)
            self.assertEqual(values["rpds/__init__.py"], b"# original\n")
            self.assertEqual(values["package0/py.typed"], b"")
            for name in driver.STDLIB_FILES:
                self.assertEqual(values[name], captured["/stdlib/source/" + name])
                self.assertEqual((stage / name).read_bytes(), values[name])
            self.assertEqual(
                receipt["stdlib_source"],
                {
                    key: control["stdlib"][key]
                    for key in ("source_pin", "archive", "files")
                },
            )
            with patch.object(
                driver, "read", side_effect=AssertionError("must preflight")
            ):
                with self.assertRaisesRegex(ValueError, "bound"):
                    driver.stage_inputs(stage, {**control, "input_limit": 1})
            package_bytes = sum(
                row["bytes"] + len(name.encode()) + 128
                for pin in pins
                for name, row in pin["files"].items()
                if pin["role"] != "native-oracle"
                or (name.startswith("rpds/") and not name.endswith(".so"))
            )
            with patch.object(
                driver, "read", side_effect=AssertionError("must preflight stdlib")
            ):
                with self.assertRaisesRegex(ValueError, "bound"):
                    driver.stage_inputs(
                        stage, {**control, "input_limit": package_bytes}
                    )
                for name in ("logging/config.py", "../../outside.py"):
                    altered = {**stdlib_files}
                    if name in altered:
                        del altered[name]
                    else:
                        altered[name] = next(iter(stdlib_files.values()))
                    with (
                        self.subTest(name=name),
                        self.assertRaisesRegex(ValueError, "selection"),
                    ):
                        driver.stage_inputs(
                            stage,
                            {
                                **control,
                                "stdlib": {**control["stdlib"], "files": altered},
                            },
                        )
        with tempfile.TemporaryDirectory() as directory:
            original = captured["/stdlib/source/logging/__init__.py"]
            captured["/stdlib/source/logging/__init__.py"] = original + b"# changed\n"
            with patch.object(driver, "read", side_effect=read):
                with self.assertRaisesRegex(ValueError, "CPython source changed"):
                    driver.stage_inputs(Path(directory), control)
            captured["/stdlib/source/logging/__init__.py"] = original
        with tempfile.TemporaryDirectory() as directory:
            name = "logging/__init__.py"
            pins[0]["files"][name] = stdlib_files[name]
            captured["/inputs/packages/package0/" + name] = captured[
                "/stdlib/source/" + name
            ]
            with patch.object(driver, "read", side_effect=read):
                with self.assertRaisesRegex(ValueError, "namespace collision"):
                    driver.stage_inputs(Path(directory), control)


if __name__ == "__main__":
    unittest.main()
