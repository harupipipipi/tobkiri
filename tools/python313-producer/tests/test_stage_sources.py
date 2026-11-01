"""Bounded synthetic tests for source staging; never execute archive content."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
try:
    SPEC = importlib.util.spec_from_file_location(
        "stage_sources", HERE / "stage_sources.py"
    )
    assert SPEC and SPEC.loader
    STAGE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(STAGE)
finally:
    sys.path.pop(0)


def archive_bytes(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    """Create small regular, directory, or special-member test archives."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o644
            member.size = len(content)
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "target"
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


class ArchiveTests(unittest.TestCase):
    """Test every archive trust boundary without downloads or build commands."""

    def inspect(self, entries: list[tuple[str, bytes, bytes]]) -> dict:
        """Inspect one synthetic archive with fresh aggregate counters."""
        return STAGE.inspect_archive(
            archive_bytes(entries), "root", {"members": 0, "bytes": 0}
        )

    def test_regular_files(self) -> None:
        self.assertEqual(self.inspect([
            ("root", tarfile.DIRTYPE, b""),
            ("root/file", tarfile.REGTYPE, b"data"),
        ]), {"file": (b"data", 0o644)})

    def test_traversal_and_noncanonical_paths(self) -> None:
        for name in ("/root/file", "root/../evil", "root/./file", "root//file",
                     "wrong/file", "root/evil\\file"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "path"):
                self.inspect([(name, tarfile.REGTYPE, b"data")])

    def test_links_and_special_members(self) -> None:
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE,
                     tarfile.CHRTYPE, tarfile.BLKTYPE):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "member"):
                self.inspect([("root/file", kind, b"")])

    def test_duplicate_members(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.inspect([("root/file", tarfile.REGTYPE, b"a")] * 2)

    def test_collisions_both_orders(self) -> None:
        entries = [("root/a", tarfile.REGTYPE, b"a"),
                   ("root/a/b", tarfile.REGTYPE, b"b")]
        for order in (entries, list(reversed(entries))):
            with self.subTest(order=order), self.assertRaisesRegex(
                ValueError, "collision"
            ):
                self.inspect(order)

    def test_member_limit(self) -> None:
        with mock.patch.object(STAGE, "MAX_MEMBERS", 0):
            with self.assertRaisesRegex(ValueError, "member limit"):
                self.inspect([("root/file", tarfile.REGTYPE, b"a")])

    def test_file_limit(self) -> None:
        with mock.patch.object(STAGE.verifier, "MAX_FILE", 1):
            with self.assertRaisesRegex(ValueError, "file limit"):
                self.inspect([("root/file", tarfile.REGTYPE, b"aa")])

    def test_aggregate_expanded_limit(self) -> None:
        with mock.patch.object(STAGE, "MAX_EXPANDED", 1):
            with self.assertRaisesRegex(ValueError, "expanded"):
                self.inspect([("root/a", tarfile.REGTYPE, b"a"),
                              ("root/b", tarfile.REGTYPE, b"b")])

    def test_existing_destination_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "existing"
            destination.mkdir()
            (destination / "marker").write_bytes(b"unchanged")
            with self.assertRaisesRegex(ValueError, "must not exist"):
                STAGE.stage(root, root / "missing", destination)
            self.assertEqual((destination / "marker").read_bytes(), b"unchanged")

    def test_archive_hash_mismatch_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "componentize-py.tar.gz").write_bytes(b"untrusted")
            destination = root / "new"
            with self.assertRaisesRegex(ValueError, "archive digest mismatch"):
                STAGE.stage(root, root / "missing", destination)
            self.assertFalse(destination.exists())
            self.assertFalse(list(root.glob(".new.staging-*")))


class PublicationTests(unittest.TestCase):
    """Test atomic publication and retained diagnostics with synthetic sources."""

    def setUp(self) -> None:
        """Substitute only local synthetic pins; never relax production pins."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "source"
        patch = (b"--- a/cpython/file\n+++ b/cpython/file\n"
                 b"@@ -1 +1 @@\n-old\n+new\n")
        archive = archive_bytes([("root/file", tarfile.REGTYPE, b"old\n")])
        self.archive = self.root / "input.tar.gz"
        self.archive.write_bytes(archive)
        manifest = {"files": {"cpython/file": {
            "role": "patch-input", "sha256": STAGE.verifier.digest(b"old\n"),
            "patched_sha256": STAGE.verifier.digest(b"new\n"),
        }}}
        manifest_bytes = json.dumps(manifest).encode()
        (self.root / "source-manifest.json").write_bytes(manifest_bytes)
        (self.root / "python313-source.patch").write_bytes(patch)
        identities = {"cpython": {
            "filename": self.archive.name, "prefix": "root",
            "sha256": STAGE.verifier.digest(archive), "url": "synthetic fixture",
        }}
        for replacement in (
            mock.patch.object(STAGE, "ARCHIVES", identities),
            mock.patch.multiple(
                STAGE.verifier, HERE=self.root,
                PATCH_SHA256=STAGE.verifier.digest(patch),
                MANIFEST_SHA256=STAGE.verifier.digest(manifest_bytes),
            ),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_published_patch_and_manifest(self) -> None:
        result = STAGE.stage(self.root, self.archive, self.destination)
        self.assertEqual((self.destination / "cpython/file").read_bytes(), b"new\n")
        receipt_data = (self.destination / "stage-manifest.json").read_bytes()
        self.assertEqual(result["manifest_sha256"], STAGE.verifier.digest(receipt_data))
        receipt = json.loads(receipt_data)
        self.assertEqual(receipt["files"]["cpython/file"]["mode"], "0o644")
        self.assertEqual(receipt["selected_files_verified"], 1)
        self.assertEqual(receipt["patched_files_verified"], 1)
        self.assertFalse(list(self.root.glob(".source.staging-*")))

    def test_publish_failure_retains_partial(self) -> None:
        with mock.patch.object(STAGE.os, "rename", side_effect=OSError("test failure")):
            with self.assertRaisesRegex(RuntimeError, "retained diagnostic tree"):
                STAGE.stage(self.root, self.archive, self.destination)
        self.assertFalse(self.destination.exists())
        partials = list(self.root.glob(".source.staging-*"))
        self.assertEqual(len(partials), 1)
        self.assertEqual((partials[0] / "cpython/file").read_bytes(), b"new\n")
        self.assertTrue((partials[0] / "stage-manifest.json").is_file())

    def test_compressed_size_limit(self) -> None:
        with mock.patch.object(STAGE.verifier, "MAX_ARCHIVE", 1):
            with self.assertRaisesRegex(ValueError, "oversized"):
                STAGE.stage(self.root, self.archive, self.destination)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
