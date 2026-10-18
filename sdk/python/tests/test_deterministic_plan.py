"""Build declaration checks; these do not prove patched preinitialization."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deterministic_plan import entry_metadata, initializer_inputs


class DeterministicPlanTests(unittest.TestCase):
    def test_seed_is_only_public_versioned_declaration_not_ambient_state(self):
        a = initializer_inputs({"sources": {"b": "2", "a": "1"}}, 0)
        b = initializer_inputs({"sources": {"a": "1", "b": "2"}}, 0)
        self.assertEqual(a, b)
        self.assertFalse(a["runtime_crypto_enabled"])
        self.assertEqual(a["guest_environment"]["PYTHONHASHSEED"], "0")
        self.assertNotEqual(a["public_build_seed_hex"],
                            initializer_inputs({"sources": {"a": "3", "b": "2"}}, 0)
                            ["public_build_seed_hex"])

    def test_float_noncanonical_unicode_and_undeclared_epoch_fail(self):
        for plan, epoch in [({"x": 1.0}, 0), ({"x": "\ud800"}, 0), ({}, -1),
                            ({}, True), ({"x": 2**64}, 0), ({"x": 2**53}, 0)]:
            with self.assertRaises(ValueError):
                initializer_inputs(plan, epoch)

    def test_metadata_uses_only_normalized_declared_path_and_explicit_time(self):
        self.assertEqual(entry_metadata("pkg/app.py", 12, 0),
                         entry_metadata("pkg/app.py", 12, 0))
        self.assertNotEqual(entry_metadata("pkg/app.py", 12, 0)["metadata_hash_hex"],
                            entry_metadata("pkg/other.py", 12, 0)["metadata_hash_hex"])
        for path in ["/private/file", "../file", "pkg//file", "pkg/./file", "", "."]:
            with self.assertRaises(ValueError):
                entry_metadata(path, 1, 0)


if __name__ == "__main__":
    unittest.main()
