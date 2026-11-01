"""Original artifact checks over captured UI input, not native/GUI authority."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
import plistlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tobkiri_runtime"))

from tobkiri_protocol.captured_inputs import (
    CAPTURE_CHUNK_BYTES,
    MAX_CAPTURE_FILES,
    MAX_CAPTURE_READ_BYTES,
    CapturedFile,
    CapturedInputs,
    CapturedReader,
)
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.platform_artifact import artifact_digest, verify_platform_artifact


def capture(files: dict[str, bytes]):
    """Stand in for a closed native ordinal reader, never an OS-path callback."""
    items = tuple(files.items())
    calls = []

    def read(ordinal: int, offset: int, count: int) -> bytes:
        calls.append((ordinal, offset, count))
        return items[ordinal][1][offset : offset + count]

    view = CapturedInputs(
        (CapturedFile(name, len(value)) for name, value in items),
        read,
    )
    return view.root(), calls


def executable(kind: str) -> bytes:
    """Small valid headers for each unchanged original architecture parser."""
    if kind == "pe":
        value = bytearray(152)
        value[:2] = b"MZ"
        value[60:64] = (128).to_bytes(4, "little")
        value[128:132] = b"PE\0\0"
        value[132:134] = (0x8664).to_bytes(2, "little")
        return bytes(value)
    if kind == "macho":
        return b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little")
    return b"\x7fELF\x02\x01" + bytes(12) + (62).to_bytes(2, "little")


class CapturedSetupInputTests(unittest.TestCase):
    def test_original_tree_entrypoint_headers_and_plist_match_without_os_io(self):
        for kind, platform, architecture in (
            ("elf", "linux", "x86_64"),
            ("pe", "windows", "x86_64"),
            ("macho", "macos", "arm64"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                native_root = Path(directory).resolve(strict=True)
                entry = "App/Contents/MacOS/shell" if kind == "macho" else "App/shell"
                files = {entry: executable(kind), "App/resources/日本語": b"same bytes"}
                if kind == "macho":
                    files["App/Contents/Info.plist"] = plistlib.dumps(
                        {
                            "CFBundleIdentifier": "io.tobkiri.fixture",
                        }
                    )
                for name, value in files.items():
                    path = native_root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(value)
                variant = {
                    "relative_path": "App",
                    "entrypoint": entry,
                    "artifact_digest": artifact_digest(native_root / "App"),
                    "entrypoint_digest": "sha256:"
                    + hashlib.sha256(files[entry]).hexdigest(),
                    "platform": platform,
                    "architecture": architecture,
                    "bundle_identity": "io.tobkiri.fixture",
                }
                self.assertEqual(
                    verify_platform_artifact(native_root, variant), native_root / "App"
                )
                captured, calls = capture(files)
                with (
                    patch("os.open", side_effect=AssertionError("OS access")),
                    patch.object(
                        Path,
                        "read_bytes",
                        side_effect=AssertionError("OS access"),
                    ),
                ):
                    self.assertEqual(
                        artifact_digest(captured / "App"), variant["artifact_digest"]
                    )
                    self.assertEqual(
                        verify_platform_artifact(captured, variant), captured / "App"
                    )
                self.assertTrue(calls)
                self.assertLessEqual(
                    max(count for _, _, count in calls), CAPTURE_CHUNK_BYTES
                )

    def test_original_verifier_rejects_stale_artifact_entrypoint_and_architecture(self):
        root, _ = capture({"App/shell": executable("pe")})
        variant = {
            "relative_path": "App",
            "entrypoint": "App/shell",
            "artifact_digest": artifact_digest(root / "App"),
            "entrypoint_digest": "sha256:"
            + hashlib.sha256(executable("pe")).hexdigest(),
            "platform": "windows",
            "architecture": "x86_64",
            "bundle_identity": "fixture",
        }
        for field, value in (
            ("artifact_digest", "sha256:" + "ab" * 32),
            ("entrypoint_digest", "sha256:" + "ab" * 32),
            ("architecture", "arm64"),
            ("entrypoint", "Elsewhere/shell"),
            ("relative_path", "../App"),
            ("relative_path", "a/" * 3000 + "not-captured"),
            ("entrypoint", "a/" * 3000 + "not-captured"),
        ):
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                verify_platform_artifact(root, {**variant, field: value})

    def test_large_input_is_streamed_and_cannot_be_materialized_whole(self):
        size = MAX_CAPTURE_READ_BYTES + 1
        calls = []

        def read(ordinal, offset, count):
            self.assertEqual(ordinal, 0)
            self.assertLessEqual(offset + count, size)
            calls.append(count)
            return b"x" * count

        root = CapturedInputs([CapturedFile("large", size)], read).root()
        with self.assertRaisesRegex(ProtocolError, "buffer bound"):
            (root / "large").read_bytes()
        self.assertEqual(calls, [])
        expected = hashlib.sha256(b"\0")  # Empty relative name for a root file.
        for _ in range(MAX_CAPTURE_READ_BYTES // CAPTURE_CHUNK_BYTES):
            expected.update(b"x" * CAPTURE_CHUNK_BYTES)
        expected.update(b"x")
        self.assertEqual(
            artifact_digest(root / "large"), "sha256:" + expected.hexdigest()
        )
        self.assertLessEqual(max(calls), CAPTURE_CHUNK_BYTES)

    def test_no_path_escape_os_fallback_or_cross_capture_identity(self):
        root, _ = capture(
            {
                "schemas/b.schema.json": b"[]",
                "schemas/a.schema.json": b"{}",
                "bundle.lock.json": b"lock",
            }
        )
        other, _ = capture({"schemas/a.schema.json": b"different"})
        for name in ("../file", "/etc/passwd", "a/../../file", "a\\file", "a\0file"):
            with self.subTest(name=name), self.assertRaises(ProtocolError):
                root / name
        with self.assertRaises(TypeError):
            os.fspath(root / "bundle.lock.json")
        with self.assertRaises(ValueError):
            (root / "schemas/a.schema.json").relative_to(other)
        self.assertEqual((root / "bundle.lock.json").read_bytes(), b"lock")
        self.assertEqual(
            [p.name for p in sorted((root / "schemas").glob("*.schema.json"))],
            ["a.schema.json", "b.schema.json"],
        )
        self.assertEqual((root / "schemas/a.schema.json").suffix, ".json")
        self.assertEqual(
            root / PureWindowsPath("schemas/a.schema.json"),
            root / "schemas/a.schema.json",
        )
        with self.assertRaises(ProtocolError):
            root / PureWindowsPath("C:/schemas/a.schema.json")
        with self.assertRaises(FileNotFoundError):
            (root / "not-captured").read_bytes()

    def test_inventory_rejects_duplicates_collisions_and_unbounded_metadata(self):
        for files in (
            [CapturedFile("../x", 1)],
            [CapturedFile("/x", 1)],
            [CapturedFile("a", -1)],
            [CapturedFile("a", True)],
            [CapturedFile("a", 1), CapturedFile("a", 1)],
            [CapturedFile("a", 1), CapturedFile("a/b", 1)],
            [CapturedFile("a/b", 1), CapturedFile("a", 1)],
            [CapturedFile("a/./b", 1)],
            [CapturedFile("a//b", 1)],
            [CapturedFile("x" * (1024 * 1024), 1)],
            [CapturedFile("a/" * 3000 + "file", 1)],
            [CapturedFile("bad\ud800path", 1)],
        ):
            with self.subTest(count=len(files)), self.assertRaises(ProtocolError):
                CapturedInputs(files, lambda *_: b"")
        with self.assertRaises(ProtocolError):
            CapturedInputs(
                (CapturedFile(str(i), 0) for i in range(MAX_CAPTURE_FILES + 1)),
                lambda *_: b"",
            )

    def test_original_schema_alias_and_sorted_inventory_use_only_capture(self):
        from tobkiri_protocol import validation

        original = validation.schema_path("pack")
        raw = original.read_bytes()
        root, _ = capture({f"schemas/{original.name}": raw})
        with patch.object(validation, "SCHEMA_DIR", root / "schemas"):
            with patch("os.open", side_effect=AssertionError("OS access")):
                selected = validation.schema_path("pack")
                self.assertEqual(selected.read_bytes(), raw)
                self.assertEqual(
                    sorted((root / "schemas").glob("*.schema.json")), [selected]
                )
                with self.assertRaises(ProtocolError):
                    validation.schema_path("not-captured.schema.json")

    def test_short_ranges_closed_readers_deadlines_and_native_signature_fail_closed(
        self,
    ):
        bad = CapturedInputs([CapturedFile("file", 4)], lambda *_: b"x").root()
        with self.assertRaisesRegex(ProtocolError, "truncated"):
            (bad / "file").read_bytes()
        root, _ = capture({"file": b"hello"})
        for ordinal, size in ((1, 5), (-1, 5), (True, 5), (0, 6)):
            with self.assertRaises(ProtocolError):
                CapturedReader(root._capture, ordinal, size)
        with (root / "file").open_binary() as reader:
            reader.seek(4)
            self.assertEqual(reader.read(64), b"o")
        with self.assertRaises(ProtocolError):
            reader.read(1)
        with self.assertRaisesRegex(ProtocolError, "deadline"):
            artifact_digest(root / "file", deadline_monotonic=time.monotonic() - 1)
        # No subprocess API or native signature assertion is offered by this adapter.
        files = {
            "App/Contents/MacOS/shell": executable("macho"),
            "App/Contents/Info.plist": plistlib.dumps(
                {"CFBundleIdentifier": "fixture"}
            ),
        }
        root, _ = capture(files)
        variant = {
            "relative_path": "App",
            "entrypoint": "App/Contents/MacOS/shell",
            "artifact_digest": artifact_digest(root / "App"),
            "entrypoint_digest": "sha256:"
            + hashlib.sha256(executable("macho")).hexdigest(),
            "platform": "macos",
            "architecture": "arm64",
            "bundle_identity": "fixture",
        }
        with patch("subprocess.run", side_effect=AssertionError("process execution")):
            with self.assertRaisesRegex(ProtocolError, "native User inspection"):
                verify_platform_artifact(
                    root, variant, require_macos_code_signature=True
                )


if __name__ == "__main__":
    unittest.main()
