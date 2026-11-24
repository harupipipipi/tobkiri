"""Bounded source-fixture checks; no Cargo, guest, network or tool execution."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

PACKS = Path(__file__).resolve().parents[1]
REPO = PACKS.parent


def digest(value: object) -> str:
    """Match these StrictV1 fixture documents' canonical encoding."""
    data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


class InputEntryFixtureTests(unittest.TestCase):
    def test_all_published_and_local_profiles_have_explicit_declared_entries(self):
        documents = 0
        for path in sorted((PACKS / "profiles").rglob("*.json")):
            profile = json.loads(path.read_text())
            if "startup" not in profile:
                continue
            documents += 1
            pins = {pin["id"]: pin for pin in profile["packs"]}
            for entry in profile["startup"]["inputs"]:
                self.assertEqual(set(entry), {"source_pack", "exact_input_view", "operation"})
                source = entry["source_pack"]
                manifest = json.loads((PACKS / source / "pack.json").read_text())
                self.assertEqual(pins[source]["sha256"], digest(manifest))
                declaration = {key: entry[key] for key in ("exact_input_view", "operation")}
                self.assertIn(declaration, manifest["inputs"])
                name, version = entry["exact_input_view"].rsplit("@", 1)
                self.assertTrue(any(port["shape"] == name and port["version"] == version
                                    for port in manifest["ports"]["provides"]))
        self.assertEqual(documents, 14)

    def test_receipt_binds_current_source_documents_without_rewriting_old_evidence(self):
        receipt = json.loads((REPO / "crates/tobkiri-e2e/evidence/native-input-entry-source-migration-001.json").read_text())
        self.assertFalse(receipt["component_bytes_or_guest_WIT_changed"])
        for relative, change in receipt["profile_changes"].items():
            self.assertEqual(digest(json.loads((REPO / relative).read_text())), change["after"])
            self.assertTrue(change["ceiling_unchanged"])
            self.assertTrue(change["pack_limits_unchanged"])
        for source, change in receipt["manifest_changes"].items():
            self.assertEqual(digest(json.loads((PACKS / source / "pack.json").read_text())), change["after"])
            self.assertTrue(change["file_inventory_unchanged"])

    def test_profile_producer_emits_the_author_selected_record(self):
        spec = importlib.util.spec_from_file_location("pack_builder_source", PACKS / "build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for shape, source, provider in [("harness-chat", "terminal-input", "mock-llm"),
                                        ("echo-chat", "echo-input", "echo-llm")]:
            names = [shape, source, "harness-conversation-store" if shape == "harness-chat" else "echo-store",
                     "harness-prompt" if shape == "harness-chat" else "echo-prompt", provider,
                     "terminal-output" if shape == "harness-chat" else "echo-output"]
            manifests = {name: json.loads((PACKS / ("local." + name) / "pack.json").read_text())
                         for name in names}
            document = module.profile(shape, manifests, provider=provider, origin="http://127.0.0.1:18080")
            self.assertEqual(document["startup"]["inputs"], [{"source_pack": "local." + source,
                             "exact_input_view": "local." + shape + ".user-input@1.0", "operation": "read"}])


if __name__ == "__main__":
    unittest.main()
