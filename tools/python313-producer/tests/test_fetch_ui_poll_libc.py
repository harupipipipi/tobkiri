"""Offline tests for the pinned, data-only wasi-libc source acquisition."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_ui_poll_libc


def _tar_bytes(
    poll: bytes, extra: list[tuple[str, bytes, int, bytes]] | None = None
) -> bytes:
    """Build a tiny synthetic archive with a regular pinned poll.c member."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        root = tarfile.TarInfo(fetch_ui_poll_libc.PREFIX + "/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)

        directories = (
            "libc-bottom-half",
            "libc-bottom-half/cloudlibc",
            "libc-bottom-half/cloudlibc/src",
            "libc-bottom-half/cloudlibc/src/libc",
            "libc-bottom-half/cloudlibc/src/libc/poll",
            "tools",
            "empty",
        )
        for relative in directories:
            directory = tarfile.TarInfo(f"{fetch_ui_poll_libc.PREFIX}/{relative}/")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)

        members = [
            (fetch_ui_poll_libc.POLL_PATH, poll, 0o644, tarfile.REGTYPE),
            ("tools/run.sh", b"#!/bin/sh\nexit 0\n", 0o755, tarfile.REGTYPE),
            ("README.md", b"source only\n", 0o644, tarfile.REGTYPE),
        ]
        if extra:
            members.extend(extra)
        for relative, content, mode, kind in members:
            item = tarfile.TarInfo(f"{fetch_ui_poll_libc.PREFIX}/{relative}")
            item.type = kind
            item.mode = mode
            item.size = (
                len(content) if kind in (tarfile.REGTYPE, tarfile.AREGTYPE) else 0
            )
            if kind in (tarfile.REGTYPE, tarfile.AREGTYPE):
                archive.addfile(item, io.BytesIO(content))
            else:
                item.linkname = content.decode("utf-8")
                archive.addfile(item)
    return output.getvalue()


class _Response(io.BytesIO):
    def geturl(self) -> str:
        return fetch_ui_poll_libc.URL


