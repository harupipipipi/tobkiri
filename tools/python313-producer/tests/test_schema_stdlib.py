"""Tiny offline tests for the data-only CPython logging source stage."""

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import fetch_schema_stdlib


def digest(data: bytes) -> str:
    """Return a SHA-256 digest for a tiny fixture."""
    return hashlib.sha256(data).hexdigest()


def tar_archive(path: Path, members) -> bytes:
    """Write small tar.xz members given as (name, data, mode, type, target)."""
    with tarfile.open(path, "w:xz") as archive:
        for name, data, mode, kind, target in members:
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.type = kind
            member.linkname = target
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(
                member,
                io.BytesIO(data) if kind == tarfile.REGTYPE else None,
            )
    return path.read_bytes()


def source_fixture(path: Path, *, data=(b"init", b"config", b"handlers")) -> bytes:
    """Make a tiny CPython-shaped archive with exactly three selected files."""
    prefix = fetch_schema_stdlib.LOCKED_PREFIX
    members = [
        (f"{prefix}/", b"", 0o755, tarfile.DIRTYPE, ""),
        (
            f"{prefix}/Lib/logging/__init__.py",
            data[0],
            0o644,
            tarfile.REGTYPE,
            "",
        ),
        (
            f"{prefix}/Lib/logging/config.py",
            data[1],
            0o755,
            tarfile.REGTYPE,
            "",
        ),
        (
            f"{prefix}/Lib/logging/handlers.py",
            data[2],
            0o644,
            tarfile.REGTYPE,
            "",
        ),
        (
            f"{prefix}/Lib/other.py",
            b"not selected",
            0o644,
            tarfile.REGTYPE,
            "",
        ),
    ]
    return tar_archive(path, members)


