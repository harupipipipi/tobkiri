"""Synthetic shared-import guards only; no SDK, compiler or Wasmtime execution."""

import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from empty_poll_probe.inspect_helper import checks


def fixture():
    signatures = {
        "poll": [{"params": ["i32", "i32", "i32"], "results": ["i32"]}],
        "__errno_location": [{"params": [], "results": ["i32"]}],
    }
    helper = {
        "function_imports": ["poll", "__errno_location"],
        "function_exports": ["PyInit__tobkiri_poll_oracle"],
        "custom_sections": ["dylink.0"],
        "imported_memories": 1,
        "imported_memory_types": [{"is_shared": False, "is_64": False}],
        "imported_tables": 1,
        "required_function_signatures": {"imports": copy.deepcopy(signatures)},
    }
    original = {
        "function_exports": ["poll", "__errno_location"],
        "required_function_signatures": {"exports": copy.deepcopy(signatures)},
    }
    return helper, original


class SharedImportGuardTests(unittest.TestCase):
    def test_complete_matching_dynamic_metadata_is_required(self):
        helper, original = fixture()
        self.assertTrue(all(checks(helper, original).values()))
        helper["function_imports"].remove("poll")
        self.assertFalse(checks(helper, original)["required_function_imports"])

    def test_embedded_export_or_missing_sdk_provider_is_rejected(self):
        helper, original = fixture()
        helper["function_exports"].append("poll")
        self.assertFalse(checks(helper, original)["no_embedded_poll_or_errno_exports"])
        original["function_exports"].remove("__errno_location")
        self.assertFalse(checks(helper, original)["sdk_libc_function_exports"])

    def test_changed_or_ambiguous_function_signatures_are_rejected(self):
        helper, original = fixture()
        original["required_function_signatures"]["exports"]["poll"][0]["params"][0] = (
            "i64"
        )
        self.assertFalse(checks(helper, original)["function_signatures_match"])
        helper, original = fixture()
        helper["required_function_signatures"]["imports"]["poll"] *= 2
        self.assertFalse(checks(helper, original)["function_signatures_match"])

    def test_initializer_dylink_memory_and_table_guards_remain_independent(self):
        helper, original = fixture()
        for field, guard, replacement in (
            ("function_exports", "python_initializer", []),
            ("custom_sections", "dynamic_link_metadata", []),
            ("imported_memories", "imported_memory", 0),
            ("imported_tables", "imported_table", 0),
        ):
            with self.subTest(field=field):
                changed = {**helper, field: replacement}
                result = checks(changed, original)
                self.assertFalse(result[guard])
                self.assertEqual(sum(not value for value in result.values()), 1)

    def test_shared_or_memory64_attributes_are_recorded_but_rejected(self):
        for changed in ("is_shared", "is_64"):
            with self.subTest(attribute=changed):
                helper, original = fixture()
                helper["imported_memory_types"][0][changed] = True
                result = checks(helper, original)
                self.assertFalse(result["non_shared_wasm32_memory"])
                self.assertTrue(result["imported_memory"])


if __name__ == "__main__":
    unittest.main()
