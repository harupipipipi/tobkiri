"""Synthetic build-pair invariants, not actual producer or guest observations."""

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poll_producer_pair as pair


def fixture():
    original = {
        "assets": {
            "inputs": {"source": "same"},
            "runtime_receipt": "same",
            "outputs": {"libc.so.zst": "original", "other": "same"},
        },
        "source": {
            "files": {"libc.so.zst": "original", "Cargo.lock": "same"},
            "original_files": {"Cargo.lock": "same"},
            "trace": "same",
        },
        "producer": {
            "inputs": "same",
            "candidate_recipe": "same",
            "ci_link_trace_candidate": "same",
        },
    }
    candidate = copy.deepcopy(original)
    candidate["assets"]["outputs"]["libc.so.zst"] = "candidate"
    candidate["source"]["files"]["libc.so.zst"] = "candidate"
    return original, candidate


class ProducerPairTests(unittest.TestCase):
    def test_only_one_asset_difference_is_accepted(self):
        original, candidate = fixture()
        pair.require_pair(original, candidate)
        candidate["assets"]["outputs"]["other"] = "changed"
        with self.assertRaises(ValueError):
            pair.require_pair(original, candidate)

    def test_no_difference_cannot_pretend_to_compare_the_candidate(self):
        original, _ = fixture()
        with self.assertRaises(ValueError):
            pair.require_pair(original, original)

    def test_lock_and_observer_changes_cannot_hide_behind_the_asset_delta(self):
        for change in ("lock", "observer", "source-baseline"):
            with self.subTest(change=change):
                original, candidate = fixture()
                if change == "lock":
                    candidate["source"]["files"]["Cargo.lock"] = "changed"
                elif change == "observer":
                    candidate["source"]["trace"] = "changed"
                else:
                    candidate["source"]["original_files"]["Cargo.lock"] = "changed"
                with self.assertRaises(ValueError):
                    pair.require_pair(original, candidate)

    def test_different_runtime_or_compiler_recipe_is_rejected(self):
        for owner, field in (
            ("assets", "runtime_receipt"),
            ("producer", "candidate_recipe"),
        ):
            with self.subTest(field=field):
                original, candidate = fixture()
                candidate[owner][field] = "changed"
                with self.assertRaises(ValueError):
                    pair.require_pair(original, candidate)

    def test_local_pair_invocation_cannot_read_or_write_any_build_tree(self):
        with (
            patch.object(pair.sys, "platform", "darwin"),
            patch.object(pair, "one") as read,
        ):
            with self.assertRaisesRegex(ValueError, "Linux CI"):
                pair.run(*[Path("/must-not-open")] * 6)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
