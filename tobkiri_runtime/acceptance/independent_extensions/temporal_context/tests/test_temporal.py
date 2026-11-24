"""Deterministic reducer tests; these do not demonstrate Broker/VM acceptance."""
import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "independent_temporal", ROOT / "source/temporal.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def apply(state, kind, at, sequence=1, namespace="profile:a/conversation:one"):
    return MODULE.transition({"namespace": namespace, "state": state,
                              "event": {"id": f"event-{sequence}",
                                        "sequence": sequence, "kind": kind,
                                        "at": at}})


class TemporalTests(unittest.TestCase):
    def baseline(self):
        return apply({}, "assistant.completed", "2026-08-27T10:01:00+09:00")["state"]

    def test_threshold_hours_day(self):
        for seconds in (0, 3599, 3600, 21600, 86400, 604800):
            with self.subTest(seconds=seconds):
                at = (datetime.fromisoformat("2026-08-27T10:01:00+09:00")
                      + timedelta(seconds=seconds)).isoformat()
                result = apply(self.baseline(), "user.received", at, 2)
                context = result["internal_temporal_context"]
                if seconds < 3600:
                    self.assertIsNone(context)
                else:
                    self.assertEqual(context["elapsed_seconds"], seconds)
                    self.assertEqual(context["current_user_message_at"], at)
                    self.assertEqual(context["previous_task_completed_at"],
                                     "2026-08-27T10:01:00+09:00")

    def test_offset_and_dst_transitions(self):
        for earlier, later in (
            ("2026-03-08T01:30:00-05:00", "2026-03-08T03:30:00-04:00"),
            ("2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00"),
            ("2026-08-27T10:00:00+09:00", "2026-08-27T02:00:00Z"),
        ):
            with self.subTest(earlier=earlier):
                state = apply({}, "assistant.completed", earlier)["state"]
                result = apply(state, "user.received", later, 2)
                self.assertEqual(result["internal_temporal_context"]
                                 ["elapsed_seconds"], 3600)

    def test_new_completion_resets_gap(self):
        state = apply(self.baseline(), "user.received",
                      "2026-08-27T16:01:00+09:00", 2)["state"]
        state = apply(state, "assistant.completed",
                      "2026-08-27T16:02:00+09:00", 3)["state"]
        self.assertIsNone(apply(state, "user.received",
                                "2026-08-27T16:03:00+09:00", 4)
                          ["internal_temporal_context"])

    def test_tools_errors_cancellation_do_not_complete(self):
        for kind in ("tool.completed", "assistant.error", "assistant.cancelled"):
            initial = apply({}, kind, "2026-08-27T10:01:00+09:00")["state"]
            self.assertIsNone(initial["last_task_completed_at"])
            state = apply(self.baseline(), kind,
                          "2026-08-27T16:01:00+09:00", 2)["state"]
            result = apply(state, "user.received",
                           "2026-08-27T16:02:00+09:00", 3)
            self.assertEqual(result["internal_temporal_context"]
                             ["elapsed_seconds"], 21660)

    def test_invalid_stale_duplicate_backwards_and_isolation(self):
        cases = [
            ("user.received", "2026-08-27T16:00:00+09:00", 1,
             "profile:a/conversation:one"),
            ("user.received", "2026-08-27T10:00:00+09:00", 2,
             "profile:a/conversation:one"),
            ("user.received", "2026-08-27T16:00:00", 2,
             "profile:a/conversation:one"),
            ("user.received", "bad", 2, "profile:a/conversation:one"),
            ("unknown", "2026-08-27T16:00:00Z", 2,
             "profile:a/conversation:one"),
            ("user.received", "2026-08-27T16:00:00Z", 2,
             "profile:a/conversation:two"),
            ("user.received", "2026-08-27T16:00:00Z", True,
             "profile:a/conversation:one"),
        ]
        for kind, at, sequence, namespace in cases:
            with self.subTest(case=(kind, at, sequence, namespace)):
                with self.assertRaises(ValueError):
                    apply(self.baseline(), kind, at, sequence, namespace)
        payload = {"namespace": "profile:a/conversation:one",
                   "state": self.baseline(),
                   "event": {"id": "event-1", "sequence": 2,
                             "kind": "user.received",
                             "at": "2026-08-27T16:01:00+09:00"}}
        with self.assertRaises(ValueError):
            MODULE.transition(payload)
        payload["event"]["id"] = "event-2"
        payload["user_message"] = "どう？"
        with self.assertRaises(ValueError):
            MODULE.transition(payload)

    def test_missing_baseline_and_corrupt_snapshot(self):
        result = apply({}, "user.received", "2026-08-27T16:01:00+09:00")
        self.assertIsNone(result["internal_temporal_context"])
        for field, value in (("last_task_completed_at", "2026-08-28T10:00:00Z"),
                             ("last_task_completed_at", "not-a-timestamp"),
                             ("sequence", True), ("event_id", "")):
            state = self.baseline()
            state[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    apply(state, "user.received", "2026-08-28T12:00:00Z", 2)

    def test_snapshot_round_trip_and_no_input_mutation(self):
        import json
        state = json.loads(json.dumps(self.baseline()))
        original = json.dumps(state)
        result = apply(state, "user.received", "2026-08-28T10:01:00+09:00", 2)
        self.assertEqual(json.dumps(state), original)
        self.assertEqual(set(result), {"state", "internal_temporal_context"})
        self.assertEqual(result["internal_temporal_context"]["elapsed_seconds"], 86400)


if __name__ == "__main__":
    unittest.main()
