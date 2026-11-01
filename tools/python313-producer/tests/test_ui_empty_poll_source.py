"""Exact upstream source derivation only, without compiling or running libc."""

import hashlib
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ui_empty_poll_source


class EmptyPollSourceTests(unittest.TestCase):
    def setUp(self):
        self.original = (
            Path(__file__).parent / "fixtures/wasi-libc-161b3195-poll.c"
        ).read_bytes()

    def test_only_exact_empty_poll_path_is_inserted_after_original_checks(self):
        candidate, receipt = ui_empty_poll_source.derive(self.original)
        offset = receipt["insertion_offset"]
        self.assertEqual(candidate[:offset], self.original[:offset])
        self.assertEqual(
            candidate[offset + len(ui_empty_poll_source.ADDITION) :],
            self.original[offset:],
        )
        self.assertEqual(candidate.count(ui_empty_poll_source.ADDITION), 1)
        self.assertIn(b"if (nfds == 0 && timeout == 0)", candidate)
        self.assertLess(self.original.index(b"#elif defined(__wasip2__)"), offset)
        self.assertLess(
            self.original.index(b"if (state.event_count > 0 && timeout != 0)"), offset
        )
        self.assertTrue(
            self.original[offset:].startswith(
                b"  poll_own_pollable_t timeout_pollable;"
            )
        )
        self.assertEqual(
            receipt["candidate"]["sha256"], hashlib.sha256(candidate).hexdigest()
        )

    def test_changed_original_and_already_patched_sources_are_rejected(self):
        candidate, _ = ui_empty_poll_source.derive(self.original)
        for changed in (self.original + b"\n", candidate, bytearray(self.original)):
            with self.subTest(type=type(changed).__name__):
                with self.assertRaises(ValueError):
                    ui_empty_poll_source.derive(changed)


if __name__ == "__main__":
    unittest.main()
