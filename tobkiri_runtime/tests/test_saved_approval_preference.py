"""Candidate verification; no Host operations or live approval are invoked."""

from pathlib import Path
import unittest

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol import saved_conversation as protocol
from ecosystem.tobkiri_conversation_orchestration_pack.runtime import saved_conversation as guest

ROOT = Path(__file__).resolve().parents[1]


class CapturedPreferenceTests(unittest.TestCase):
    def request(self, **extra):
        return dict(
            turn_id="turn:1",
            conversation_id="conversation:1",
            conversation_revision=1,
            content="hello",
            **extra,
        )

    def test_absent_mode_preserves_legacy_identity_and_ask_semantics(self):
        original = {"request": self.request()}
        validated = protocol.validate_saved_conversation_input(original)
        self.assertEqual(validated, original)
        self.assertEqual(validated["request"].get("action_approval_mode", "ask"), "ask")
        self.assertEqual(canonical_digest(validated), canonical_digest(original))

    def test_every_mode_is_captured_without_mutating_request(self):
        for mode in ("ask", "agent", "full"):
            original = {"request": self.request(action_approval_mode=mode)}
            validated = protocol.validate_saved_conversation_input(original)
            original["request"]["action_approval_mode"] = "ask" if mode != "ask" else "full"
            intent = guest.start(validated["request"])
            self.assertEqual(intent["state"]["request"]["action_approval_mode"], mode)
            self.assertEqual(
                guest._message(intent["state"], "user")["metadata"]["action_approval_mode"], mode
            )
            guest._state_request(intent["state"]["request"])

    def test_mode_change_changes_initial_identity(self):
        digests = {
            canonical_digest({"request": self.request(action_approval_mode=mode)})
            for mode in ("ask", "agent", "full")
        }
        self.assertEqual(len(digests), 3)

    def test_invalid_modes_and_client_approved_flags_fail_closed(self):
        for value in (None, True, 1, [], {}, "side_model", "FULL", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                protocol.validate_saved_conversation_input(
                    {"request": self.request(action_approval_mode=value)}
                )
            with self.subTest(value=value), self.assertRaises(ValueError):
                guest.start(self.request(action_approval_mode=value))
        with self.assertRaises(ValueError):
            protocol.validate_saved_conversation_input({"request": self.request(approved=True)})
        with self.assertRaises(ValueError):
            guest._state_request({**self.request(action_approval_mode="full"), "approved": True})


class CanonicalSchemaTests(unittest.TestCase):
    def test_real_canonical_validator_and_every_source_schema(self):
        import json
        from unittest.mock import patch
        from jsonschema import Draft202012Validator
        from tobkiri_protocol import validation

        source = json.loads((ROOT / "schemas/pack_v4_catalog.v1.json").read_text())
        schemas = []
        for pack in source["packs"]:
            for contract in pack.get("provided_contracts", []):
                schema = contract.get("schemas", {}).get("input", {})
                request_schema = schema.get("properties", {}).get("request", {})
                if "action_approval_mode" in request_schema.get("properties", {}):
                    schemas.append(schema)
        self.assertEqual(len(schemas), 2)
        schema = json.loads(
            (ROOT / "tobkiri_protocol/schemas/saved_conversation_input_v1.schema.json").read_text()
        )
        with patch.object(validation, "load_schema", return_value=schema):
            for mode in (None, "ask", "agent", "full"):
                request = CapturedPreferenceTests().request(
                    **({} if mode is None else {"action_approval_mode": mode})
                )
                value = {"request": request}
                self.assertEqual(
                    validation.validate_document(value, "saved_conversation_input"), value
                )
                for input_schema in schemas:
                    Draft202012Validator(input_schema).validate(value)
            for extra in (
                {"approved": True},
                {"action_approval_mode": "side_model"},
                {"action_approval_mode": None},
            ):
                value = {"request": CapturedPreferenceTests().request(**extra)}
                with self.assertRaises(Exception):
                    validation.validate_document(value, "saved_conversation_input")
                for input_schema in schemas:
                    self.assertTrue(list(Draft202012Validator(input_schema).iter_errors(value)))

    def test_compact_capture_and_owner_acknowledgement_binding(self):
        initial = guest.start(CapturedPreferenceTests().request(action_approval_mode="agent"))
        user = guest.resume(
            initial["state"],
            {
                "status": "ok",
                "value": {
                    "conversation": {
                        "id": "conversation:1",
                        "conversation_revision": 1,
                        "model_reference": "model:1",
                        "messages": [],
                        "current_node_id": None,
                    }
                },
            },
        )
        self.assertEqual(user["state"]["request"]["action_approval_mode"], "agent")
        message = user["payload"]["message"]
        self.assertEqual(message["metadata"]["action_approval_mode"], "agent")
        accepted = guest.resume(
            user["state"],
            {
                "status": "ok",
                "value": {
                    "action": "message_appended",
                    "message": message,
                    "conversation_revision": 2,
                },
            },
        )
        self.assertEqual(accepted["state"]["stage"], "ai")
        changed = {**message, "metadata": {**message["metadata"], "action_approval_mode": "full"}}
        rejected = guest.resume(
            user["state"],
            {
                "status": "ok",
                "value": {
                    "action": "message_appended",
                    "message": changed,
                    "conversation_revision": 2,
                },
            },
        )
        self.assertEqual(rejected["error"]["code"], "OWNER_RESPONSE_INVALID")


class OwnerReceiptTests(unittest.TestCase):
    def test_owner_receipt_binds_exact_mode_and_rejects_rebound_metadata(self):
        from unittest.mock import patch
        from ecosystem.rumi_conversation_store_pack.runtime import saved_receipt as receipt

        initial = {"request": CapturedPreferenceTests().request(action_approval_mode="full")}
        state = guest.start(initial["request"])["state"]
        message = guest._message(state, "user")
        with patch.object(
            receipt, "validate_saved_conversation_input", protocol.validate_saved_conversation_input
        ):
            evidence = receipt.append_receipt(None, initial, "conversation:1", 1, message)
            self.assertEqual(evidence["input_digest"], canonical_digest(initial))
            changed = {
                **message,
                "metadata": {**message["metadata"], "action_approval_mode": "ask"},
            }
            with self.assertRaises(ValueError):
                receipt.append_receipt(None, initial, "conversation:1", 1, changed)


if __name__ == "__main__":
    unittest.main()
