"""Guest adapter protocol tests with explicit fixtures, not native IO parity."""

from __future__ import annotations

import base64
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools/python313-producer"),
                str(ROOT / "packs/local.ai-tool-bridge-ordered-python")]

from harness_native.native_path import PathBridge  # noqa: E402
from harness_native.value_graph import Scope, UnsupportedValue  # noqa: E402
import ordered_json_v2 as wire  # noqa: E402


def blob(value):
    return {"kind": "bytes", "value": base64.b64encode(value).decode("ascii")}


def path(binding, value):
    return {"kind": "path", "binding": binding, "value": value}


STAT = {"kind": "stat", "st_mode": 0o100600, "st_dev": 123, "st_ino": 456,
        "st_size": 3, "st_mtime_ns": 1793000000000000123}


class ScriptedPort:
    """A protocol fixture: no native authority, IO, budget or lifecycle claims."""

    def __init__(self, scope, results=()):
        self.scope, self.results = scope, list(results)
        self.requests = []

    def __call__(self, payload):
        frame = json.loads(payload)
        self.requests.append(frame)
        expected, result = self.results.pop(0)
        if frame["operation"] != expected:
            raise AssertionError((frame["operation"], expected))
        if isinstance(result, BaseException):
            raise result
        field = "error" if result.get("kind") == "fixture-error" else "result"
        return wire.dumps({"format": "io.tobkiri.native-inspection-response.v1",
                           "scope": self.scope.wire(), field: result})


