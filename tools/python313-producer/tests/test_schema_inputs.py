"""Offline tests for lock-pinned JSON Schema wheel acquisition and verification."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import archive_inputs
import fetch_schema
from build_native import fingerprint
import fetch_sdk


def digest(data: bytes) -> str:
    """Return a fixture SHA-256 digest."""
    return hashlib.sha256(data).hexdigest()


def zip_file(
    archive: zipfile.ZipFile,
    name: str,
    data: bytes,
    mode: int = stat.S_IFREG | 0o644,
) -> None:
    """Write one regular or special ZIP member with an explicit Unix mode."""
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = mode << 16
    archive.writestr(member, data)


def wheel_bytes(distribution: str, version: str) -> bytes:
    """Build a tiny synthetic wheel with code, resources and dist-info data."""
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    files = {
        f"{normalized}/__init__.py": b"# synthetic fixture; never imported\n",
        f"{normalized}/schemas/fixture.json": b'{"type":"object"}\n',
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
        ).encode(),
        f"{dist_info}/RECORD": b"synthetic wheel record\n",
    }
    import io

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            zip_file(archive, name, data)
    return output.getvalue()


class SchemaInputTests(unittest.TestCase):
    def test_canonical_lock_yields_five_pure_wheels_and_native_oracle(self):
        selected = fetch_schema.selected_inputs(fetch_schema.LOCK_PATH)
        self.assertEqual(
            [
                (item["role"], item["distribution"], item["version"])
                for item in selected
            ],
            [
                ("pure", "attrs", "26.1.0"),
                ("pure", "jsonschema", "4.26.0"),
                ("pure", "jsonschema-specifications", "2025.9.1"),
                ("pure", "referencing", "0.37.0"),
                ("pure", "packaging", "26.2"),
                ("native-oracle", "rpds-py", "2026.6.3"),
            ],
        )
        self.assertEqual(
            [item["bytes"] for item in selected],
            [67548, 90630, 18437, 26766, 100195, 365678],
        )
        self.assertEqual(
            [item["sha256"] for item in selected],
            [
                "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309",
                "d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce",
                "98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe",
                "381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231",
                "5fc45236b9446107ff2415ce77c807cee2862cb6fac22b8a73826d0693b0980e",
                "acac386b453c2516111b50985d60ce46e7fadb5ea71ae7b25f4c946935bf27cf",
            ],
        )
        self.assertEqual(sum(item["bytes"] for item in selected), 669254)
        self.assertEqual(
            selected[-1]["filename"],
            "rpds_py-2026.6.3-cp313-cp313-manylinux_2_17_x86_64."
            "manylinux2014_x86_64.whl",
        )
        self.assertEqual(
            selected[-1]["sha256"],
            "acac386b453c2516111b50985d60ce46e7fadb5ea71ae7b25f4c946935bf27cf",
        )
        self.assertLess(
            sum(item["bytes"] for item in selected),
            fetch_schema.MAX_DOWNLOADS,
        )

    def test_changed_lock_version_and_wheel_pin_fail_lock_identity(self):
        original = fetch_schema.LOCK_PATH.read_bytes()
        changed_inputs = [
            original + b"\n",
            original.replace(
                b'name = "jsonschema"\nversion = "4.26.0"',
                b'name = "jsonschema"\nversion = "4.26.1"',
                1,
            ),
            original.replace(
                b"c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309",
                b"0" * 64,
                1,
            ),
        ]
        self.assertEqual(len(changed_inputs[1]), len(original))
        self.assertEqual(len(changed_inputs[2]), len(original))
        for changed in changed_inputs:
            with (
                self.subTest(changed_hash=digest(changed)),
                tempfile.TemporaryDirectory() as temp,
            ):
                lock = Path(temp) / "uv.lock"
                lock.write_bytes(changed)
                with self.assertRaisesRegex(ValueError, "lock identity differs"):
                    fetch_schema.selected_inputs(lock)

    def test_duplicate_package_and_unsupported_lock_selection_are_rejected(self):
        original = fetch_schema.LOCK_PATH.read_bytes()
        duplicate = original + (
            b"\n[[package]]\n"
            b'name = "attrs"\n'
            b'version = "26.1.0"\n'
            b'source = { registry = "https://pypi.org/simple" }\n'
            b'wheels = [{ url = "https://files.pythonhosted.org/packages/'
            b"64/b4/17d4b0b2a2dc85a6df63d1157e028ed19f90d4cd97c36717afef2bc2f395/"
            b'attrs-26.1.0-py3-none-any.whl", hash = '
            b'"sha256:c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043'
            b'ac309", '
            b"size = 67548 }]\n"
        )
        unsupported = original.replace(
            b"attrs-26.1.0-py3-none-any.whl",
            b"attrs-26.1.0-cp313-cp313-wasm32.whl",
            1,
        )
        changed_version = original.replace(
            b'name = "jsonschema"\nversion = "4.26.0"',
            b'name = "jsonschema"\nversion = "4.26.1"',
            1,
        )
        for changed, message in (
            (duplicate, "duplicated or missing"),
            (unsupported, "supported locked wheel"),
            (changed_version, "unsupported schema dependency version"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp:
                lock = Path(temp) / "uv.lock"
                lock.write_bytes(changed)
                with patch.object(fetch_schema, "LOCK_SHA256", digest(changed)):
                    with self.assertRaisesRegex(ValueError, message):
                        fetch_schema.selected_inputs(lock)

    def _offline_stage(self, destination: Path):
        """Stage synthetic wheel bytes through the production data-only path."""
        pins = []
        contents = {}
        for locked in fetch_schema.selected_inputs(fetch_schema.LOCK_PATH):
            data = wheel_bytes(locked["distribution"], locked["version"])
            pin = {**locked, "sha256": digest(data), "bytes": len(data)}
            pins.append(pin)
            contents[pin["url"]] = data

        def download(url, archive, expected, maximum):
            data = contents[url]
            if len(data) > maximum or digest(data) != expected:
                raise AssertionError("synthetic wheel pin was not passed through")
            with archive.open("xb") as output:
                output.write(data)
            return len(data)

        with (
            patch.object(fetch_schema, "_select_from_lock", return_value=pins),
            patch.object(fetch_schema.fetch_rpds, "download", side_effect=download),
            patch.object(fetch_schema.sys, "platform", "linux"),
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            patch.object(
                fetch_schema.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=fetch_schema.DISK_FLOOR),
            ),
        ):
            receipt = fetch_schema.run(destination)
            verified = fetch_schema.verify(destination)
        return receipt, verified, pins

    def test_data_only_stage_keeps_package_resources_and_native_oracle_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp).resolve() / "schema-inputs"
            receipt, verified, _ = self._offline_stage(destination)
            self.assertEqual(len(receipt["inputs"]), 6)
            self.assertEqual(verified["packages"], 6)
            self.assertTrue(
                (
                    destination / "packages/jsonschema/jsonschema/schemas/fixture.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    destination
                    / "packages/jsonschema/jsonschema-4.26.0.dist-info/METADATA"
                ).is_file()
            )
            self.assertTrue(
                (
                    destination
                    / "packages/rpds-py-native/rpds_py-2026.6.3.dist-info/METADATA"
                ).is_file()
            )
            self.assertEqual(
                {path.name for path in (destination / "packages").iterdir()},
                {
                    "attrs",
                    "jsonschema",
                    "jsonschema-specifications",
                    "referencing",
                    "packaging",
                    "rpds-py-native",
                },
            )

    def test_forged_tree_and_receipt_cannot_override_pinned_wheel(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp).resolve() / "schema-inputs"
            _, _, pins = self._offline_stage(destination)
            package = destination / "packages/attrs"
            module = package / "attrs/__init__.py"
            module.write_bytes(b"forged contents\n")
            receipt_path = destination / "schema-inputs.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["inputs"][0]["files"] = fetch_sdk.inventory(package)
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with patch.object(fetch_schema, "_select_from_lock", return_value=pins):
                with self.assertRaisesRegex(
                    ValueError,
                    "differs from its pinned wheel",
                ):
                    fetch_schema.verify(destination)

    def _inspect_fixture(self, entries):
        """Inspect one tiny malicious wheel using the shared archive policy."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.whl"
            with zipfile.ZipFile(path, "w") as archive:
                for name, data, mode in entries:
                    zip_file(archive, name, data, mode)
            identity = fingerprint(path, 1024 * 1024)
            return archive_inputs.inspect_wheel(
                path,
                identity["sha256"],
                maximum=1024 * 1024,
                expected_bytes=identity["bytes"],
                max_file=1024,
                max_expanded=4096,
                max_members=32,
            )

    def test_unsafe_zip_paths_symlinks_and_file_directory_collisions_fail(self):
        fixtures = [
            [("../escape.py", b"bad", stat.S_IFREG | 0o644)],
            [("pkg/link.py", b"target", stat.S_IFLNK | 0o777)],
            [
                ("pkg", b"file", stat.S_IFREG | 0o644),
                ("pkg/child.py", b"child", stat.S_IFREG | 0o644),
            ],
        ]
        for entries in fixtures:
            with self.subTest(entries=[name for name, _, _ in entries]):
                with self.assertRaises(ValueError):
                    self._inspect_fixture(entries)

    def test_ci_guard_rejects_before_checking_or_creating_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp).resolve() / "must-not-exist"
            with (
                patch.object(fetch_schema.sys, "platform", "darwin"),
                patch.dict(os.environ, {}, clear=True),
                patch.object(fetch_schema, "_lock_bytes", side_effect=AssertionError),
                patch.object(
                    fetch_schema.shutil,
                    "disk_usage",
                    side_effect=AssertionError,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "restricted to Linux CI"):
                    fetch_schema.run(destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
