import hashlib
import gzip
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_registry as registry


def lock_fixture(source=registry.REGISTRY, checksum="a" * 64):
    blocks = [f'[[package]]\nname = "{name}"\nversion = "0.1.0"'
              for name in sorted(registry.LOCAL_PACKAGES)]
    blocks += [f'[[package]]\nname = "wit-dylib-ffi"\nversion = "0.1.0"\n'
               f'source = "{registry.GIT_SOURCE}"',
               f'[[package]]\nname = "demo"\nversion = "1.0.0"\n'
               f'source = "{source}"\nchecksum = "{checksum}"']
    return ("\n\n".join(blocks) + "\n").encode()


def archive_fixture(members):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content, kind, *mode in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.mode = mode[0] if mode else 0o644
            info.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                info.linkname = "../../outside"
            archive.addfile(info, io.BytesIO(content) if info.size else None)
    # Python's writer strips file-type bits. Encode the legacy header field
    # explicitly so this exercises the bytes seen in historical crates.
    data = bytearray(output.getvalue())
    offset = 0
    for _, content, kind, *mode in members:
        if mode:
            data[offset + 100:offset + 108] = f"{mode[0]:07o}\0".encode()
            data[offset + 148:offset + 156] = b" " * 8
            checksum = sum(data[offset:offset + 512])
            data[offset + 148:offset + 156] = f"{checksum:06o}\0 ".encode()
        size = len(content) if kind == tarfile.REGTYPE else 0
        offset += 512 + (size + 511) // 512 * 512
    return gzip.compress(data)