class NativePathAdapterTests(unittest.TestCase):
    def setUp(self):
        self.scope = Scope(1, 2, 3, 4)

    def bridge(self, results=(), **kwargs):
        port = ScriptedPort(self.scope, results)
        return PathBridge(self.scope, "posix", port, os_flags={"O_RDONLY": 0},
                          **kwargs), port

    def test_lexical_path_comparison_and_derivation_do_no_inspection(self):
        for flavor, cls, root, joins in (
            ("posix", PurePosixPath, "/a/b", ("..", "child", "/other", "leaf")),
            ("windows", PureWindowsPath, "C:\\a\\b",
             ("..", "C:child", "\\rooted", "D:relative", "\\\\host\\share\\x")),
        ):
            with self.subTest(flavor=flavor):
                bridge = PathBridge(self.scope, flavor, self.fail,
                                    os_flags={"O_RDONLY": 0})
                value, expected = bridge.bind("root", root), cls(root)
                equal = bridge.bind("different_authority", root)
                self.assertEqual(value, equal)
                self.assertEqual(hash(value), hash(expected))
                self.assertNotEqual(value, root)
                self.assertEqual([str(p) for p in value.parents],
                                 [str(p) for p in expected.parents])
                for child in joins:
                    value, expected = value / child, expected / child
                    self.assertEqual(str(value), str(expected))
                    self.assertEqual(value.parts, expected.parts)
                    self.assertEqual(value.is_absolute(), expected.is_absolute())
                    self.assertEqual(value.as_posix(), expected.as_posix())
                self.assertEqual(value, expected)
                self.assertEqual(expected, value)
        posix, _ = self.bridge()
        windows = PathBridge(self.scope, "windows", self.fail,
                             os_flags={"O_RDONLY": 0})
        with self.assertRaises(TypeError):
            posix.bind("a", "/a") < windows.bind("b", "C:\\a")

    def test_expression_preserves_dotdot_and_does_not_merge_equal_bindings(self):
        bridge, port = self.bridge([
            ("is-file", {"kind": "bool", "value": True}),
            ("is-file", {"kind": "bool", "value": False}),
        ])
        first = bridge.bind("source_a", "/a/b") / ".." / "x"
        second = bridge.bind("source_b", "/a/b") / ".." / "x"
        self.assertEqual(first, second)
        self.assertIn("..", first.parts)
        self.assertTrue(first.is_file())
        self.assertFalse(second.is_file())
        self.assertEqual(port.requests[0]["target"], {
            "binding": "source_a", "steps": [["join", ".."], ["join", "x"]]})
        self.assertEqual(port.requests[1]["target"]["binding"], "source_b")

    def test_module_anchor_resolve_stays_at_original_import_reachpoint(self):
        bridge, port = self.bridge([
            ("resolve", path("resolved_module", "/app/runtime/core_runtime/module.py")),
            ("resolve", path("strict_root", "/app/bundle")),
        ])
        module_path = bridge.module_path(
            "/component/original.py", bridge.bind("module_anchor", "/app/raw/module.py")
        )
        lexical = module_path("/component/original.py")
        self.assertEqual(port.requests, [])
        projection = lexical.resolve().parents[1] / "profile_projections"
        self.assertEqual(str(projection), "/app/runtime/profile_projections")
        self.assertEqual(port.requests[0]["target"]["binding"], "module_anchor")
        self.assertEqual(port.requests[0]["args"], [False])
        bridge.bind("bundle", "/app/bundle").resolve(strict=True)
        self.assertEqual(port.requests[1]["args"], [True])
        self.assertEqual(module_path("relative").parts, ("relative",))

    def test_unbound_relative_absolute_observes_live_cwd_only_when_reached(self):
        bridge, port = self.bridge([
            ("absolute", path("absolute_a", "/cwd/a/../b")),
            ("absolute", path("absolute_b", "/changed/a/../b")),
        ], cwd_binding="live_cwd")
        relative = bridge.path("a/../b")
        with self.assertRaises(UnsupportedValue):
            relative.read_bytes()
        self.assertEqual(port.requests, [])
        self.assertEqual(str(relative.absolute()), "/cwd/a/../b")
        self.assertEqual(str(relative.absolute()), "/changed/a/../b")
        self.assertEqual([r["args"] for r in port.requests], [["a/../b"], ["a/../b"]])
        absolute = bridge.bind("root", "/a/b")
        self.assertIs(absolute.absolute(), absolute)
        child = (absolute / "child").relative_to(absolute)
        self.assertEqual(str(child), "child")
        with self.assertRaises(UnsupportedValue):
            child.is_file()
        disconnected, _ = self.bridge()
        with self.assertRaises(UnsupportedValue):
            disconnected.path("a").absolute()

    def test_fd_short_reads_eof_stat_fields_and_finally_close_keep_original_order(self):
        bridge, port = self.bridge([
            ("open", {"kind": "fd", "binding": "fd_1"}), ("fstat", STAT),
            ("read", blob(b"a")), ("read", blob(b"bc")), ("read", blob(b"")),
            ("fstat", STAT), ("lstat", STAT), ("close", {"kind": "unit"}),
        ])
        source = bridge.bind("file", "/bundle/file")
        self.assertFalse(hasattr(bridge.os, "O_NOFOLLOW"))
        fd = bridge.os.open(source, bridge.os.O_RDONLY)
        chunks = []
        try:
            before = bridge.os.fstat(fd)
            while chunk := bridge.os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            after = bridge.os.fstat(fd)
            current = source.lstat()
        finally:
            bridge.os.close(fd)
        self.assertEqual(chunks, [b"a", b"bc"])
        self.assertEqual(before.st_mtime_ns, 1793000000000000123)
        self.assertEqual((before.st_dev, before.st_ino, before.st_size), (123, 456, 3))
        self.assertEqual(before, after)
        self.assertEqual(after, current)
        self.assertEqual([r["args"] for r in port.requests if r["operation"] == "read"],
                         [[1024 * 1024]] * 3)
        with self.assertRaises(UnsupportedValue):
            bridge.os.read(fd, 1)
        with self.assertRaises(UnsupportedValue):
            before.st_ctime_ns
        with_flags = PathBridge(self.scope, "posix", self.fail,
                               os_flags={"O_RDONLY": 1234, "O_NOFOLLOW": 8192})
        self.assertTrue(hasattr(with_flags.os, "O_NOFOLLOW"))
        self.assertEqual(with_flags.os.O_RDONLY, 1234)

    def test_rglob_is_lazy_and_native_order_is_consumed_before_guest_sort(self):
        bridge, port = self.bridge([
            ("rglob", {"kind": "iterator", "binding": "walk"}),
            ("iterator-next", path("z", "/root/z")),
            ("iterator-next", path("a", "/root/a")),
            ("iterator-next", {"kind": "end"}),
            ("iterator-close", {"kind": "unit"}),
        ])
        iterator = bridge.bind("root", "/root").rglob("*")
        self.assertEqual(port.requests, [])
        self.assertEqual([str(p) for p in sorted(iterator)], ["/root/a", "/root/z"])
        self.assertEqual([r["operation"] for r in port.requests],
                         ["rglob", "iterator-next", "iterator-next",
                          "iterator-next", "iterator-close"])
        bridge, port = self.bridge([
            ("rglob", {"kind": "iterator", "binding": "walk"}),
            ("iterator-next", path("z", "/root/z")),
            ("iterator-close", {"kind": "unit"}),
        ])
        iterator = bridge.bind("root", "/root").rglob("*")
        next(iterator)
        iterator.close()
        self.assertEqual(port.requests[-1]["operation"], "iterator-close")

    def test_domain_close_error_and_outer_execution_failure_are_not_normalized(self):
        original = OSError(5, "fixture close failure")
        bridge, port = self.bridge([
            ("open", {"kind": "fd", "binding": "fd"}),
            ("close", {"kind": "fixture-error"}),
        ], decode_error=lambda graph: original)
        fd = bridge.os.open(bridge.bind("file", "/root/file"), 0)
        with self.assertRaises(OSError) as raised:
            bridge.os.close(fd)
        self.assertIs(raised.exception, original)
        with self.assertRaises(UnsupportedValue):
            bridge.os.fstat(fd)
        failure = MemoryError("original native execution failure")
        bridge, port = self.bridge([("read-bytes", failure)])
        with self.assertRaises(MemoryError) as raised:
            bridge.bind("file", "/root/file").read_bytes()
        self.assertIs(raised.exception, failure)

    def test_corrupt_foreign_error_and_unmodeled_results_fail_without_fallback(self):
        for result in (
            {"kind": "bool", "value": 1}, {"kind": "bool", "value": True, "extra": 1},
            {"kind": "path", "binding": "x", "value": "/tmp"},
            {"kind": "fixture-error"},
        ):
            bridge, _ = self.bridge([("is-file", result)])
            with self.assertRaises(UnsupportedValue):
                bridge.bind("file", "/root/file").is_file()
        response = {"format": "io.tobkiri.native-inspection-response.v1",
                    "scope": Scope(1, 2, 9, 4).wire(),
                    "result": {"kind": "bool", "value": True}}
        bridge = PathBridge(self.scope, "posix", lambda payload: wire.dumps(response),
                            os_flags={"O_RDONLY": 0})
        with self.assertRaises(UnsupportedValue):
            bridge.bind("file", "/root/file").is_file()


if __name__ == "__main__":
    unittest.main()
