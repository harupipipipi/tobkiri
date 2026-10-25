"""Synthetic C-result records; these do not prove libc or guest behavior."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poll_reference_results as results


EXPECTED_CASES = (
    "errno-zero",
    "errno-e2big",
    "errno-eintr",
    "errno-ebadf",
    "errno-enotsup",
    "sentinel-zero-fds",
    "finite-positive-timeout",
    "negative-fd",
    "invalid-fd",
    "mixed-invalid-fds",
    "priority-only-validation",
)
CASE_KEYS = {
    "case",
    "nfds",
    "timeout_ms",
    "incoming_errno",
    "return_value",
    "outgoing_errno",
    "entries",
    "zero_nfds_buffer_unchanged",
}


def _valid_record():
    """Mirror the committed C case inputs with synthetic observation values."""
    incoming = (0, 7, 4, 9, 95, 7, 4, 7, 4, 9, 7)
    entry_inputs = {
        7: [(-1, 1)],
        8: [(2**31 - 1, 1)],
        9: [(-1, 1), (2**31 - 1, 2)],
        10: [(-1, 0x0200)],
    }
    cases = []
    timing = []
    for index, name in enumerate(EXPECTED_CASES):
        entries = [
            {"fd": fd, "events": events, "revents": index + entry_index}
            for entry_index, (fd, events) in enumerate(entry_inputs.get(index, ()))
        ]
        nfds = 2 if index == 9 else 1 if index >= 7 else 0
        cases.append(
            {
                "case": name,
                "nfds": nfds,
                "timeout_ms": 1 if index == 6 else 0,
                "incoming_errno": incoming[index],
                "return_value": 0,
                "outgoing_errno": incoming[index],
                "entries": entries,
                "zero_nfds_buffer_unchanged": True if index == 5 else None,
            }
        )
        before = 1000 + index * 10
        after = before + 5
        timing.append(
            {
                "case": name,
                "before_ns": before,
                "after_ns": after,
                "elapsed_ns": after - before,
            }
        )
    return {
        "target": {
            "python": [3, 13, 16],
            "pointer_bits": 32,
            "extension_file": "/synthetic/_tobkiri_poll_oracle.abi3.so",
        },
        "behavior": {
            "abi": {
                "pointer_bytes": 4,
                "nfds_t_bytes": 4,
                "pollfd_bytes": 8,
                "fd_offset": 0,
                "events_offset": 4,
                "revents_offset": 6,
                "int_bytes": 4,
                "short_bytes": 2,
                "sig_atomic_t_bytes": 4,
            },
            "cases": cases,
            "synchronous_signal": {
                "kind": "synchronous-userspace-raise",
                "previous_was_default": 1,
                "raise_return": 0,
                "handler_calls": 1,
                "incoming_errno": 7,
                "poll_return": 0,
                "poll_errno": 7,
                "restore_succeeded": 1,
            },
        },
        "timing": timing,
        "scope": results.SCOPE,
        "negative_timeout": "not executed; excluded by exact source condition",
        "asynchronous_cancellation": "unsupported target; not a runtime pass",
        "production_enabled": False,
    }


def _decode(record):
    return results.decode(json.dumps(record, separators=(",", ":")))


class PollReferenceResultTests(unittest.TestCase):
    def test_decodes_all_eleven_exact_case_shapes_inputs_signal_and_metrics(self):
        record = _valid_record()
        decoded = _decode(record)

        self.assertEqual(
            tuple(row["case"] for row in decoded["behavior"]["cases"]), EXPECTED_CASES
        )
        self.assertEqual(len(decoded["timing"]), 11)
        self.assertEqual(decoded["behavior"]["abi"]["revents_offset"], 6)
        self.assertEqual(
            [set(row) for row in decoded["behavior"]["cases"]],
            [CASE_KEYS] * 11,
        )
        self.assertEqual(
            [
                [(entry["fd"], entry["events"]) for entry in row["entries"]]
                for row in decoded["behavior"]["cases"]
            ],
            [
                [],
                [],
                [],
                [],
                [],
                [],
                [],
                [(-1, 1)],
                [(2**31 - 1, 1)],
                [(-1, 1), (2**31 - 1, 2)],
                [(-1, 0x0200)],
            ],
        )
        self.assertEqual(decoded["behavior"]["synchronous_signal"]["handler_calls"], 1)
        self.assertEqual(decoded["scope"], results.SCOPE)

    def test_rejects_missing_case_or_zero_fd_buffer_observation(self):
        missing_case = _valid_record()
        missing_case["behavior"]["cases"].pop()
        with self.assertRaises(ValueError):
            _decode(missing_case)

        missing_buffer_observation = _valid_record()
        missing_buffer_observation["behavior"]["cases"][5][
            "zero_nfds_buffer_unchanged"
        ] = False
        with self.assertRaises(ValueError):
            _decode(missing_buffer_observation)

        missing_entry = _valid_record()
        missing_entry["behavior"]["cases"][8]["entries"] = []
        with self.assertRaises(ValueError):
            _decode(missing_entry)

    def test_rejects_per_case_inputs_that_differ_from_the_committed_c_fixture(self):
        wrong_negative_fd = _valid_record()
        wrong_negative_fd["behavior"]["cases"][7]["entries"][0]["fd"] = 7
        with self.assertRaises(ValueError):
            _decode(wrong_negative_fd)

        wrong_priority_mask = _valid_record()
        wrong_priority_mask["behavior"]["cases"][10]["entries"][0]["events"] = 0
        with self.assertRaises(ValueError):
            _decode(wrong_priority_mask)

    def test_booleans_cannot_stand_in_for_observed_integers(self):
        mutations = (
            lambda r: r["target"].__setitem__("pointer_bits", True),
            lambda r: r["behavior"]["abi"].__setitem__("pointer_bytes", True),
            lambda r: r["behavior"]["cases"][0].__setitem__("incoming_errno", True),
            lambda r: r["behavior"]["cases"][8]["entries"][0].__setitem__("fd", True),
            lambda r: r["behavior"]["synchronous_signal"].__setitem__(
                "restore_succeeded", True
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                record = _valid_record()
                mutation(record)
                with self.assertRaises(ValueError):
                    _decode(record)

    def test_compare_ignores_valid_clock_changes_but_requires_every_behavior_value(
        self,
    ):
        original = _valid_record()
        candidate = copy.deepcopy(original)
        for index, row in enumerate(candidate["timing"]):
            row["before_ns"] = 9000 + index * 3
            row["after_ns"] = row["before_ns"] + index
            row["elapsed_ns"] = index
        compared = results.compare(original, candidate)
        self.assertTrue(compared["raw_c_observations_match"])
        self.assertFalse(compared["raw_timing_compared_for_equality"])
        self.assertFalse(compared["c_abi_verified"])

        mutations = (
            lambda r: r["behavior"]["abi"].__setitem__("revents_offset", 7),
            lambda r: r["behavior"]["cases"][8].__setitem__("outgoing_errno", 123),
            lambda r: r["behavior"]["cases"][9]["entries"][1].__setitem__("revents", 6),
            lambda r: r["behavior"]["synchronous_signal"].__setitem__(
                "poll_errno", 123
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                altered = copy.deepcopy(candidate)
                mutation(altered)
                with self.assertRaises(ValueError):
                    results.compare(original, altered)

    def test_rejects_missing_or_malformed_raw_timing_metrics(self):
        mutations = []
        missing_row = _valid_record()
        missing_row["timing"].pop()
        mutations.append(missing_row)
        bad_elapsed = _valid_record()
        bad_elapsed["timing"][0]["elapsed_ns"] += 1
        mutations.append(bad_elapsed)
        backward = _valid_record()
        backward["timing"][0]["after_ns"] = backward["timing"][0]["before_ns"] - 1
        mutations.append(backward)
        boolean_metric = _valid_record()
        boolean_metric["timing"][0]["elapsed_ns"] = True
        mutations.append(boolean_metric)
        for index, record in enumerate(mutations):
            with self.subTest(mutation=index):
                with self.assertRaises(ValueError):
                    _decode(record)

        duplicate_metric_key = json.dumps(_valid_record(), separators=(",", ":"))
        duplicate_metric_key = duplicate_metric_key.replace(
            '"elapsed_ns":5', '"elapsed_ns":5,"elapsed_ns":5', 1
        )
        with self.assertRaises(ValueError):
            results.decode(duplicate_metric_key)


if __name__ == "__main__":
    unittest.main()
