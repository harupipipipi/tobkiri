"""Offline derivation boundaries; actual adapter execution remains CI-only."""

from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import build_cli
import build_schema
from fetch_sdk import inventory
import ui_clock_source
import ui_monotonic_source

STUB = (Path(__file__).parent / "fixtures/stubwasi-c0949b19.rs.txt").read_bytes()
MANIFEST = b'[package]\nname = "componentize-py"\n[dependencies]\nsemver = "1.0.23"\n'


class UiMonotonicSourceTests(unittest.TestCase):
    def stage_wall_candidate(self, root):
        (root / "src").mkdir()
        (root / "src/stubwasi.rs").write_bytes(STUB)
        (root / "Cargo.toml").write_bytes(MANIFEST)
        (root / "asset.zst").write_bytes(b"authenticated original asset")
        receipt = {"files": inventory(root), "delta": "original", "original_files": {}}
        return ui_clock_source.enable(root, receipt)

    def test_existing_wall_and_nonclock_code_is_preserved_exactly(self):
        wall = ui_clock_source.patch_stub_source(STUB)
        derived = ui_monotonic_source.patch_stub_source(wall)
        self.assertTrue(derived.endswith(ui_monotonic_source.ADAPTER))
        restored = derived[: -len(ui_monotonic_source.ADAPTER)].replace(
            ui_monotonic_source.REPLACEMENT, ui_monotonic_source.ANCHOR, 1
        )
        self.assertEqual(restored, wall)
        self.assertEqual(derived.count(ui_clock_source.REPLACEMENT), 1)
        self.assertIn(b"function.instruction(&Ins::Unreachable);", derived)
        self.assertIn(b"let index = 2 + defined_functions;", derived)
        self.assertIn(b'"now" => 0,', derived)
        self.assertIn(b'"resolution" => 1,', derived)

    def test_raw_unknown_duplicate_and_already_derived_sources_are_refused(self):
        wall = ui_clock_source.patch_stub_source(STUB)
        for original in (
            STUB,
            wall.replace(ui_monotonic_source.ANCHOR, b"unknown dispatch"),
            wall + ui_monotonic_source.ANCHOR,
            ui_monotonic_source.patch_stub_source(wall),
        ):
            with self.subTest(length=len(original)), self.assertRaises(ValueError):
                ui_monotonic_source.patch_stub_source(original)

    def test_only_explicit_feature_and_its_wall_dependency_are_added(self):
        wall = ui_clock_source.feature_manifest(MANIFEST)
        updated = tomllib.loads(ui_monotonic_source.feature_manifest(wall).decode())
        self.assertEqual(
            updated.pop("features"),
            {"ui-wall-clock": [], "ui-monotonic-clock": ["ui-wall-clock"]},
        )
        self.assertEqual(updated, tomllib.loads(MANIFEST.decode()))
        for invalid in (MANIFEST, wall + b"unrelated = []\n"):
            with self.assertRaises(ValueError):
                ui_monotonic_source.feature_manifest(invalid)

    def test_original_and_wall_build_recipes_remain_separate(self):
        original = (HERE / "trials/2026-10-19/producer-cli.sh").read_bytes()
        wall = ui_clock_source.build_recipe(original)
        mono = ui_monotonic_source.build_recipe(wall)
        self.assertEqual(mono.replace(b" --features ui-monotonic-clock", b""), original)
        self.assertEqual(wall, ui_clock_source.build_recipe(original))
        for invalid in (original, wall + wall, mono):
            with self.assertRaises(ValueError):
                ui_monotonic_source.build_recipe(invalid)

    def test_full_source_inventory_and_both_candidate_identities_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wall = self.stage_wall_candidate(root)
            result = ui_monotonic_source.enable(root, wall)
            self.assertEqual(result["ui_wall_clock"], wall["ui_wall_clock"])
            self.assertEqual(result["original_files"], wall["original_files"])
            self.assertEqual(result["files"], inventory(root))
            changed = {
                name
                for name in wall["files"]
                if wall["files"][name] != result["files"][name]
            }
            self.assertEqual(changed, {"Cargo.toml", "src/stubwasi.rs"})
            candidate = result["ui_monotonic_clock"]
            self.assertEqual(
                candidate["import_interface"], ui_monotonic_source.IMPORT_INTERFACE
            )
            self.assertEqual(candidate["functions"], ["now", "resolution"])
            self.assertTrue(candidate["stub_wasi_retained"])
            for name, entry in candidate["changed_files"].items():
                self.assertEqual(entry["wall_clock_candidate"], wall["files"][name])
                self.assertEqual(entry["candidate"], result["files"][name])
            self.assertEqual(
                (root / "asset.zst").read_bytes(), b"authenticated original asset"
            )
            with self.assertRaises(ValueError):
                ui_monotonic_source.enable(root, result)

    def test_tampered_source_is_refused_before_any_candidate_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wall = self.stage_wall_candidate(root)
            (root / "asset.zst").write_bytes(b"tampered")
            before = inventory(root)
            with self.assertRaises(ValueError):
                ui_monotonic_source.enable(root, wall)
            self.assertEqual(inventory(root), before)

    def test_monotonic_option_cannot_silently_enable_wall_clock(self):
        missing = Path("/must-not-be-created")
        with (
            patch.object(sys, "platform", "linux"),
            patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}),
        ):
            with self.assertRaisesRegex(ValueError, "explicit wall-clock"):
                build_cli.run(*([missing] * 8), allow_ui_monotonic_clock=True)

    def test_historical_consumer_rejects_either_monotonic_identity(self):
        for enabled in (False, True):
            for cli, source in (
                ({"ui_monotonic_clock_candidate": {}}, {}),
                ({}, {"ui_monotonic_clock": {}}),
            ):
                with self.subTest(wall=enabled, cli=cli, source=source):
                    with self.assertRaisesRegex(
                        ValueError, "separate consuming controller"
                    ):
                        build_schema.verify_clock_variant(
                            cli, source, allow_ui_wall_clock=enabled
                        )
        build_schema.verify_clock_variant({}, {}, allow_ui_wall_clock=False)
        with self.assertRaisesRegex(ValueError, "explicit controller"):
            build_schema.verify_clock_variant(
                {}, {"ui_wall_clock": {}}, allow_ui_wall_clock=False
            )

    def test_explicit_consumer_requires_the_full_two_stage_source_ancestry(self):
        import copy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wall = self.stage_wall_candidate(root)
            source = ui_monotonic_source.enable(root, wall)
            cli = {
                "ui_wall_clock_candidate": source["ui_wall_clock"],
                "ui_monotonic_clock_candidate": source["ui_monotonic_clock"],
            }
            build_schema.verify_clock_variant(
                cli, source, allow_ui_wall_clock=True, allow_ui_monotonic_clock=True
            )
            with self.assertRaisesRegex(ValueError, "explicit wall-clock"):
                build_schema.verify_clock_variant(
                    cli,
                    source,
                    allow_ui_wall_clock=False,
                    allow_ui_monotonic_clock=True,
                )
            for field in ("wall_clock_candidate", "candidate"):
                forged = copy.deepcopy(source)
                forged["ui_monotonic_clock"]["changed_files"]["Cargo.toml"][field] = {}
                forged_cli = {
                    "ui_wall_clock_candidate": forged["ui_wall_clock"],
                    "ui_monotonic_clock_candidate": forged["ui_monotonic_clock"],
                }
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(ValueError, "ancestry"),
                ):
                    build_schema.verify_clock_variant(
                        forged_cli,
                        forged,
                        allow_ui_wall_clock=True,
                        allow_ui_monotonic_clock=True,
                    )


if __name__ == "__main__":
    unittest.main()
