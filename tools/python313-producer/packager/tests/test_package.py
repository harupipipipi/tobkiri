"""Pure driver tests: mocked worker transport is not actual Wasm validation."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("package", HERE / "package.py")
assert SPEC and SPEC.loader
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)


class DriverTests(unittest.TestCase):
    """Test bounded manifests and publication without executing any worker."""

    def setUp(self) -> None:
        """Create self-authored fake input bytes; never claim they are real Wasm."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worker = self.root / "worker"
        self.worker.write_bytes(b"unexecuted test fixture")
        self.worker_hash = PACKAGE.digest(self.worker.read_bytes())
        self.records = []
        for index, asset in enumerate(sorted(PACKAGE.ASSETS)):
            name = f"input{index}"
            data = b"\0asm" + name.encode()
            (self.root / name).write_bytes(data)
            self.records.append({"asset": asset, "path": name,
                                 "member": ("module.py"
                                            if asset in PACKAGE.TAR_ASSETS else None),
                                 "sha256": PACKAGE.digest(data), "mode": 0o644})
        self.manifest = self.root / "inputs.json"
        self.destination = self.root / "output"
        self.save_manifest()

    def save_manifest(self) -> None:
        """Update synthetic manifest and its caller supplied identity."""
        self.manifest.write_text(json.dumps(
            {"schema_version": 1, "files": self.records}
        ))
        self.manifest_hash = PACKAGE.digest(self.manifest.read_bytes())

    def run_package(self) -> dict:
        """Call driver with a transport mock installed by each publication test."""
        return PACKAGE.package(self.root, self.manifest, self.manifest_hash,
                               self.worker, self.worker_hash, self.destination)

    def test_publish_thirteen_assets(self) -> None:
        def fake_worker(worker: Path, mode: str, data: bytes) -> bytes:
            return b"\x28\xb5\x2f\xfd" + mode.encode() + data
        with mock.patch.object(
            PACKAGE, "compress", side_effect=fake_worker
        ) as transport:
            result = self.run_package()
        self.assertEqual(result["assets"], 13)
        self.assertEqual(transport.call_count, 13)
        receipt = json.loads((self.destination / "package-receipt.json").read_text())
        self.assertEqual(len(receipt["artifacts"]), 13)

    def test_duplicate_final_runtimes_rejected(self) -> None:
        runtimes = [r for r in self.records if "runtime_" in r["asset"]]
        inputs = [(self.root / r["path"]).read_bytes() for r in runtimes]
        self.assertNotEqual(inputs[0], inputs[1])

        def fake_worker(worker: Path, mode: str, data: bytes) -> bytes:
            if data in inputs:
                # Simulate differences residing solely in stripped metadata.
                return b"\x28\xb5\x2f\xfdidentical stripped runtime"
            return b"\x28\xb5\x2f\xfd" + mode.encode() + data

        with mock.patch.object(PACKAGE, "compress", side_effect=fake_worker):
            with self.assertRaisesRegex(ValueError, "packaged runtimes"):
                self.run_package()
        self.assertFalse(self.destination.exists())
        partial = next(self.root.glob(".output.partial-*"))
        self.assertTrue((partial / "FAILED.txt").is_file())
        receipt = json.loads((partial / "package-receipt.json").read_text())
        self.assertTrue(receipt["status"].startswith("failed"))
        artifacts = receipt["artifacts_completed"]
        self.assertEqual(
            artifacts["libcomponentize_py_runtime_sync.so.zst"]["sha256"],
            artifacts["libcomponentize_py_runtime_async.so.zst"]["sha256"],
        )

    def test_manifest_digest_mismatch(self) -> None:
        self.manifest.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            self.run_package()

    def test_worker_digest_mismatch(self) -> None:
        self.worker.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "worker digest"):
            self.run_package()

    def test_input_digest_mismatch(self) -> None:
        (self.root / self.records[0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "input digest"):
            self.run_package()

    def test_missing_asset(self) -> None:
        self.records.pop()
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "13 real assets"):
            self.run_package()

    def test_duplicate_runtimes_rejected(self) -> None:
        runtimes = [r for r in self.records if "runtime_" in r["asset"]]
        data = (self.root / runtimes[0]["path"]).read_bytes()
        (self.root / runtimes[1]["path"]).write_bytes(data)
        runtimes[1]["sha256"] = PACKAGE.digest(data)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.run_package()

    def test_symlink_input_rejected(self) -> None:
        self.records[0]["path"] = "link"
        (self.root / "link").symlink_to(self.worker)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_package()

    def test_unsafe_paths(self) -> None:
        for name in ("../escape", "/escape", "a//b", "a/./b", "a\\b", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                PACKAGE.safe_name(name)

    def test_excluded_tar_member(self) -> None:
        record = next(r for r in self.records if r["asset"] in PACKAGE.TAR_ASSETS)
        record["member"] = "__pycache__/file.pyc"
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "excluded"):
            self.run_package()

    def test_duplicate_member(self) -> None:
        record = dict(next(r for r in self.records if r["asset"] in PACKAGE.TAR_ASSETS))
        record["path"] = "extra"
        (self.root / "extra").write_bytes(b"\0asmextra")
        record["sha256"] = PACKAGE.digest(b"\0asmextra")
        self.records.append(record)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "duplicate asset member"):
            self.run_package()

    def test_input_limits(self) -> None:
        with mock.patch.object(PACKAGE, "MAX_TOTAL", 1):
            with self.assertRaisesRegex(ValueError, "aggregate"):
                self.run_package()
        with mock.patch.object(PACKAGE, "MAX_FILE", 1):
            with self.assertRaisesRegex(ValueError, "oversized"):
                self.run_package()

    def test_worker_failure_preserves_partial(self) -> None:
        with mock.patch.object(
            PACKAGE, "compress", side_effect=ValueError("worker failed")
        ):
            with self.assertRaisesRegex(ValueError, "worker failed"):
                self.run_package()
        self.assertFalse(self.destination.exists())
        partial = next(self.root.glob(".output.partial-*"))
        self.assertTrue((partial / "FAILED.txt").is_file())
        receipt = json.loads((partial / "package-receipt.json").read_text())
        self.assertTrue(receipt["status"].startswith("failed"))

    def test_invalid_compressor_output(self) -> None:
        with mock.patch.object(PACKAGE, "compress", return_value=b"not zstd"):
            with self.assertRaisesRegex(ValueError, "not a zstd frame"):
                self.run_package()
        self.assertFalse(self.destination.exists())

    def test_tar_collision(self) -> None:
        record = dict(next(r for r in self.records if r["asset"] in PACKAGE.TAR_ASSETS))
        record["path"] = "extra"
        record["member"] += "/child.py"
        data = b"child"
        (self.root / "extra").write_bytes(data)
        record["sha256"] = PACKAGE.digest(data)
        self.records.append(record)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "collision"):
            self.run_package()

    def test_existing_output_untouched(self) -> None:
        self.destination.mkdir()
        with self.assertRaisesRegex(ValueError, "new"):
            self.run_package()

    def test_tar_order_metadata_content(self) -> None:
        entries = [{"member": "z.py", "data": b"last", "mode": 0o755},
                   {"member": "a.py", "data": b"first", "mode": 0o644}]
        one = PACKAGE.tar_bytes(entries)
        self.assertEqual(one, PACKAGE.tar_bytes(list(reversed(entries))))
        with tarfile.open(fileobj=io.BytesIO(one)) as tar:
            members = tar.getmembers()
            self.assertEqual([m.name for m in members], ["a.py", "z.py"])
            self.assertEqual([m.mode for m in members], [0o644, 0o755])
            self.assertTrue(all(m.uid == m.gid == m.mtime == 0 for m in members))
            self.assertEqual(tar.extractfile(members[0]).read(), b"first")


