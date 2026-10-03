from __future__ import annotations

import json
import unittest
from pathlib import Path

from _defaultspack_test_isolation import is_pack_test_child, run_pack_test


PACK_ROOT = Path(__file__).resolve().parents[1]
HUMAN_OPERATOR_MANIFEST = (
    PACK_ROOT / "tools" / "human_operator_canvas_open" / "manifest.json"
)
HUMAN_OPERATOR_ARRAY_ITEM_TYPES = {
    "messages": "object",
    "tool_names": "string",
}


def _array_paths_missing_items(node, path="$"):
    missing = []
    if isinstance(node, dict):
        if node.get("type") == "array" and "items" not in node:
            missing.append(path)
        for key, value in node.items():
            missing.extend(_array_paths_missing_items(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            missing.extend(_array_paths_missing_items(value, f"{path}[{index}]"))
    return missing


def _human_operator_array_contract_failures(manifest):
    properties = (
        manifest.get("config", {})
        .get("schema", {})
        .get("parameters", {})
        .get("properties", {})
    )
    failures = []
    for field, item_type in HUMAN_OPERATOR_ARRAY_ITEM_TYPES.items():
        schema = properties.get(field)
        if not isinstance(schema, dict) or schema.get("type") != "array":
            failures.append(f"{field} must be an array")
            continue
        if schema.get("items") != {"type": item_type}:
            failures.append(f"{field}.items must be {{'type': '{item_type}'}}")
    return failures


def _human_operator_payload_contract_failures(parameters, payload):
    properties = parameters.get("properties", {})
    failures = []
    for field, expected_item_type in HUMAN_OPERATOR_ARRAY_ITEM_TYPES.items():
        schema = properties.get(field)
        item_schema = schema.get("items") if isinstance(schema, dict) else None
        item_type = item_schema.get("type") if isinstance(item_schema, dict) else None
        if item_type != expected_item_type:
            failures.append(f"{field}.items has an invalid type")
            continue
        if field not in payload:
            continue
        values = payload[field]
        if not isinstance(values, list):
            failures.append(f"{field} must be an array")
            continue
        for index, value in enumerate(values):
            valid = isinstance(value, dict) if item_type == "object" else isinstance(value, str)
            if not valid:
                failures.append(f"{field}[{index}] must be a {item_type}")
    return failures


class GoogleSchemaManifestTests(unittest.TestCase):
    def test_native_schema_normalizes_nullable_and_array_items(self):
        if not is_pack_test_child():
            run_pack_test(
                Path(__file__),
                "GoogleSchemaManifestTests::test_native_schema_normalizes_nullable_and_array_items",
            )
            return

        from domain.ai_client.providers.google_provider import GoogleProvider

        schema = {
            "type": "object",
            "properties": {
                "conversation": {"type": ["object", "null"]},
                "rows": {"type": "array"},
            },
        }

        native = GoogleProvider._native_schema(schema)

        self.assertEqual(native["properties"]["conversation"]["type"], "object")
        self.assertTrue(native["properties"]["conversation"]["nullable"])
        self.assertEqual(native["properties"]["rows"]["type"], "array")
        self.assertEqual(
            native["properties"]["rows"]["items"],
            {"type": "object", "properties": {}, "required": []},
        )

    def test_tool_and_ui_manifests_define_items_for_arrays(self):
        if not is_pack_test_child():
            run_pack_test(
                Path(__file__),
                "GoogleSchemaManifestTests::test_tool_and_ui_manifests_define_items_for_arrays",
            )
            return

        manifest_paths = sorted((PACK_ROOT / "tools").glob("*/manifest.json"))
        manifest_paths.extend(sorted((PACK_ROOT / "extensions" / "ui").glob("*/manifest.json")))

        failures = []
        for manifest_path in manifest_paths:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            missing = _array_paths_missing_items(payload)
            if missing:
                failures.append(f"{manifest_path}: {', '.join(missing)}")

        self.assertEqual(failures, [])

    def test_human_operator_array_items_match_payload_contract(self):
        if not is_pack_test_child():
            run_pack_test(
                Path(__file__),
                "GoogleSchemaManifestTests::test_human_operator_array_items_match_payload_contract",
            )
            return

        manifest = json.loads(HUMAN_OPERATOR_MANIFEST.read_text(encoding="utf-8"))
        parameters = manifest["config"]["schema"]["parameters"]

        self.assertEqual(_human_operator_array_contract_failures(manifest), [])

        valid_payload = {
            "session_id": "session-1",
            "messages": [
                {"role": "system", "content": "Review this prompt."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "tool", "arguments": "{}"},
                        }
                    ],
                },
            ],
            "tool_names": ["human_operator_canvas_open", "tool"],
        }
        self.assertEqual(_human_operator_payload_contract_failures(parameters, valid_payload), [])

        invalid_payloads = (
            {"session_id": "session-1", "messages": ["not-a-message"]},
            {"session_id": "session-1", "tool_names": [{"name": "tool"}]},
        )
        for payload in invalid_payloads:
            self.assertTrue(_human_operator_payload_contract_failures(parameters, payload), payload)

        missing_items = json.loads(json.dumps(manifest))
        del missing_items["config"]["schema"]["parameters"]["properties"]["messages"][
            "items"
        ]
        self.assertTrue(_human_operator_array_contract_failures(missing_items))

        unknown_shape = json.loads(json.dumps(manifest))
        unknown_shape["config"]["schema"]["parameters"]["properties"]["tool_names"][
            "items"
        ] = {"type": "integer"}
        self.assertTrue(_human_operator_array_contract_failures(unknown_shape))


if __name__ == "__main__":
    unittest.main()
