"""Offline input-integrity tests; no downloaded executable is run."""

import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tools = importlib.import_module("fetch_ui_poll_tools")


class ToolInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def archive(self, extra=(), binary=None):
        executable = bytearray(64) if binary is None else bytearray(binary)
        if binary is None:
            executable[:6] = b"\x7fELF\x02\x01"
            executable[18:20] = (62).to_bytes(2, "little")
        result = self.root / "source.tar.gz"
        with tarfile.open(result, "w:gz") as bundle:
            entries = [(tools.PREFIX + "/wasm-tools", bytes(executable)), *extra]
            for name, raw in entries:
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                info.size = len(raw)
                bundle.addfile(info, io.BytesIO(raw))
        return result, bytes(executable)

    def pin(self, archive):
        raw = archive.read_bytes()
        return (
            patch.object(tools, "ARCHIVE_BYTES", len(raw)),
            patch.object(tools, "ARCHIVE_SHA256", hashlib.sha256(raw).hexdigest()),
        )

    def cmake_wheel(self, binary):
        path = self.root / "cmake.whl"
        with zipfile.ZipFile(path, "w") as bundle:
            for name in tools.CMAKE_EXECUTABLES:
                bundle.writestr(name, binary)
            bundle.writestr(
                "cmake/data/share/cmake-3.31/Modules/example.cmake", b"# source"
            )
        return (
            patch.object(tools, "CMAKE_BYTES", path.stat().st_size),
            patch.object(
                tools, "CMAKE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest()
            ),
        )

    def test_rewritten_receipt_cannot_hide_changed_executable(self):
        archive, binary = self.archive()
        cmake_pins = self.cmake_wheel(binary)
        with self.pin(archive)[0], self.pin(archive)[1], cmake_pins[0], cmake_pins[1]:
            actual, members = tools.selected(archive)
            self.assertEqual(actual, binary)
            target = self.root / "wasm-tools"
            target.write_bytes(binary)
            target.chmod(0o700)
            cmake = tools.cmake_entries(self.root / "cmake.whl", self.root / "cmake")
            receipt = tools.receipt(archive, binary, members, cmake)
            manifest = self.root / "tool-inputs.json"
            manifest.write_text(json.dumps(receipt))
            self.assertEqual(tools.verify(self.root), receipt)
            target.write_bytes(binary + b"changed")
            receipt["executable"] = tools.fingerprint(target)
            manifest.write_text(json.dumps(receipt))
            with self.assertRaises(ValueError):
                tools.verify(self.root)

    def test_cmake_tree_is_bound_to_the_complete_pinned_wheel(self):
        _, binary = self.archive()
        pins = self.cmake_wheel(binary)
        with pins[0], pins[1]:
            files = tools.cmake_entries(self.root / "cmake.whl", self.root / "cmake")
            self.assertEqual(len(files), 4)
            self.assertTrue(tools.CMAKE_EXECUTABLES <= files.keys())
            with patch.object(tools, "CMAKE_SHA256", "0" * 64):
                with self.assertRaises(ValueError):
                    tools.cmake_entries(self.root / "cmake.whl")

    def test_unsafe_or_duplicate_archive_paths_are_rejected(self):
        for name in (tools.PREFIX + "/../escape", tools.PREFIX + "/wasm-tools"):
            with self.subTest(name=name):
                archive, _ = self.archive([(name, b"unexpected")])
                with self.pin(archive)[0], self.pin(archive)[1]:
                    with self.assertRaises(ValueError):
                        tools.selected(archive)

    def test_non_native_or_changed_archive_is_rejected(self):
        archive, _ = self.archive(binary=b"not-a-native-tool")
        with self.pin(archive)[0], self.pin(archive)[1]:
            with self.assertRaises(ValueError):
                tools.selected(archive)
        archive, _ = self.archive()
        with self.pin(archive)[0], patch.object(tools, "ARCHIVE_SHA256", "0" * 64):
            with self.assertRaises(ValueError):
                tools.selected(archive)

    def test_local_execution_guard_precedes_network_and_mutation(self):
        target = self.root / "must-stay-absent"
        with (
            patch.object(tools.sys, "platform", "darwin"),
            patch.object(tools, "urlopen") as opened,
        ):
            with self.assertRaises(ValueError):
                tools.run(target)
        self.assertFalse(target.exists())
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
