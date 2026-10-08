"""Public SDK parity for supplied owner shapes; no authentication/Run proof."""
import importlib.util
import unittest
from pathlib import Path

from tobkiri_protocol.conversation_lifecycle import active_task_gap_context
from tobkiri_protocol.saved_internal_context import validate_timing_output

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("owner_timing", ROOT / "source/temporal.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def snapshot(seconds=3600):
    return {"id": "chat", "conversation_revision": 4,
            "messages": [{"id": "user-next", "role": "user", "content": "どう？"}],
            "lifecycle": {"version": "tobkiri.conversation-lifecycle.v1",
                          "state": "running", "active_user_message_id": "user-next",
                          "completion_message_id": "assistant-final",
                          "resumed_completed_at_ms": 1_790_000_000_000,
                          "active_user_received_at_ms": 1_790_000_000_000 + seconds*1000}}


class OwnerTimingTests(unittest.TestCase):
    def test_exact_public_projection_and_null_boundaries_without_mutation(self):
        for seconds in (-1, 0, 3599, 3600, 21600, 86400):
            with self.subTest(seconds=seconds):
                owner = snapshot(seconds)
                output = MODULE.tobkiri_packvm_invoke("timing.project.owner", {
                    "profile_id": "profile", "owner_snapshot": owner})
                self.assertEqual(output["context"], active_task_gap_context(owner))
                self.assertEqual(validate_timing_output(output, profile_id="profile",
                                                       conversation=owner),
                                 active_task_gap_context(owner))
                self.assertEqual(owner["messages"][0]["content"], "どう？")

    def test_typed_sink_rejects_stale_namespace_receipt_and_arbitrary_fields(self):
        owner = snapshot()
        output = MODULE.project_owner_timing({"profile_id": "profile",
                                              "owner_snapshot": owner})
        for field, value in (("profile_id", "other"), ("conversation_revision", 3),
                             ("active_user_message_id", "wrong"), ("approved", True)):
            wrong = dict(output)
            wrong[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_timing_output(wrong, profile_id="profile", conversation=owner)
        # Matching dictionaries pass projection validation, which is not authentication.
        self.assertEqual(output["context"], active_task_gap_context(owner))

    def test_nonrunning_or_wrong_role_cannot_project_a_gap(self):
        for state, role in (("waiting_user", "user"), ("cancelled", "user"),
                            ("running", "assistant"), ("running", "tool")):
            owner = snapshot()
            owner["lifecycle"]["state"] = state
            owner["messages"][0]["role"] = role
            with self.subTest(state=state, role=role):
                output = MODULE.project_owner_timing({"profile_id": "profile",
                                                      "owner_snapshot": owner})
                self.assertIsNone(output["context"])
                self.assertEqual(output["context"], active_task_gap_context(owner))

    def test_event_flags_cannot_be_input_to_owner_projection(self):
        with self.assertRaises(ValueError):
            MODULE.project_owner_timing({"profile_id": "profile",
                                         "owner_snapshot": snapshot(), "approved": True})
        with self.assertRaises(ValueError):
            MODULE.project_owner_timing({"namespace": "profile", "state": {}, "event": {}})


if __name__ == "__main__":
    unittest.main()
