import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_git as git


class GitInputTests(unittest.TestCase):
    def test_exact_commit_bytes_and_unique_tree_header_required(self):
        tree = "a" * 40
        data = f"tree {tree}\nauthor Test\ncommitter Test\n\nmessage\n".encode()
        identity = hashlib.sha1(b"commit " + str(len(data)).encode() + b"\0" + data).hexdigest()
        git.validate_commit(data, identity, tree)
        for changed, changed_id, changed_tree in ((data + b"x", identity, tree),
                                                  (data, "b" * 40, tree),
                                                  (data, identity, "b" * 40)):
            with self.assertRaises(ValueError):
                git.validate_commit(changed, changed_id, changed_tree)
        duplicated = f"tree {tree}\ntree {tree}\n\nmessage\n".encode()
        duplicate_id = hashlib.sha1(b"commit " + str(len(duplicated)).encode() + b"\0" + duplicated).hexdigest()
        with self.assertRaisesRegex(ValueError, "root tree"):
            git.validate_commit(duplicated, duplicate_id, tree)

    def test_only_two_explicit_fetches_and_no_inherited_authority_or_checkout(self):
        work = Path("/tmp/space ' $(must-not-run)")
        lines = [shlex.split(line) for line in git.script(work).splitlines()]
        exports = [word for line in lines if line[0] == "export" for word in line[1:]]
        self.assertIn("HOME=" + str(work / "home"), exports)
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", exports)
        self.assertIn("GIT_CONFIG_NOSYSTEM=1", exports)
        self.assertIn("GIT_TERMINAL_PROMPT=0", exports)
        self.assertIn("GIT_ALLOW_PROTOCOL=https", exports)
        fetches = [line for line in lines if "fetch" in line]
        self.assertEqual(len(fetches), 2)
        for command, entry in zip(fetches, git.INPUTS):
            self.assertNotIn("--depth=1", command)
            self.assertIn("--no-recurse-submodules", command)
            self.assertEqual(command[-2:], [entry["url"], "+" + entry["commit"] + ":refs/tobkiri/input"])
            self.assertIn("core.hooksPath=/dev/null", command)
            self.assertIn("credential.helper=", command)
            self.assertIn("fetch.fsckObjects=true", command)
        self.assertFalse(any("checkout" in line or "cargo" in line for line in lines))

    def test_root_pin_matches_historical_dependency_receipt(self):
        recorded = json.loads((Path(git.__file__).parent /
            "trials/2026-10-19/producer-receipt.json").read_text())["sources"]
        self.assertEqual(git.INPUTS[0]["commit"], recorded["approved_wit_dylib_git_commit"])
        self.assertEqual(git.INPUTS[0]["tree"], recorded["approved_git_tree"])
        self.assertEqual(git.INPUTS[1]["url"], "https://github.com/WebAssembly/testsuite")

    def test_local_run_never_fetches_or_creates_output(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(git.sys, "platform", "darwin"), \
                patch.object(git, "run_stage", side_effect=AssertionError("fetch started")):
            destination = Path(directory) / "new"
            with self.assertRaisesRegex(ValueError, "Linux CI"):
                git.acquire(destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
