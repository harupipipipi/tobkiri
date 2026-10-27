"""Exercise fixture source staging without starting the original runtime."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


HELPER = Path(__file__).parent / "support" / "activate_fixture_profile.py"
SPEC = importlib.util.spec_from_file_location("profile_fixture_source", HELPER)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def put(root: Path, relative: str, payload: bytes) -> Path:
    """Write a source-processing fixture, never an application identity."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


class ProfileFixtureSourceTests(unittest.TestCase):
    """A Pack closure cannot silently stand in for the Host import inputs."""

    def test_pack_only_source_fails_before_any_host_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, host = root / "source", root / "host"
            put(source, "ecosystem/defaultspack/defaultspack/a.py", b"original")
            with self.assertRaisesRegex(ValueError, "original Host source is missing"):
                MODULE.stage(source, host)
            self.assertFalse(host.exists())

    def test_original_bytes_and_resources_preserve_the_generated_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, host = root / "source", root / "host"
            for relative in MODULE.HOST_SOURCE_ANCHORS:
                put(source, relative, b"raise AssertionError('must not execute')\n")
            put(source, "tobkiri_host/canonical-files.v1.json", b'{"files": []}')
            put(source, "ecosystem/defaultspack/v4/generated.json", b"source copy")
            generated = put(host, "ecosystem/defaultspack/v4/generated.json", b"generated")
            artifact = put(host, "ecosystem/defaultspack/platform-artifacts/app", b"artifact")
            put(source, "user_data/private.json", b"private data")
            put(source, "ecosystem.json", b"legacy authority")
            MODULE.stage(source, host)
            self.assertEqual(generated.read_bytes(), b"generated")
            self.assertEqual(artifact.read_bytes(), b"artifact")
            self.assertFalse((host / "user_data").exists())
            self.assertFalse((host / "ecosystem.json").exists())
            for relative in MODULE.HOST_SOURCE_ANCHORS:
                self.assertEqual((host / relative).read_bytes(), (source / relative).read_bytes())
            manifest = json.loads((host / MODULE.MANIFEST).read_text())
            entries = {entry["path"]: entry for entry in manifest["entries"]}
            self.assertIn("tobkiri_host/canonical-files.v1.json", entries)
            actual = {
                path.relative_to(host).as_posix()
                for path in host.rglob("*")
                if path.is_file() and path.name != MODULE.MANIFEST
            }
            self.assertEqual(set(entries), actual)
            for relative, entry in entries.items():
                payload = (host / relative).read_bytes()
                self.assertEqual(entry["size"], len(payload))
                self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())

    def test_conflicting_original_host_source_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, host = root / "source", root / "host"
            for relative in MODULE.HOST_SOURCE_ANCHORS:
                put(source, relative, b"source")
            existing = put(host, "core_runtime/__init__.py", b"existing resource")
            with self.assertRaisesRegex(ValueError, "refusing to replace"):
                MODULE.stage(source, host)
            self.assertEqual(existing.read_bytes(), b"existing resource")
            self.assertFalse((host / MODULE.MANIFEST).exists())


if __name__ == "__main__":
    unittest.main()