@contextmanager
def _ci_for(archive: bytes, poll: bytes):
    """Patch only the pin and download boundary for a synthetic archive."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(fetch_ui_poll_libc.sys, "platform", "linux"))
        stack.enter_context(patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}))
        stack.enter_context(
            patch.object(fetch_ui_poll_libc, "ARCHIVE_BYTES", len(archive))
        )
        stack.enter_context(
            patch.object(
                fetch_ui_poll_libc,
                "ARCHIVE_SHA256",
                hashlib.sha256(archive).hexdigest(),
            )
        )
        stack.enter_context(
            patch.object(
                fetch_ui_poll_libc, "POLL_SHA256", hashlib.sha256(poll).hexdigest()
            )
        )
        stack.enter_context(patch.object(fetch_ui_poll_libc, "DISK_FLOOR", 0))
        stack.enter_context(
            patch.object(
                fetch_ui_poll_libc.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=16 * 1024**3),
            )
        )
        stack.enter_context(
            patch.object(fetch_ui_poll_libc, "urlopen", return_value=_Response(archive))
        )
        yield


class FetchUiPollLibcTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_run_verify_inventory_and_executable_modes(self):
        poll = b"original poll source\n"
        archive = _tar_bytes(poll)
        destination = self.root / "inputs"
        with _ci_for(archive, poll):
            receipt = fetch_ui_poll_libc.run(destination)
            verification = fetch_ui_poll_libc.verify(destination)

        self.assertEqual(receipt["source_root"], "source")
        self.assertFalse(receipt["build_performed"])
        self.assertFalse(receipt["production_enabled"])
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["poll"]["sha256"], hashlib.sha256(poll).hexdigest())
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["files"], 3)
        self.assertEqual(verification["directories"], 7)
        self.assertEqual(
            (destination / "source" / "tools" / "run.sh").stat().st_mode & 0o777,
            0o700,
        )
        self.assertEqual(
            (destination / "source" / "README.md").stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(
            (destination / "source" / "README.md").read_bytes(), b"source only\n"
        )
        self.assertEqual(
            (destination / "source" / "tools" / "run.sh").read_bytes(),
            b"#!/bin/sh\nexit 0\n",
        )
        self.assertTrue((destination / "source.tar.gz").is_file())

    def test_source_or_receipt_tampering_is_rejected(self):
        poll = b"original poll source\n"
        archive = _tar_bytes(poll)
        destination = self.root / "inputs"
        with _ci_for(archive, poll):
            fetch_ui_poll_libc.run(destination)
            staged_poll = destination / "source" / fetch_ui_poll_libc.POLL_PATH
            staged_poll.write_bytes(b"changed poll source\n")
            staged_poll.chmod(0o600)
            with self.assertRaises(ValueError):
                fetch_ui_poll_libc.verify(destination)

            staged_poll.write_bytes(poll)
            staged_poll.chmod(0o600)
            receipt_path = destination / "source-inputs.json"
            receipt = json.loads(receipt_path.read_text("utf-8"))
            receipt["production_enabled"] = True
            receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            receipt_path.chmod(0o600)
            with self.assertRaises(ValueError):
                fetch_ui_poll_libc.verify(destination)

    def test_path_escape_link_duplicate_and_size_caps_fail(self):
        poll = b"original poll source\n"
        cases = (
            (
                "path escape",
                [("../escape", b"bad", 0o644, tarfile.REGTYPE)],
                "unsafe wasi-libc archive path",
                None,
            ),
            (
                "link",
                [("docs/link", b"../../outside", 0o777, tarfile.SYMTYPE)],
                "unsupported wasi-libc archive member",
                None,
            ),
            (
                "duplicate",
                [("README.md", b"again", 0o644, tarfile.REGTYPE)],
                "duplicate or colliding wasi-libc path",
                None,
            ),
            (
                "file cap",
                [("large.bin", b"x" * 31, 0o644, tarfile.REGTYPE)],
                "wasi-libc file exceeds its bound",
                30,
            ),
            (
                "special mode",
                [("bad-mode", b"bad", 0o4755, tarfile.REGTYPE)],
                "special mode bits",
                None,
            ),
        )
        for title, extra, message, file_cap in cases:
            with self.subTest(title=title):
                archive = _tar_bytes(poll, extra)
                destination = self.root / title.replace(" ", "-")
                with _ci_for(archive, poll):
                    if file_cap is not None:
                        cap = patch.object(
                            fetch_ui_poll_libc, "MAX_FILE_BYTES", file_cap
                        )
                    else:
                        cap = patch.object(
                            fetch_ui_poll_libc,
                            "MAX_FILE_BYTES",
                            fetch_ui_poll_libc.MAX_FILE_BYTES,
                        )
                    with cap, self.assertRaisesRegex(ValueError, message):
                        fetch_ui_poll_libc.run(destination)
                self.assertFalse((destination / "source-inputs.json").exists())

    def test_expanded_byte_and_member_bounds_fail(self):
        poll = b"original poll source\n"
        archive = _tar_bytes(poll)
        for label, constant, value, message in (
            ("expanded", "MAX_EXPANDED_BYTES", 20, "expanded source exceeds"),
            ("members", "MAX_MEMBERS", 2, "member bound exceeded"),
        ):
            with self.subTest(label=label):
                destination = self.root / label
                with (
                    _ci_for(archive, poll),
                    patch.object(fetch_ui_poll_libc, constant, value),
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        fetch_ui_poll_libc.run(destination)
                self.assertFalse((destination / "source-inputs.json").exists())

    def test_cold_ci_guard_runs_before_destination_mutation(self):
        for label, platform, actions in (
            ("non-linux", "darwin", "true"),
            ("no-actions", "linux", None),
        ):
            with self.subTest(label=label):
                destination = self.root / label
                environment = {"GITHUB_ACTIONS": actions} if actions else {}
                with (
                    patch.object(fetch_ui_poll_libc.sys, "platform", platform),
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(fetch_ui_poll_libc, "urlopen") as opened,
                ):
                    with self.assertRaisesRegex(ValueError, "restricted to Linux CI"):
                        fetch_ui_poll_libc.run(destination)
                opened.assert_not_called()
                self.assertFalse(destination.exists())

    def test_archive_and_staged_hardlinks_are_rejected(self):
        poll = b"original poll source\n"
        archive = _tar_bytes(poll)
        destination = self.root / "inputs"
        with _ci_for(archive, poll):
            fetch_ui_poll_libc.run(destination)
            source_file = destination / "source" / "README.md"
            os.link(source_file, destination / "source" / "README-copy.md")
            with self.assertRaisesRegex(ValueError, "hard-linked"):
                fetch_ui_poll_libc.verify(destination)

        # The archive pin check rejects the same bytes when they have a second name.
        archive_path = destination / "source.tar.gz"
        alias = self.root / "archive-copy.tar.gz"
        os.link(archive_path, alias)
        with _ci_for(archive, poll):
            with self.assertRaisesRegex(ValueError, "unlinked regular file"):
                fetch_ui_poll_libc._archive_identity(alias)


if __name__ == "__main__":
    unittest.main()
