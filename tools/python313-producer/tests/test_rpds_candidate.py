"""Offline input/ABI envelope regressions; no Cargo, Wasmtime or guest runs."""

from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import build_rpds
import fetch_rpds

spec = importlib.util.spec_from_file_location(
    "rpds_probe_driver", HERE / "rpds_probe/driver.py"
)
driver = importlib.util.module_from_spec(spec)
before = sys.path[:]
spec.loader.exec_module(driver)
sys.path[:] = before


def lock(*, source: str = fetch_rpds.REGISTRY, count: int = 18) -> bytes:
    """Synthetic graph exercises policy separately from the real immutable pin."""
    result = [
        f'version = 4\n[[package]]\nname = "rpds-py"\nversion = "{fetch_rpds.VERSION}"\n'
    ]
    for index in range(count):
        result.append(
            f'[[package]]\nname = "dep{index}"\nversion = "1.0.0"\n'
            f'source = "{source}"\nchecksum = "{"ab" * 32}"\n'
        )
    return "\n".join(result).encode()


class RpdsCandidateTests(unittest.TestCase):
    def test_real_source_pin_is_required_before_any_graph_is_accepted(self):
        data = lock()
        with self.assertRaisesRegex(ValueError, "identity"):
            fetch_rpds.packages(data)
        with patch.object(fetch_rpds, "LOCK_SHA256", hashlib.sha256(data).hexdigest()):
            self.assertEqual(len(fetch_rpds.packages(data)), 18)

    def test_git_dependency_and_incomplete_graph_never_replace_locked_registry(self):
        for data in (lock(source="git+https://example.invalid/repo"), lock(count=17)):
            with patch.object(
                fetch_rpds, "LOCK_SHA256", hashlib.sha256(data).hexdigest()
            ):
                with self.assertRaises(ValueError):
                    fetch_rpds.packages(data)

    def test_source_archive_identity_and_links_are_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source"
            with self.assertRaisesRegex(ValueError, "identity"):
                fetch_rpds.extract_source(b"not the pinned archive", output)
            self.assertFalse(output.exists())
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                member = tarfile.TarInfo(fetch_rpds.PREFIX + "/escape")
                member.type, member.linkname = tarfile.SYMTYPE, "../../private"
                archive.addfile(member)
            data = stream.getvalue()
            with patch.object(
                fetch_rpds, "ARCHIVE_SHA256", hashlib.sha256(data).hexdigest()
            ):
                with self.assertRaises(ValueError):
                    fetch_rpds.extract_source(data, output)
            self.assertFalse(output.exists())

    def test_build_and_fetch_refuse_non_ci_before_tools_or_destinations(self):
        missing = Path("/must-not-be-created")
        with (
            patch.object(sys, "platform", "darwin"),
            patch.dict("os.environ", {}, clear=True),
        ):
            with self.assertRaisesRegex(ValueError, "CI"):
                fetch_rpds.run(missing)
            with self.assertRaisesRegex(ValueError, "CI"):
                build_rpds.run(missing, missing, missing, missing, missing, missing)

    def test_core_wasm_metadata_envelope_is_checked_without_running_it(self):
        header = b"\0asm\x01\0\0\0"
        self.assertEqual(
            driver.custom_sections(header + b"\0\x09\x08dylink.0"), ["dylink.0"]
        )
        for data in (
            b"\0asm\x0d\0\x01\0",
            header + b"\0\xff\xff\xff\xff\xff\0",
            header + b"\0\x10\x08dylink.0",
            header + b"\0\x01\x09",
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                driver.custom_sections(data)

    def test_private_initializer_is_required_inside_and_rejected_outside(self):
        driver.require_boundary([], [driver.INTERFACE, "exports"], inner=True)
        driver.require_boundary([], [driver.INTERFACE], inner=False)
        for imports, exports, inner in (
            ([], [driver.INTERFACE], True),
            ([], [driver.INTERFACE, "exports"], False),
            (["wasi:filesystem/types@0.2.0"], [driver.INTERFACE], False),
            ([], [driver.INTERFACE, driver.INTERFACE], False),
            ([], [driver.INTERFACE, "other"], False),
        ):
            with self.subTest(imports=imports, exports=exports, inner=inner):
                with self.assertRaisesRegex(ValueError, "authority"):
                    driver.require_boundary(imports, exports, inner=inner)

    def test_experiment_is_separate_from_historical_sources_and_normal_pack_builds(
        self,
    ):
        self.assertIn("--frozen", build_rpds.RECIPE)
        self.assertIn("CARGO_NET_OFFLINE=true", build_rpds.RECIPE)
        self.assertIn("--features pyo3/abi3-py313", build_rpds.RECIPE)
        self.assertIn("abi3=true", build_rpds.PYO3_CONFIG)
        self.assertIn("pointer_width=32", build_rpds.PYO3_CONFIG)
        self.assertNotIn("python313-source.patch", build_rpds.RECIPE)
        source = (HERE / "rpds_probe/driver.py").read_text()
        self.assertLess(
            source.index("if not limited:"), source.index('stage / "rpds.abi3.so"')
        )
        self.assertIn("function.post_return(store)", source)
        self.assertIn("timer.join()", source)
        self.assertIn('"production_enabled": False', source)


if __name__ == "__main__":
    unittest.main()
