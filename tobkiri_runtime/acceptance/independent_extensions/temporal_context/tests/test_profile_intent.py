"""Public source-intent selection validation, not release/runtime acceptance."""
import sys
import unittest
from pathlib import Path
import json

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT.parents[2]
# Required for standalone discovery of this author's source-intent helper.
sys.path.insert(0, str(ROOT))
from author_profile import render_intent  # noqa: E402 -- author bootstrap


class ProfileIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schemas = RUNTIME_ROOT / "tobkiri_protocol/schemas"
        registry = Registry()
        for name in ("common_v1.schema.json", "profile_intent_v1.schema.json",
                     "profile_content_projection_v1.schema.json"):
            document = json.loads((schemas / name).read_text())
            registry = registry.with_resource(document["$id"],
                                              Resource.from_contents(document))
        cls.validator = Draft202012Validator(
            json.loads((schemas / "profile_intent_v1.schema.json").read_text()),
            registry=registry,
        )

    def test_explicit_selected_unselected_pack_and_projection_source(self):
        # Example identities validate source syntax only; no catalog is fabricated.
        baseline = [{"pack_id": "example.application", "artifact_digest": None,
                     "role": "application"}]
        for pack_selected in (False, True):
            for projection_selected in (False, True):
                with self.subTest(pack=pack_selected, projection=projection_selected):
                    intent = render_intent(
                        base_pack_id="example.base", baseline_packs=baseline,
                        caller_function_id="example.application.caller",
                        temporal_selected=pack_selected,
                        projection_selected=projection_selected,
                    )
                    self.validator.validate(intent)
                    members = {pack["pack_id"] for pack in intent["packs"]}
                    self.assertEqual("acceptance.temporal.context" in members,
                                     pack_selected)
                    self.assertEqual(bool(intent["requested_edges"]), pack_selected)
                    self.assertEqual(bool(intent["content_projections"]),
                                     projection_selected)
                    self.assertEqual(intent["authority_references"], [])
                    self.assertIsNone(intent["profile_authority_snapshot_digest"])
                    self.assertEqual(intent["state"], "needs_resolution")
        self.assertEqual(len(baseline), 1)

    def test_selected_source_edge_is_exact_operation_without_authority(self):
        intent = render_intent(base_pack_id="example.base", baseline_packs=[],
                               caller_function_id="example.caller")
        self.validator.validate(intent)
        edge = intent["requested_edges"][0]
        self.assertEqual(edge["contract_id"], "acceptance.temporal.context.v1")
        self.assertEqual(edge["operation_id"], "temporal.reduce")
        self.assertEqual(edge["requested_scope_template"], {})
        projection = intent["content_projections"][0]
        self.assertEqual(projection["artifact_root"], "profile_projections/temporal")
        self.assertIsNone(projection["content_digest"])


if __name__ == "__main__":
    unittest.main()
