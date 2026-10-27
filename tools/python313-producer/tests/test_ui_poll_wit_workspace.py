"""Exercise derived WIT custody using tiny local data; no build or guest runs."""

import importlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
wit = importlib.import_module("ui_poll_wit_workspace")


class WitWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "candidate"
        self.work = self.root / "work"
        self.work.mkdir()
        for version in wit.VERSIONS:
            path = self.source / "wasi" / version / "wit"
            path.mkdir(parents=True)
            (path / "wasi-libc.wit.in").write_text("package original:libc;\n")
            (path / "deps").mkdir()
            (path / "deps/original.wit").write_text("original dependency\n")
        self.original = {
            version: wit.inventory(self.source / "wasi" / version / "wit")
            for version in wit.VERSIONS
        }

    def prepared(self):
        records = wit.prepare(self.source, self.work)
        for version in wit.VERSIONS:
            (self.work / "derived-wit" / version / wit.OUTPUT).write_text(
                "package derived:libc;\n"
            )
        return records

    def test_only_two_derived_views_change_and_original_bytes_remain(self):
        records = self.prepared()
        observed = wit.observe(self.work, records, successful=True)
        self.assertTrue(observed["copied_inputs_verified_before_and_after"])
        self.assertFalse(observed["copied_inputs_continuously_immutable"])
        self.assertFalse(observed["generated_wit_is_original_source"])
        for version in wit.VERSIONS:
            self.assertEqual(
                wit.inventory(self.source / "wasi" / version / "wit"),
                self.original[version],
            )
            self.assertEqual(
                observed["directories"][version]["derived_output"],
                observed["directories"][version]["after"][wit.OUTPUT],
            )
        self.assertEqual(
            wit.mounts(self.work),
            [
                "--bind",
                str(self.work / "derived-wit/p2"),
                "/libc-source/wasi/p2/wit",
                "--bind",
                str(self.work / "derived-wit/p3"),
                "/libc-source/wasi/p3/wit",
            ],
        )

    def test_missing_generated_output_or_success_temporary_is_rejected(self):
        records = self.prepared()
        path = self.work / "derived-wit/p2"
        (path / wit.TEMPORARY).write_text("incomplete output")
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=True)
        observed = wit.observe(self.work, records, successful=False)
        self.assertIsNotNone(observed["directories"]["p2"]["failure_temporary"])
        (path / wit.TEMPORARY).unlink()
        (path / wit.OUTPUT).unlink()
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=True)

    def test_dependency_replacement_and_deletion_are_rejected(self):
        records = self.prepared()
        dependency = self.work / "derived-wit/p3/deps/original.wit"
        dependency.write_text("changed")
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=True)
        dependency.unlink()
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=False)

    def test_unexpected_empty_directory_and_mode_changes_are_rejected(self):
        records = self.prepared()
        parent = self.work / "derived-wit/p2"
        (parent / "unexpected").mkdir()
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=False)
        (parent / "unexpected").rmdir()
        dependency = parent / "deps/original.wit"
        dependency.chmod(dependency.stat().st_mode ^ 0o100)
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=True)

    def test_output_directory_and_external_link_are_rejected(self):
        records = self.prepared()
        output = self.work / "derived-wit/p2" / wit.OUTPUT
        output.unlink()
        output.mkdir()
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=True)
        output.rmdir()
        output.symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            wit.observe(self.work, records, successful=False)

    def test_source_link_and_reused_workspace_are_rejected_before_copy(self):
        target = self.source / "wasi/p2/wit/deps/original.wit"
        target.unlink()
        target.symlink_to(self.source / "wasi/p3/wit/deps/original.wit")
        with self.assertRaises(ValueError):
            wit.prepare(self.source, self.work)
        with self.assertRaises(FileExistsError):
            wit.prepare(self.source, self.work)


if __name__ == "__main__":
    unittest.main()
