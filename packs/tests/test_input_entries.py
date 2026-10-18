"""Bounded source-fixture checks; no Cargo, guest, network or tool execution."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

PACKS = Path(__file__).resolve().parents[1]
REPO = PACKS.parent
MIGRATION_RECEIPT = (
    REPO / "crates/tobkiri-e2e/evidence/native-input-entry-source-migration-001.json"
)
MIGRATION_RECEIPT_SHA256 = (
    "4f1ce0f19a110381b7acf113d2fde5870ce54b22f7b50c0829f9827925707193"
)
BASELINE_PROFILES = {
    "echo.profile.json",
    "echo.published.json",
    "harness-live.profile.json",
    "harness-live.published.json",
    "harness-mock.profile.json",
    "harness-mock.published.json",
    "harness-ui.profile.json",
    *(f"presets/{name}.published.json" for name in (
        "chatgpt_agent_deep_research_service",
        "claude_code_service",
        "cline_pair_programmer_service",
        "gemini_cli_service",
        "genspark_report_service",
        "manus_project_service",
        "openclaw_hermes_local_service",
    )),
}


def digest(value: object) -> str:
    """Match these StrictV1 fixture documents' canonical encoding."""
    data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


class InputEntryFixtureTests(unittest.TestCase):
    def test_all_published_and_local_profiles_have_explicit_declared_entries(self) -> None:
        directory = PACKS / "profiles"
        # Authored Profiles, including new variants; provenance is not a Profile.
        paths = {
            path for pattern in ("*.profile.json", "*.published.json")
            for path in directory.rglob(pattern)
        }
        self.assertTrue(paths)
        self.assertLessEqual(
            BASELINE_PROFILES,
            {path.relative_to(directory).as_posix() for path in paths},
        )
        documents = {
            path: json.loads(path.read_text(encoding="utf-8"))
            for path in directory.rglob("*.json")
        }
        self.assertEqual(
            {path for path, document in documents.items() if "startup" in document},
            paths,
            "every authored Profile needs startup; no startup JSON may be skipped",
        )
        for path in sorted(paths):
            with self.subTest(profile=path.relative_to(directory).as_posix()):
                profile = documents[path]
                self.assertIn("startup", profile)
                self.assertIsInstance(profile["startup"]["inputs"], list)
                self.assertTrue(profile["startup"]["inputs"])
                pins = {pin["id"]: pin for pin in profile["packs"]}
                for entry in profile["startup"]["inputs"]:
                    self.assertEqual(
                        set(entry), {"source_pack", "exact_input_view", "operation"}
                    )
                    source = entry["source_pack"]
                    manifest = json.loads(
                        (PACKS / source / "pack.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(pins[source]["sha256"], digest(manifest))
                    declaration = {
                        key: entry[key] for key in ("exact_input_view", "operation")
                    }
                    self.assertIn(declaration, manifest["inputs"])
                    name, version = entry["exact_input_view"].rsplit("@", 1)
                    self.assertTrue(any(
                        port["shape"] == name and port["version"] == version
                        for port in manifest["ports"]["provides"]
                    ))

    def test_historical_receipt_preserves_its_frozen_migration_scope(self) -> None:
        # This source-only receipt describes the original migration, not current
        # Profile or manifest bytes after subsequent legitimate producer changes.
        raw = MIGRATION_RECEIPT.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), MIGRATION_RECEIPT_SHA256)
        receipt = json.loads(raw)
        self.assertEqual(set(receipt), {
            "format", "source_only", "rust_tests_executed",
            "component_bytes_or_guest_WIT_changed",
            "old_receipts_and_837_todo_unchanged", "profile_changes",
            "manifest_changes",
        })
        self.assertEqual(receipt["format"], "tobkiri.native-input-source-migration/1")
        self.assertIs(receipt["source_only"], True)
        self.assertIs(receipt["rust_tests_executed"], False)
        self.assertIs(receipt["component_bytes_or_guest_WIT_changed"], False)
        self.assertIs(receipt["old_receipts_and_837_todo_unchanged"], True)
        self.assertEqual(
            set(receipt["profile_changes"]),
            {f"packs/profiles/{name}" for name in BASELINE_PROFILES},
        )
        declarations = {
            "local.echo-input": {
                "exact_input_view": "local.echo-chat.user-input@1.0",
                "operation": "read",
            },
            "local.terminal-input": {
                "exact_input_view": "local.harness-chat.user-input@1.0",
                "operation": "read",
            },
        }
        self.assertEqual(set(receipt["manifest_changes"]), set(declarations))
        for relative, change in receipt["profile_changes"].items():
            self.assertEqual(set(change), {
                "before", "after", "ceiling_unchanged", "pack_limits_unchanged",
                "startup_before", "startup_after",
            })
            for key in ("before", "after"):
                self.assertRegex(change[key], r"\Asha256:[0-9a-f]{64}\Z")
            self.assertNotEqual(change["before"], change["after"])
            self.assertIs(change["ceiling_unchanged"], True)
            self.assertIs(change["pack_limits_unchanged"], True)
            source = (
                "local.echo-input" if Path(relative).name.startswith("echo.")
                else "local.terminal-input"
            )
            self.assertEqual(change["startup_before"], [source])
            self.assertEqual(change["startup_after"], [
                {"source_pack": source, **declarations[source]}
            ])
        for source, change in receipt["manifest_changes"].items():
            self.assertEqual(set(change), {
                "before", "after", "declaration", "file_inventory_unchanged"
            })
            for key in ("before", "after"):
                self.assertRegex(change[key], r"\Asha256:[0-9a-f]{64}\Z")
            self.assertNotEqual(change["before"], change["after"])
            self.assertEqual(change["declaration"], declarations[source])
            self.assertIs(change["file_inventory_unchanged"], True)

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
