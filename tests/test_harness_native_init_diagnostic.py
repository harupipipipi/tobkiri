"""Portable init diagnostics only; no original runtime, guest or native import."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools/python313-producer"),
               str(ROOT / "packs/local.ai-tool-bridge-ordered-python")]

from harness_native import init_diagnostic as diagnostic  # noqa: E402
from harness_native.value_graph import Scope  # noqa: E402


class InitDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.scope = Scope(1, 2, 3, 4)

    def frame(self, error, phase="original-factory"):
        payload = diagnostic.diagnostic(self.scope, phase, error)
        self.assertLessEqual(len(payload.encode("utf-8")), 4096)
        value = json.loads(payload)
        self.assertEqual(set(value), {
            "format", "scope", "phase", "kind", "args", "fields",
            "locations", "omitted",
        })
        self.assertEqual(value["format"], diagnostic.FORMAT)
        self.assertEqual(value["scope"], self.scope.wire())
        self.assertEqual(value["phase"], phase)
        return value

    def test_exact_builtin_tags_and_applicable_descriptor_fields(self):
        self.assertEqual(len(diagnostic._KINDS), 14)
        for cls, kind in diagnostic._KINDS:
            with self.subTest(kind=kind):
                frame = self.frame(cls("plain message"))
                self.assertEqual(frame["kind"], kind)
                self.assertEqual(frame["args"], ["plain message"])
                self.assertFalse(frame["omitted"])
        error = ModuleNotFoundError("absent", name="safe.module", path="safe.py")
        self.assertEqual(self.frame(error)["fields"],
                         {"name": "safe.module", "path": "safe.py"})
        self.assertEqual(self.frame(NameError("name", name="safe"))["fields"],
                         {"name": "safe"})
        self.assertEqual(self.frame(AttributeError("attribute", name="safe"))["fields"],
                         {"name": "safe"})
        self.assertEqual(self.frame(FileNotFoundError(2, "missing", "original.py"))[
            "fields"], {"errno": 2, "strerror": "missing",
                        "filename": "original.py", "filename2": None})

    def test_unknown_class_never_calls_class_hash_attribute_or_display_hooks(self):
        calls = []

        class Meta(type):
            def __hash__(cls):
                calls.append("class hash")
                raise AssertionError("must not hash an unknown class")

            def __getattribute__(cls, name):
                calls.append("class attribute")
                raise AssertionError("must not inspect an unknown class")

        class Unknown(ValueError, metaclass=Meta):
            def __getattribute__(self, name):
                calls.append("exception attribute")
                raise AssertionError("must not inspect unknown exception attributes")

            def __str__(self):
                calls.append("str")
                raise AssertionError("must not display the exception")

            def __repr__(self):
                calls.append("repr")
                raise AssertionError("must not display the exception")

        error = Unknown("private")
        frame = self.frame(error)
        self.assertEqual(calls, [])
        self.assertEqual(frame["kind"], "unrepresented-exception")
        self.assertEqual(frame["args"], [])
        self.assertEqual(frame["fields"], {})
        self.assertEqual(frame["locations"], [])
        self.assertTrue(frame["omitted"])

    def test_scalars_utf8_lengths_arg_count_and_unsafe_values_are_bounded(self):
        class Private:
            def __str__(self):
                raise AssertionError("must not stringify a value")

            def __repr__(self):
                raise AssertionError("must not represent a value")

        valid = [None, True, -(1 << 53) + 1, "x" * 512]
        frame = self.frame(ValueError(*valid))
        self.assertEqual(frame["args"], valid)
        self.assertFalse(frame["omitted"])
        for unsafe in (Private(), [], {}, 1.0, 1 << 53, "x" * 513,
                       "é" * 257, "\ud800"):
            with self.subTest(kind=type(unsafe)):
                frame = self.frame(ValueError("safe prefix", unsafe, "later"))
                self.assertEqual(frame["args"], ["safe prefix"])
                self.assertTrue(frame["omitted"])
        frame = self.frame(ValueError(1, 2, 3, 4, 5))
        self.assertEqual(frame["args"], [1, 2, 3, 4])
        self.assertTrue(frame["omitted"])
        error = NameError("safe", name=Private())
        self.assertEqual(self.frame(error)["fields"], {})
        self.assertTrue(self.frame(error)["omitted"])

    def test_oversized_or_unserializable_diagnostic_uses_same_kind_minimal_frame(self):
        text = "\n" * 512
        error = OSError(2, text, text, None, text)
        error.args = (text,) * 4
        frame = self.frame(error, "transport-registry")
        self.assertEqual(frame["kind"], "file-not-found-error")
        self.assertEqual(frame["args"], [])
        self.assertEqual(frame["fields"], {})
        self.assertTrue(frame["omitted"])
        original = diagnostic.wire.dumps
        calls = []

        def fail_once(frame):
            calls.append(frame)
            if len(calls) == 1:
                raise UnicodeEncodeError("utf8", "x", 0, 1, "fixture")
            return original(frame)

        with patch.object(diagnostic.wire, "dumps", side_effect=fail_once):
            frame = self.frame(ValueError("original"))
        self.assertEqual(frame["kind"], "value-error")
        self.assertEqual(frame["args"], [])
        self.assertTrue(frame["omitted"])

    def test_original_trace_locations_are_bounded_and_never_expose_locals(self):
        def recurse(depth):
            private_local = "secret local which must never be serialized"
            if private_local and depth:
                return recurse(depth - 1)
            raise ValueError("original location")

        def capture():
            try:
                recurse(8)
            except ValueError as error:
                return self.frame(error)

        frame = capture()
        self.assertEqual(len(frame["locations"]), 4)
        self.assertTrue(frame["omitted"])
        self.assertNotIn("secret local", json.dumps(frame))
        for filename, function, line in frame["locations"]:
            self.assertLessEqual(len(filename.encode("utf-8")), 256)
            self.assertLessEqual(len(function.encode("utf-8")), 64)
            self.assertGreater(line, 0)
        code = compile("raise ValueError('long location')", "é" * 129, "exec")
        try:
            exec(code, {})
        except ValueError as error:
            frame = self.frame(error)
        self.assertTrue(frame["omitted"])
        self.assertFalse(any(row[0] == "é" * 129 for row in frame["locations"]))
        source = ast.parse(Path(diagnostic.__file__).read_text())
        attributes = {node.attr for node in ast.walk(source) if isinstance(node, ast.Attribute)}
        self.assertTrue(attributes.isdisjoint({"f_locals", "tb_lasti", "__dict__",
                                             "__cause__", "__context__"}))
        self.assertFalse(any(isinstance(node, ast.Call)
                             and isinstance(node.func, ast.Name)
                             and node.func.id in {"str", "repr", "getattr"}
                             for node in ast.walk(source)))

    def test_checkpoints_are_seven_fixed_closed_frames_and_no_reply_is_authority(self):
        bindings = ModuleType("wit_world.imports.bindings")
        calls = []
        bindings.inspect = lambda payload: calls.append(payload) or object()
        with patch.dict(sys.modules, {"wit_world.imports.bindings": bindings}):
            for sequence in range(1, 8):
                self.assertIsNone(diagnostic.report_init_checkpoint(sequence))
            for invalid in (0, 8, True, "1", None):
                with self.assertRaises(ValueError):
                    diagnostic.report_init_checkpoint(invalid)
        self.assertEqual(len(calls), 7)
        for sequence, payload in enumerate(calls, 1):
            self.assertLessEqual(len(payload.encode()), 128)
            self.assertEqual(json.loads(payload), {
                "format": "io.tobkiri.native-init-checkpoint.v1",
                "sequence": sequence,
                "stage": diagnostic.CHECKPOINT_STAGES[sequence - 1],
            })

    def test_only_fixed_inspect_import_is_used_and_reply_is_ignored(self):
        bindings = ModuleType("wit_world.imports.bindings")
        calls = []
        bindings.inspect = lambda payload: calls.append(payload) or object()
        with patch.dict(sys.modules, {"wit_world.imports.bindings": bindings}):
            self.assertIsNone(diagnostic.report_init_failure(
                self.scope, "original-factory", ValueError("original"),
            ))
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0])["kind"], "value-error")

    def test_invalid_trusted_scope_or_phase_cannot_create_a_new_frame(self):
        for scope, phase in ((None, "original-factory"),
                             (self.scope, "runtime-method"), (self.scope, None)):
            with self.assertRaises(ValueError):
                diagnostic.diagnostic(scope, phase, ValueError())


if __name__ == "__main__":
    unittest.main()
