"""Bounded native tests of source application, never guest/entropy proofs."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_patch", HERE / "apply_patch.py")
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


class SourcePatchTests(unittest.TestCase):
    """Require exact source contexts and reject malformed patch namespaces."""

    def test_modify_and_create_with_unchanged_tail(self) -> None:
        diff = ("--- a/src/a.rs\n+++ b/src/a.rs\n@@ -2,2 +2,3 @@\n"
                "-old\n+new\n+added\n context\n"
                "--- /dev/null\n+++ b/src/b.rs\n@@ -0,0 +1 @@\n+created\n")
        result = PATCH.apply_diff(diff, {"src/a.rs": b"head\nold\ncontext\ntail\n"})
        self.assertEqual(result, {"src/a.rs": b"head\nnew\nadded\ncontext\ntail\n",
                                  "src/b.rs": b"created\n"})

    def test_changed_context_denies_without_fuzz(self) -> None:
        diff = "--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n"
        with self.assertRaisesRegex(ValueError, "context differs"):
            PATCH.apply_diff(diff, {"a": b"changed\n"})

    def test_invalid_counts_deny(self) -> None:
        diff = "--- a/a\n+++ b/a\n@@ -1,2 +1 @@\n-old\n+new\n"
        with self.assertRaisesRegex(ValueError, "counts differ"):
            PATCH.apply_diff(diff, {"a": b"old\n"})

    def test_new_file_cannot_replace_original(self) -> None:
        diff = "--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+new\n"
        with self.assertRaisesRegex(ValueError, "already exists"):
            PATCH.apply_diff(diff, {"a": b"old\n"})

    def test_path_escape_spellings_deny(self) -> None:
        for path in ("../a", "/a", "a/../b", "a//b", "./a", "a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                PATCH.safe_path(path)

    def test_duplicate_file_and_old_path_alias_deny(self) -> None:
        diff = "--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+new\n"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            PATCH.apply_diff(diff + diff, {})
        with self.assertRaisesRegex(ValueError, "old source path"):
            PATCH.apply_diff("--- a/b\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n",
                             {"a": b"old\n"})

    def test_out_of_order_hunks_deny(self) -> None:
        diff = ("--- a/a\n+++ b/a\n@@ -2 +2 @@\n-two\n+new\n"
                "@@ -1 +1 @@\n-one\n+new\n")
        with self.assertRaisesRegex(ValueError, "overlapping"):
            PATCH.apply_diff(diff, {"a": b"one\ntwo\n"})

    def test_new_location_must_match(self) -> None:
        diff = "--- a/a\n+++ b/a\n@@ -1 +2 @@\n-old\n+new\n"
        with self.assertRaisesRegex(ValueError, "location differs"):
            PATCH.apply_diff(diff, {"a": b"old\n"})


if __name__ == "__main__":
    unittest.main()
