"""Public source intent schema checks, without active authority fabricated."""
import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT.parents[2]
# Required owned source bootstrap during standalone discovery.
sys.path.insert(0, str(ROOT))
from author_workflow import (  # noqa: E402 -- standalone author bootstrap
    render_workflow, workflow_template,
)


class WorkflowTemplateTests(unittest.TestCase):
    def test_public_schema_and_exact_function_source(self):
        schema = json.loads((RUNTIME_ROOT / "tobkiri_protocol/schemas/"
                            "profile_workflow_intent_v1.schema.json").read_text())
        template = workflow_template()
        Draft202012Validator(schema).validate(template)
        request = template["steps"][1]["request"]
        self.assertEqual(request["function_id"], "acceptance.temporal.context.reduce")
        self.assertNotIn("function_principal_id", request)
        self.assertEqual(request["input"]["owner_snapshot"],
                         "${steps.owner.output.value.conversation}")
        # Concrete input remains untrusted source data, never lifecycle evidence.
        payload = {"operation": "get", "profile_id": "fixture",
                   "conversation_id": "chat"}
        bound = render_workflow(payload=payload)
        Draft202012Validator(schema).validate(bound)
        self.assertEqual(bound["steps"][1]["request"]["function_id"],
                         "acceptance.temporal.context.reduce")
        payload["profile_id"] = "changed"
        self.assertEqual(bound["steps"][0]["request"]["input"]["profile_id"], "fixture")

    def test_invalid_function_id_and_input_are_rejected(self):
        with self.assertRaises(ValueError):
            render_workflow(function_id="", payload={})
        with self.assertRaises(ValueError):
            render_workflow(payload=[])
        workflow = workflow_template(function_id="renamed.function",
                                     pack_id="renamed.temporal.pack")
        self.assertEqual(workflow["steps"][1]["request"]["function_id"],
                         "renamed.function")


if __name__ == "__main__":
    unittest.main()
