"""Synthetic SDK archives only; no network, compiler or upstream execution."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_sdk


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for name, kind, value in entries:
            member = tarfile.TarInfo(fetch_sdk.PREFIX + "/" + name)
            member.mode = 0o755 if name.endswith("clang") else 0o644
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                bundle.addfile(member, io.BytesIO(value))
            else:
                member.linkname = value
                bundle.addfile(member)
    return output.getvalue()


class SdkStageTests(unittest.TestCase):
    def extract(self, data, directory):
        source = directory / "sdk.tar.gz"
        source.write_bytes(data)
        return fetch_sdk.extract(source, directory / "sdk")

    def test_regular_files_and_internal_relative_links_preserve_identity(self):
        data = archive([
            ("bin/clang", tarfile.REGTYPE, b"tool"),
            ("bin/clang++", tarfile.SYMTYPE, "clang"),
            ("lib/alias", tarfile.SYMTYPE, "../bin/clang"),
            ("bin/clang-copy", tarfile.LNKTYPE, fetch_sdk.PREFIX + "/bin/clang"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            records = self.extract(data, root)
            self.assertEqual((root / "sdk/lib/alias").read_bytes(), b"tool")
            self.assertEqual(records["bin/clang++"], {"link": "clang"})
            self.assertEqual(records["bin/clang"], records["bin/clang-copy"])

    def test_unsafe_paths_duplicates_links_and_special_files_are_rejected(self):
        cases = [
            [("../outside", tarfile.REGTYPE, b"bad")],
            [("/absolute", tarfile.REGTYPE, b"bad")],
            [("a", tarfile.REGTYPE, b"1"), ("a", tarfile.REGTYPE, b"2")],
            [("bin/a", tarfile.SYMTYPE, "../../outside")],
            [("a", tarfile.SYMTYPE, "/etc/passwd")],
            [("a", tarfile.SYMTYPE, "missing")],
            [("a", tarfile.SYMTYPE, "b"), ("b", tarfile.SYMTYPE, "a")],
            [("a", tarfile.FIFOTYPE, "")],
            [("a", tarfile.LNKTYPE, "../../outside")],
            [("a", tarfile.SYMTYPE, "."), ("a/file", tarfile.REGTYPE, b"bad")],
        ]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    self.extract(archive(entries), Path(tmp).resolve())

    def test_chained_directory_symlinks_cannot_escape_physical_root(self):
        data = archive([
            ("dir/file", tarfile.REGTYPE, b"inside"),
            ("dir/up", tarfile.SYMTYPE, ".."),
            ("dir/escape", tarfile.SYMTYPE, "up/../outside"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "outside").write_bytes(b"outside")
            with self.assertRaisesRegex(ValueError, "outside root"):
                self.extract(data, root)
            self.assertEqual((root / "outside").read_bytes(), b"outside")

    def test_member_count_file_size_and_expanded_total_are_bounded(self):
        data = archive([("a", tarfile.REGTYPE, b"1234"),
                        ("b", tarfile.REGTYPE, b"5678")])
        for key, limit in (("MAX_MEMBERS", 1), ("MAX_FILE", 3), ("MAX_EXPANDED", 7)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                with patch.object(fetch_sdk, key, limit), self.assertRaises(ValueError):
                    self.extract(data, Path(tmp).resolve())

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "sdk").mkdir()
            marker = root / "sdk/keep"
            marker.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                self.extract(archive([("a", tarfile.REGTYPE, b"data")]), root)
            self.assertEqual(marker.read_bytes(), b"existing")

    def test_full_download_requires_exact_size_and_hash(self):
        original = b"pinned archive"
        with patch.object(fetch_sdk, "ARCHIVE_BYTES", len(original)), patch.object(
                fetch_sdk, "SHA256", hashlib.sha256(original).hexdigest()):
            output = io.BytesIO()
            fetch_sdk.capture(io.BytesIO(original), output)
            self.assertEqual(output.getvalue(), original)
            for changed in (original[:-1], original + b"extra", b"changed bytes!"):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    fetch_sdk.capture(io.BytesIO(changed), io.BytesIO())


if __name__ == "__main__":
    unittest.main()