class SchemaStdlibInputTests(unittest.TestCase):
    def test_existing_cpython_source_pin_is_exact(self):
        self.assertEqual(
            fetch_schema_stdlib._source_pin(),
            {
                "filename": "Python-3.13.16.tar.xz",
                "sha256": (
                    "f4b1bfb3c79b5bb11b8d228a12504163b4c0dab4d679828d8f5f26b6cb6ab35d"
                ),
                "prefix": "Python-3.13.16",
                "url": (
                    "https://www.python.org/ftp/python/3.13.16/Python-3.13.16.tar.xz"
                ),
            },
        )

    def test_streaming_selection_captures_only_expected_files_and_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "tiny.tar.xz"
            data = source_fixture(archive)
            pin = {
                **fetch_schema_stdlib._source_pin(),
                "sha256": digest(data),
            }
            selected, report = fetch_schema_stdlib._source_members(archive, pin)
            self.assertEqual(set(selected), set(fetch_schema_stdlib.SELECTED_FILES))
            self.assertEqual(report["archive"]["bytes"], len(data))
            self.assertEqual(report["archive"]["sha256"], digest(data))
            self.assertEqual(
                [
                    report["files"][name]["mode"]
                    for name in fetch_schema_stdlib.SELECTED_FILES
                ],
                ["0o644", "0o755", "0o644"],
            )
            self.assertEqual(
                [
                    report["files"][name]["sha256"]
                    for name in fetch_schema_stdlib.SELECTED_FILES
                ],
                [digest(b"init"), digest(b"config"), digest(b"handlers")],
            )

    def test_archive_hash_mismatch_is_rejected_before_member_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "tiny.tar.xz"
            source_fixture(archive)
            pin = {
                **fetch_schema_stdlib._source_pin(),
                "sha256": "0" * 64,
            }
            with self.assertRaisesRegex(ValueError, "differs from its source pin"):
                fetch_schema_stdlib._source_members(archive, pin)

    def _run_offline_stage(self, destination: Path):
        """Exercise acquisition and verification with a tiny pinned fixture."""
        with tempfile.TemporaryDirectory() as temp:
            fixture_path = Path(temp) / "tiny.tar.xz"
            archive_bytes = source_fixture(fixture_path)
        pin = {
            **fetch_schema_stdlib._source_pin(),
            "sha256": digest(archive_bytes),
        }

        def download(url, target, expected, maximum):
            if (
                url != pin["url"]
                or expected != pin["sha256"]
                or len(archive_bytes) > maximum
            ):
                raise AssertionError("pinned helper received unexpected archive input")
            with target.open("xb") as output:
                output.write(archive_bytes)
            return len(archive_bytes)

        with (
            patch.object(fetch_schema_stdlib, "_source_pin", return_value=pin),
            patch.object(
                fetch_schema_stdlib.fetch_rpds,
                "download",
                side_effect=download,
            ),
            patch.object(fetch_schema_stdlib.sys, "platform", "linux"),
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            patch.object(
                fetch_schema_stdlib.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=fetch_schema_stdlib.DISK_FLOOR),
            ),
        ):
            receipt = fetch_schema_stdlib.run(destination)
            verified = fetch_schema_stdlib.verify(destination)
        return receipt, verified, pin

    def test_ci_stage_retains_archive_and_publishes_only_logging_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp).resolve() / "stdlib-inputs"
            receipt, verified, pin = self._run_offline_stage(destination)
            self.assertEqual(receipt["source_pin"], pin)
            self.assertEqual(verified["files"], 3)
            self.assertTrue((destination / "archives/Python-3.13.16.tar.xz").is_file())
            self.assertEqual(
                {path.name for path in (destination / "source/logging").iterdir()},
                {"__init__.py", "config.py", "handlers.py"},
            )
            self.assertEqual(
                set(receipt["files"]),
                set(fetch_schema_stdlib.SELECTED_FILES),
            )
            self.assertFalse((destination / "source/other.py").exists())

    def test_forged_source_and_receipt_still_fail_against_pinned_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp).resolve() / "stdlib-inputs"
            _, _, pin = self._run_offline_stage(destination)
            changed = destination / "source/logging/config.py"
            changed.write_bytes(b"forged")
            actual = fetch_schema_stdlib._read_source_tree(destination / "source")
            receipt_path = destination / "stdlib-inputs.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["files"] = fetch_schema_stdlib._inventory(actual)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with patch.object(fetch_schema_stdlib, "_source_pin", return_value=pin):
                with self.assertRaisesRegex(
                    ValueError,
                    "differs from its pinned archive",
                ):
                    fetch_schema_stdlib.verify(destination)

    def _inspect_fixture(self, members, *, patches=None):
        """Stream and inspect a tiny tar fixture with a test-only source pin."""
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "fixture.tar.xz"
            raw = tar_archive(archive, members)
            pin = {
                **fetch_schema_stdlib._source_pin(),
                "sha256": digest(raw),
            }
            if patches is None:
                return fetch_schema_stdlib._source_members(archive, pin)
            with patches:
                return fetch_schema_stdlib._source_members(archive, pin)

    def test_unsafe_paths_selected_symlinks_and_file_directory_collisions_fail(self):
        prefix = fetch_schema_stdlib.LOCKED_PREFIX
        regular = tarfile.REGTYPE
        link = tarfile.SYMTYPE
        fixtures = [
            [
                (
                    f"{prefix}/Lib/logging/../config.py",
                    b"bad",
                    0o644,
                    regular,
                    "",
                )
            ],
            [
                (
                    f"{prefix}/Lib/logging/config.py",
                    b"",
                    0o777,
                    link,
                    "outside.py",
                )
            ],
            [
                (f"{prefix}/Lib/logging", b"file", 0o644, regular, ""),
                (
                    f"{prefix}/Lib/logging/config.py",
                    b"child",
                    0o644,
                    regular,
                    "",
                ),
            ],
        ]
        for members in fixtures:
            with self.subTest(members=[item[0] for item in members]):
                with self.assertRaises(ValueError):
                    self._inspect_fixture(members)

    def test_selected_file_and_total_size_bounds_fail(self):
        prefix = fetch_schema_stdlib.LOCKED_PREFIX
        members = [
            (
                f"{prefix}/Lib/logging/{name}",
                b"xx",
                0o644,
                tarfile.REGTYPE,
                "",
            )
            for name in ("__init__.py", "config.py", "handlers.py")
        ]
        with patch.object(fetch_schema_stdlib, "MAX_SELECTED_FILE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "file exceeds its bound"):
                self._inspect_fixture(members)
        with (
            patch.object(fetch_schema_stdlib, "MAX_SELECTED_FILE_BYTES", 2),
            patch.object(fetch_schema_stdlib, "MAX_SELECTED_TOTAL_BYTES", 5),
        ):
            with self.assertRaisesRegex(ValueError, "total bound"):
                self._inspect_fixture(members)

    def test_archive_member_count_bound_is_enforced(self):
        prefix = fetch_schema_stdlib.LOCKED_PREFIX
        members = [
            (f"{prefix}/", b"", 0o755, tarfile.DIRTYPE, ""),
            (
                f"{prefix}/Lib/logging/__init__.py",
                b"init",
                0o644,
                tarfile.REGTYPE,
                "",
            ),
            (
                f"{prefix}/Lib/logging/config.py",
                b"config",
                0o644,
                tarfile.REGTYPE,
                "",
            ),
            (
                f"{prefix}/Lib/logging/handlers.py",
                b"handlers",
                0o644,
                tarfile.REGTYPE,
                "",
            ),
        ]
        with patch.object(fetch_schema_stdlib, "MAX_MEMBERS", 3):
            with self.assertRaisesRegex(ValueError, "member bound"):
                self._inspect_fixture(members)

    def test_ci_guard_precedes_pin_checks_and_destination_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp).resolve() / "must-not-exist"
            with (
                patch.object(fetch_schema_stdlib.sys, "platform", "darwin"),
                patch.dict(os.environ, {}, clear=True),
                patch.object(
                    fetch_schema_stdlib,
                    "_source_pin",
                    side_effect=AssertionError,
                ),
                patch.object(
                    fetch_schema_stdlib.shutil,
                    "disk_usage",
                    side_effect=AssertionError,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "restricted to Linux CI"):
                    fetch_schema_stdlib.run(destination)
            self.assertFalse(destination.exists())

    def test_disk_floor_rejects_before_pin_or_destination_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp).resolve() / "must-not-exist"
            with (
                patch.object(fetch_schema_stdlib.sys, "platform", "linux"),
                patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
                patch.object(
                    fetch_schema_stdlib,
                    "_source_pin",
                    side_effect=AssertionError,
                ),
                patch.object(
                    fetch_schema_stdlib.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(
                        free=fetch_schema_stdlib.DISK_FLOOR - 1
                    ),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "disk floor"):
                    fetch_schema_stdlib.run(destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
