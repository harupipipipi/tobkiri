"""Pure Python verifier tests; no upstream source execution."""
import importlib.util
import io
import json
import tarfile
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "verify_source", Path(__file__).resolve().parents[1] / "verify_source.py"
)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)

PATCH = b"--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n same\n-old\n+new\n"


class VerifyTests(unittest.TestCase):
    """Exercise strict patch handling and filesystem boundaries."""

    def test_exact_patch(self) -> None:
        self.assertEqual(
            VERIFY.apply_in_memory(PATCH, {"a.txt": b"same\nold\n"}),
            {"a.txt": b"same\nnew\n"},
        )

    def test_changed_context(self) -> None:
        with self.assertRaises(ValueError):
            VERIFY.apply_in_memory(PATCH, {"a.txt": b"other\nold\n"})

    def test_bad_hunk_count(self) -> None:
        with self.assertRaises(ValueError):
            VERIFY.apply_in_memory(
                PATCH.replace(b"-1,2", b"-1,3"), {"a.txt": b"same\nold\n"}
            )

    def test_duplicate_file(self) -> None:
        with self.assertRaises(ValueError):
            VERIFY.apply_in_memory(PATCH + PATCH, {"a.txt": b"same\nold\n"})

    def test_unknown_file(self) -> None:
        with self.assertRaises(ValueError):
            VERIFY.apply_in_memory(PATCH, {})

    def test_bad_output_position(self) -> None:
        with self.assertRaises(ValueError):
            VERIFY.apply_in_memory(
                PATCH.replace(b"+1,2", b"+2,2"), {"a.txt": b"same\nold\n"}
            )

    def test_file_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file").write_bytes(b"abc")
            (root / "link").symlink_to(root / "file")
            self.assertEqual(VERIFY.read_regular(root, "file"), b"abc")
            for name in ("../file", "/file", "link", "missing"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    VERIFY.read_regular(root, name)
            with self.assertRaises(ValueError):
                VERIFY.read_regular(root, "file", 2)

    def test_reviewed_patch_digest(self) -> None:
        self.assertEqual(
            VERIFY.digest((VERIFY.HERE / "python313-source.patch").read_bytes()),
            VERIFY.PATCH_SHA256,
        )


class FullVerificationTests(unittest.TestCase):
    """Exercise the entire verifier using small self-authored archive fixtures."""

    def setUp(self) -> None:
        """Create a bounded synthetic pinned source set, never upstream code."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "a.txt").write_bytes(b"same\nold\n")
        self.archive = self.root / "source.tar.xz"
        member = tarfile.TarInfo("Python-3.13.16/a.txt")
        member.size = len(b"same\nold\n")
        with tarfile.open(self.archive, "w:xz") as archive:
            archive.addfile(member, io.BytesIO(b"same\nold\n"))
        archive_hash = VERIFY.digest(self.archive.read_bytes())
        self.manifest = {
            "componentize_py_commit": VERIFY.COMMIT,
            "cpython_release": "3.13.16",
            "cpython_archive_sha256": archive_hash,
            "patch_sha256": VERIFY.digest(PATCH),
            "files": {
                "a.txt": {
                    "role": "patch-input",
                    "sha256": VERIFY.digest(b"same\nold\n"),
                    "patched_sha256": VERIFY.digest(b"same\nnew\n"),
                    "archive_member": member.name,
                }
            },
        }
        manifest_bytes = json.dumps(self.manifest).encode()
        (self.root / "source-manifest.json").write_bytes(manifest_bytes)
        (self.root / "python313-source.patch").write_bytes(PATCH)
        # Fixture identities are intentionally substituted only in tests.
        pins = mock.patch.multiple(
            VERIFY, HERE=self.root,
            PATCH_SHA256=VERIFY.digest(PATCH),
            MANIFEST_SHA256=VERIFY.digest(manifest_bytes),
            ARCHIVE_SHA256=archive_hash,
        )
        pins.start()
        self.addCleanup(pins.stop)

    def test_full_verification(self) -> None:
        result = VERIFY.verify(self.source, self.archive)
        self.assertEqual(result["selected_files"], 1)
        self.assertEqual(result["patched_files"], 1)
        self.assertFalse(result["componentize_py_full_tree_verified"])
        self.assertEqual((self.source / "a.txt").read_bytes(), b"same\nold\n")

    def test_manifest_tampering(self) -> None:
        with (self.root / "source-manifest.json").open("ab") as stream:
            stream.write(b" ")
        with self.assertRaisesRegex(ValueError, "manifest digest mismatch"):
            VERIFY.verify(self.source, self.archive)

    def test_patch_tampering(self) -> None:
        (self.root / "python313-source.patch").write_bytes(
            PATCH.replace(b"+new", b"+evil")
        )
        with self.assertRaisesRegex(ValueError, "patch digest mismatch"):
            VERIFY.verify(self.source, self.archive)

    def test_archive_tampering(self) -> None:
        with self.archive.open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(ValueError, "CPython archive digest mismatch"):
            VERIFY.verify(self.source, self.archive)

    def test_selected_source_tampering(self) -> None:
        (self.source / "a.txt").write_bytes(b"same\nchanged\n")
        with self.assertRaisesRegex(ValueError, "source digest mismatch: a.txt"):
            VERIFY.verify(self.source, self.archive)

    def test_wrong_patched_digest(self) -> None:
        self.manifest["files"]["a.txt"]["patched_sha256"] = "0" * 64
        manifest_bytes = json.dumps(self.manifest).encode()
        (self.root / "source-manifest.json").write_bytes(manifest_bytes)
        with mock.patch.object(
            VERIFY, "MANIFEST_SHA256", VERIFY.digest(manifest_bytes)
        ), self.assertRaisesRegex(ValueError, "patched digest mismatch: a.txt"):
            VERIFY.verify(self.source, self.archive)


if __name__ == "__main__":
    unittest.main()
