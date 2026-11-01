"""Native-stage input/isolation checks; no upstream code or compiler is run."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_native


class NativeStageTests(unittest.TestCase):
    def test_complete_tree_identity_rejects_content_modes_missing_and_extra(self):
        for change in (None, "content", "mode", "missing", "extra", "symlink"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                item = root / "configure"
                item.write_bytes(b"reviewed source")
                item.chmod(0o644)
                manifest = json.dumps({"files": {"configure": {
                    **build_native.fingerprint(item), "mode": "0o644",
                }}}).encode()
                (root / "stage-manifest.json").write_bytes(manifest)
                if change == "content":
                    item.write_bytes(b"modified source")
                elif change == "mode":
                    item.chmod(0o755)
                elif change == "missing":
                    item.unlink()
                elif change == "extra":
                    (root / "unlisted").write_bytes(b"extra")
                elif change == "symlink":
                    item.unlink()
                    item.symlink_to("stage-manifest.json")
                with patch.object(build_native, "STAGED_SHA256",
                                  hashlib.sha256(manifest).hexdigest()):
                    if change is None:
                        self.assertEqual(build_native.verify_tree(root)["files"], 1)
                    else:
                        with self.assertRaises(ValueError):
                            build_native.verify_tree(root)

    def test_manifest_cannot_reauthorize_changed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "stage-manifest.json").write_text('{"files": {}}')
            with self.assertRaisesRegex(ValueError, "manifest identity"):
                build_native.verify_tree(root)

    def test_fingerprint_observes_size_limit_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = Path(tmp) / "input"
            item.write_bytes(b"1234")
            self.assertEqual(build_native.fingerprint(item, 4)["bytes"], 4)
            with self.assertRaisesRegex(ValueError, "size bound"):
                build_native.fingerprint(item, 3)
            link = item.with_name("alias")
            link.symlink_to(item)
            with self.assertRaisesRegex(ValueError, "regular input"):
                build_native.fingerprint(link)

    def test_sandbox_exposes_only_tools_source_and_private_scratch(self):
        command = build_native.sandbox_command(
            Path("/stage"), Path("/private/work"), Path("/reviewed/recipe.sh"))
        self.assertIn("--unshare-all", command)
        self.assertIn("--clearenv", command)
        self.assertIn("--die-with-parent", command)
        self.assertIn("--as=2147483648", command)
        writable = [command[i + 1:i + 3] for i, arg in enumerate(command)
                    if arg == "--bind"]
        self.assertEqual(writable, [["/private/work", "/work"]])
        readonly = [command[i + 1:i + 3] for i, arg in enumerate(command)
                    if arg == "--ro-bind"]
        self.assertEqual(readonly, [[p, p] for p in (
            "/usr", "/bin", "/lib", "/lib64", "/etc/alternatives")]
            + [["/stage/cpython", "/source/cpython"],
               ["/reviewed/recipe.sh", "/recipe.sh"]])

    def test_local_invocation_refused_before_filesystem_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            with patch.dict(build_native.os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "Linux CI"):
                    build_native.run(Path(tmp), output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
