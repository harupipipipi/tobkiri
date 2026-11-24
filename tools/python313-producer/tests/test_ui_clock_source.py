"""Tiny source-only checks; no native compiler or Wasm execution.

The Rust text fixture is the unchanged Apache-2.0/LLVM-exception licensed
bytecodealliance/componentize-py src/stubwasi.rs at c0949b19d464f5d70bc1051195a3ae0e6a012df9.
"""

import hashlib
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import build_cli
from fetch_sdk import inventory
import ui_clock_source

STUB = (Path(__file__).parent / "fixtures/stubwasi-c0949b19.rs.txt").read_bytes()
MANIFEST = b"""[package]
name = "componentize-py"
build = "alt-build.rs"
[dependencies]
semver = "1.0.23"
[workspace]
package.version = "0.25.0"
"""


class UiClockSourceTests(unittest.TestCase):
    def test_only_allowlisted_clock_branch_changes_original_generator(self):
        self.assertEqual(
            hashlib.sha256(STUB).hexdigest(),
            "52f5e28de03d1bad02de3167522b87d3f9457dd6d680daa4e90b609708cca2cb",
        )
        derived = ui_clock_source.patch_stub_source(STUB)
        self.assertEqual(
            derived.replace(ui_clock_source.REPLACEMENT, ui_clock_source.ANCHOR), STUB
        )
        self.assertIn(b'matches!(import.name, "now" | "resolution")', derived)
        self.assertIn(b'feature = "ui-wall-clock"', derived)
        self.assertIn(b"function.instruction(&Ins::Unreachable);", derived)
        self.assertIn(b"assert!(new_adapter_count >= old_adapter_count);", derived)
        self.assertNotIn(b"get_environment", derived)

    def test_unknown_duplicate_or_already_derived_generator_is_refused(self):
        for raw in (
            STUB.replace(ui_clock_source.ANCHOR, b"changed"),
            STUB + ui_clock_source.ANCHOR,
            ui_clock_source.patch_stub_source(STUB),
        ):
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                ui_clock_source.patch_stub_source(raw)

    def test_feature_changes_no_dependencies_or_existing_manifest_fields(self):
        derived = ui_clock_source.feature_manifest(MANIFEST)
        document = tomllib.loads(derived.decode())
        self.assertEqual(document.pop("features"), {"ui-wall-clock": []})
        self.assertEqual(document, tomllib.loads(MANIFEST.decode()))
        self.assertTrue(derived.startswith(MANIFEST))
        for raw in (derived, MANIFEST.replace(b"componentize-py", b"other")):
            with self.assertRaises(ValueError):
                ui_clock_source.feature_manifest(raw)

    def test_build_recipe_has_only_explicit_feature_delta(self):
        original = (HERE / "trials/2026-10-19/producer-cli.sh").read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(), build_cli.RECIPE_SHA256)
        derived = ui_clock_source.build_recipe(original)
        self.assertEqual(derived.replace(b" --features ui-wall-clock", b""), original)
        for raw in (original + ui_clock_source.BUILD, b"other command"):
            with self.assertRaises(ValueError):
                ui_clock_source.build_recipe(raw)

    def stage_fixture(self, root):
        (root / "src").mkdir()
        (root / "src/stubwasi.rs").write_bytes(STUB)
        (root / "Cargo.toml").write_bytes(MANIFEST)
        (root / "asset.zst").write_bytes(b"unchanged authenticated asset")
        return {
            "files": inventory(root),
            "original_files": {"provenance": "retained"},
            "delta": "original publishing staging",
        }

    def test_private_candidate_has_only_two_source_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            receipt = self.stage_fixture(root)
            result = ui_clock_source.enable(root, receipt)
            changed = {
                name
                for name in result["files"]
                if result["files"][name] != receipt["files"][name]
            }
            self.assertEqual(changed, {"Cargo.toml", "src/stubwasi.rs"})
            self.assertEqual(result["original_files"], receipt["original_files"])
            self.assertEqual(result["files"], inventory(root))
            self.assertTrue(result["ui_wall_clock"]["stub_wasi_retained"])
            self.assertEqual(
                result["ui_wall_clock"]["functions"], ["now", "resolution"]
            )
            self.assertEqual(
                (root / "asset.zst").read_bytes(), b"unchanged authenticated asset"
            )

    def test_changed_or_linked_source_is_refused_before_mutation(self):
        for kind in ("changed", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                receipt = self.stage_fixture(root)
                if kind == "changed":
                    (root / "asset.zst").write_bytes(b"tampered")
                else:
                    (root / "link").symlink_to("Cargo.toml")
                    receipt["files"] = inventory(root)
                with self.assertRaises(ValueError):
                    ui_clock_source.enable(root, receipt)
                self.assertEqual((root / "src/stubwasi.rs").read_bytes(), STUB)
                self.assertEqual((root / "Cargo.toml").read_bytes(), MANIFEST)

    def test_clock_variant_cannot_run_a_desktop_build(self):
        with patch.object(build_cli.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "Linux CI"):
                build_cli.run(*[Path("/unused")] * 8, allow_ui_wall_clock=True)


if __name__ == "__main__":
    unittest.main()