class RegistryTests(unittest.TestCase):
    def parse_fixture(self, data):
        with patch.object(registry, "LOCK_SHA256", hashlib.sha256(data).hexdigest()), \
                patch.object(registry, "REGISTRY_COUNT", 1):
            return registry.registry_packages(data)

    def test_lock_identity_and_complete_source_closure(self):
        self.assertEqual(self.parse_fixture(lock_fixture()), [
            {"name": "demo", "version": "1.0.0", "sha256": "a" * 64}])
        with self.assertRaisesRegex(ValueError, "identity"):
            registry.registry_packages(lock_fixture())
        for data in (lock_fixture(source="registry+https://other.invalid"),
                     lock_fixture(checksum="missing"),
                     lock_fixture().replace(b'name = "demo"', b'name = "../demo"'),
                     lock_fixture().replace(b'componentize-py-test-generator', b'unrecorded'),
                     lock_fixture().replace(b'?rev=b072b0ca#', b'?rev=changed#'),
                     lock_fixture() + lock_fixture(),
                     lock_fixture().replace(b'name = "wit-dylib-ffi"', b'name = "different"')):
            with self.subTest(data=data[:80]), self.assertRaises(ValueError):
                self.parse_fixture(data)
        historical = json.loads((Path(registry.__file__).parent /
            "trials/2026-10-19/producer-receipt.json").read_text())["sources"]
        self.assertEqual(registry.LOCK_SHA256, historical["upstream_cargo_lock_sha256"])
        self.assertEqual(registry.REGISTRY_COUNT, historical["registry_packages_verified"])

    def test_download_checks_all_bytes_and_bound(self):
        data = b"actual crate bytes"
        expected = hashlib.sha256(data).hexdigest()
        output = io.BytesIO()
        self.assertEqual(registry.capture(io.BytesIO(data), output, expected, len(data)), len(data))
        self.assertEqual(output.getvalue(), data)
        for changed, bound in ((data + b"x", len(data)), (data[:-1], len(data)),
                               (b"changed", len(data)), (b"", len(data)), (data, 0)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                registry.capture(io.BytesIO(changed), io.BytesIO(), expected, bound)

    def unpack_fixture(self, root, data, totals=None):
        archive = root / "input.crate"
        archive.write_bytes(data)
        package = {"name": "demo", "version": "1.0.0",
                   "sha256": hashlib.sha256(data).hexdigest()}
        totals = totals if totals is not None else {"bytes": 0, "members": 0}
        return registry.unpack(archive, root / "source", package, totals)

    def test_verified_files_and_cargo_checksums_match_actual_archive(self):
        data = archive_fixture([("demo-1.0.0/src/lib.rs", b"// original\n", tarfile.REGTYPE),
                                ("demo-1.0.0/Cargo.toml", b"[package]\n", tarfile.REGTYPE)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            totals = {"bytes": 0, "members": 0}
            result = self.unpack_fixture(root, data, totals)
            self.assertEqual(totals, {"bytes": 22, "members": 2})
            self.assertEqual((root / "source/src/lib.rs").read_bytes(), b"// original\n")
            checksums = json.loads((root / "source/.cargo-checksum.json").read_text())
            self.assertEqual(checksums["package"], hashlib.sha256(data).hexdigest())
            self.assertEqual(checksums["files"], {
                name: hashlib.sha256((root / "source" / name).read_bytes()).hexdigest()
                for name in result["files"]})
            with self.assertRaises(FileExistsError):
                self.unpack_fixture(root, data)
            self.assertEqual((root / "source/src/lib.rs").read_bytes(), b"// original\n")

    def test_rejects_untrusted_members_before_publishing(self):
        regular = ("demo-1.0.0/src/lib.rs", b"x", tarfile.REGTYPE)
        cases = [
            [("demo-1.0.0/../outside", b"x", tarfile.REGTYPE)],
            [("other/src/lib.rs", b"x", tarfile.REGTYPE)],
            [("demo-1.0.0/link", b"", tarfile.SYMTYPE)],
            [("demo-1.0.0/pipe", b"", tarfile.FIFOTYPE)],
            [("demo-1.0.0/.cargo-checksum.json", b"{}", tarfile.REGTYPE)],
            [regular, regular], [],
        ]
        for members in cases:
            with self.subTest(members=members), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ValueError):
                    self.unpack_fixture(root, archive_fixture(members))
                self.assertFalse((root / "source").exists())

    def test_aggregate_bounds_and_archive_identity_precede_extraction(self):
        data = archive_fixture([("demo-1.0.0/file", b"content", tarfile.REGTYPE)])
        for totals in ({"bytes": registry.MAX_SOURCE_BYTES, "members": 0},
                       {"bytes": 0, "members": registry.MAX_SOURCE_MEMBERS}):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaisesRegex(ValueError, "closure budget"):
                    self.unpack_fixture(root, data, totals)
                self.assertFalse((root / "source").exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.crate").write_bytes(data)
            with self.assertRaisesRegex(ValueError, "changed before"):
                registry.unpack(root / "input.crate", root / "source",
                    {"name": "demo", "version": "1.0.0", "sha256": "0" * 64},
                    {"bytes": 0, "members": 0})
            self.assertFalse((root / "source").exists())

    def test_legacy_redundant_type_bits_do_not_admit_privileged_or_wrong_types(self):
        # Real difflib 0.4.0 uses 0100664 on regular tar members. File type bits
        # are not permission grants; privileged bits and type conflicts reject.
        data = archive_fixture([("demo-1.0.0/file", b"original", tarfile.REGTYPE, 0o100664)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.unpack_fixture(root, data)
            self.assertEqual(result["files"]["file"]["mode"], "0o664")
            self.assertEqual((root / "source/file").stat().st_mode & 0o7777, 0o664)
        # The original source-archive path remains strict by default.
        with self.assertRaisesRegex(ValueError, "special archive mode"):
            registry.inspect_archive(data, "demo-1.0.0", {"bytes": 0, "members": 0})
        for mode in (0o104664, 0o102664, 0o101664, 0o040664, 0o120664, 0o200664):
            with self.subTest(mode=oct(mode)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bad = archive_fixture([("demo-1.0.0/file", b"original", tarfile.REGTYPE, mode)])
                with self.assertRaisesRegex(ValueError, "special archive mode"):
                    self.unpack_fixture(root, bad)
                self.assertFalse((root / "source").exists())

    def test_local_invocation_does_not_start_network_or_create_output(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(registry.sys, "platform", "darwin"), \
                patch.object(registry, "urlopen", side_effect=AssertionError("network started")):
            destination = Path(directory) / "new"
            with self.assertRaisesRegex(ValueError, "Linux CI"):
                registry.fetch(Path(directory), destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