class TransportTests(unittest.TestCase):
    """Mock pipes/process groups to check timeout and read-time output bounds."""

    def test_timeout_kills_group(self) -> None:
        process = mock.MagicMock()
        process.pid = 12345
        selector = mock.MagicMock()
        selector.get_map.return_value = {1: "pending"}
        with mock.patch.object(PACKAGE.subprocess, "Popen", return_value=process), \
             mock.patch.object(PACKAGE.selectors, "DefaultSelector") as factory, \
             mock.patch.object(PACKAGE.os, "set_blocking"), \
             mock.patch.object(PACKAGE.os, "killpg") as kill, \
             mock.patch.object(PACKAGE.time, "monotonic", side_effect=[0, 241]):
            factory.return_value.__enter__.return_value = selector
            with self.assertRaises(TimeoutError):
                PACKAGE.compress(Path("/unexecuted"), "tar", b"payload")
            kill.assert_called_once_with(12345, PACKAGE.signal.SIGKILL)
            process.wait.assert_called_once()

    def test_output_cap_kills_group_during_read(self) -> None:
        process = mock.MagicMock()
        process.pid = 12345
        selector = mock.MagicMock()
        selector.get_map.return_value = {1: "pending"}
        selector.select.return_value = [(mock.Mock(fileobj=process.stdout), 1)]
        with mock.patch.object(PACKAGE.subprocess, "Popen", return_value=process), \
             mock.patch.object(PACKAGE.selectors, "DefaultSelector") as factory, \
             mock.patch.object(PACKAGE.os, "set_blocking"), \
             mock.patch.object(PACKAGE.os, "read", return_value=b"too large"), \
             mock.patch.object(PACKAGE.os, "killpg") as kill, \
             mock.patch.object(PACKAGE, "MAX_OUTPUT", 1):
            factory.return_value.__enter__.return_value = selector
            with self.assertRaisesRegex(ValueError, "output limit"):
                PACKAGE.compress(Path("/unexecuted"), "tar", b"payload")
            kill.assert_called_once_with(12345, PACKAGE.signal.SIGKILL)
            process.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
