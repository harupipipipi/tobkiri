"""Synthetic checks for the pinned CI-only Linker observation delta."""

from __future__ import annotations

import hashlib
from pathlib import Path
import stat
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_link_trace_source as trace
import fetch_sdk
import ui_clock_source
import ui_monotonic_source


FIXTURE = Path(__file__).with_name("fixtures") / "link-c0949b19.rs.txt"


def _cargo_manifest(mode: str) -> bytes:
    rows = [
        '[package]\nname = "componentize-py"\nversion = "0.25.0"\n'
        'edition = "2021"\ndefault-run = "componentize-py"\n',
        '[dependencies]\nanyhow = "1.0"\nwit-component = "0.220"\n',
    ]
    if mode == "wall":
        rows.append("[features]\nui-wall-clock = []\n")
    elif mode == "monotonic":
        rows.append(
            '[features]\nui-wall-clock = []\nui-monotonic-clock = ["ui-wall-clock"]\n'
        )
    return "\n".join(rows).encode()


def _stage(root: Path) -> tuple[Path, dict[str, bytes], dict[str, int]]:
    stage = root / "source"
    stage.mkdir()
    contents = {
        "Cargo.toml": _cargo_manifest("base"),
        "Cargo.lock": b'version = 3\n\n[[package]]\nname = "locked"\n',
        "src/link.rs": FIXTURE.read_bytes(),
        "src/lib.rs": b"pub fn retained() {}\n",
        "assets/worker.wasm": b"asset bytes stay exact",
    }
    modes = {
        "Cargo.toml": 0o644,
        "Cargo.lock": 0o644,
        "src/link.rs": 0o644,
        "src/lib.rs": 0o644,
        "assets/worker.wasm": 0o755,
    }
    for relative, content in contents.items():
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(modes[relative])
    return stage.resolve(), contents, modes


class CiLinkTraceSourceTests(unittest.TestCase):
    def test_exact_link_source_pin_and_observation_order(self):
        original = FIXTURE.read_bytes()
        self.assertEqual(
            hashlib.sha256(original).hexdigest(),
            "a663aacbd3b47eb8b674f2a4119e6a1ee089271085a5c89e4ce79620b7999015",
        )
        candidate = trace.patch_link_source(original)
        record_call = b"let ci_trace = ci_record_link_inputs(libraries)?;"
        self.assertLess(
            candidate.index(record_call), candidate.index(b"let mut linker")
        )
        self.assertIn(
            b"linker = linker.library(name, module, *dl_openable)?;", candidate
        )
        self.assertIn(
            b'linker = linker.adapter(\n        "wasi_snapshot_preview1",', candidate
        )
        self.assertLess(
            candidate.index(b"linker.library(name, module, *dl_openable)?"),
            candidate.index(b"linker = linker.adapter"),
        )
        self.assertLess(
            candidate.index(b"let component = linker.encode()"),
            candidate.index(b"ci_trace.finish(&component)?"),
        )
        recorder = trace.RECORDER.split(b"fn finish(self, component: &[u8])", 1)[1]
        self.assertLess(
            recorder.index(b"preinit-linked-component.wasm"),
            recorder.index(b"complete.tsv"),
        )
        self.assertIn(b"create_new(true)", trace.RECORDER)
        before_finish = trace.RECORDER.split(b"fn finish(self, component: &[u8])", 1)[0]
        self.assertNotIn(b"complete.tsv", before_finish)

    def test_source_pin_rejects_changes_and_reapplication(self):
        original = FIXTURE.read_bytes()
        candidate = trace.patch_link_source(original)
        with self.assertRaisesRegex(ValueError, "exact pinned original"):
            trace.patch_link_source(original + b"\n")
        with self.assertRaisesRegex(ValueError, "exact pinned original"):
            trace.patch_link_source(candidate)

    def test_only_explicit_feature_is_added_for_three_cargo_states(self):
        for mode in ("base", "wall", "monotonic"):
            with self.subTest(mode=mode):
                original = _cargo_manifest(mode)
                before = tomllib.loads(original.decode())
                updated = tomllib.loads(trace.feature_manifest(original).decode())
                expected_features = {
                    **before.get("features", {}),
                    trace.FEATURE: [],
                }
                self.assertEqual(updated, {**before, "features": expected_features})
                self.assertEqual(updated["package"], before["package"])
                self.assertEqual(updated["dependencies"], before["dependencies"])
        with self.assertRaisesRegex(ValueError, "original or authenticated"):
            trace.feature_manifest(
                _cargo_manifest("base") + b"\n[features]\nci-link-input-trace=[]\n"
            )

    def test_recipe_appends_trace_feature_without_changing_three_modes(self):
        original = b"set -eu\n" + ui_clock_source.BUILD + b"echo done\n"
        wall = ui_clock_source.build_recipe(original)
        monotonic = ui_monotonic_source.build_recipe(wall)
        expected = (
            (
                original,
                ui_clock_source.BUILD,
                ui_clock_source.BUILD.rstrip(b"\n")
                + b" --features ci-link-input-trace\n",
            ),
            (
                wall,
                ui_clock_source.BUILD.rstrip(b"\n") + b" --features ui-wall-clock\n",
                ui_clock_source.BUILD.rstrip(b"\n")
                + b" --features ui-wall-clock,ci-link-input-trace\n",
            ),
            (
                monotonic,
                ui_clock_source.BUILD.rstrip(b"\n")
                + b" --features ui-monotonic-clock\n",
                ui_clock_source.BUILD.rstrip(b"\n")
                + b" --features ui-monotonic-clock,ci-link-input-trace\n",
            ),
        )
        for recipe, old_line, new_line in expected:
            with self.subTest(old_line=old_line):
                changed = trace.build_recipe(recipe)
                self.assertEqual(changed, recipe.replace(old_line, new_line, 1))
                self.assertEqual(changed.count(b"ci-link-input-trace"), 1)
                self.assertEqual(changed.count(new_line), 1)
        with self.assertRaisesRegex(ValueError, "build command"):
            trace.build_recipe(trace.build_recipe(original))

    def test_stage_changes_only_cargo_and_link_and_rejects_tamper_or_reapply(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, contents, modes = _stage(Path(tmp).resolve())
            before = fetch_sdk.inventory(stage)
            result = trace.enable(stage, {"files": before, "delta": "original"})
            after = fetch_sdk.inventory(stage)
            self.assertEqual(result["files"], after)
            self.assertEqual(
                set(path for path in after if after[path] != before[path]),
                {"Cargo.toml", "src/link.rs"},
            )
            for relative in ("Cargo.lock", "src/lib.rs", "assets/worker.wasm"):
                target = stage / relative
                self.assertEqual(target.read_bytes(), contents[relative])
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), modes[relative])
            self.assertFalse(result["ci_link_trace"]["production_enabled"])
            with self.assertRaisesRegex(ValueError, "authenticated physical"):
                trace.enable(stage, result)

        with tempfile.TemporaryDirectory() as tmp:
            stage, contents, _modes = _stage(Path(tmp).resolve())
            receipt = {"files": fetch_sdk.inventory(stage), "delta": "original"}
            original_cargo = (stage / "Cargo.toml").read_bytes()
            original_link = (stage / "src/link.rs").read_bytes()
            asset = stage / "assets/worker.wasm"
            asset.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "authenticated physical"):
                trace.enable(stage, receipt)
            self.assertEqual((stage / "Cargo.toml").read_bytes(), original_cargo)
            self.assertEqual((stage / "src/link.rs").read_bytes(), original_link)
            self.assertNotEqual(asset.read_bytes(), contents["assets/worker.wasm"])


if __name__ == "__main__":
    unittest.main()
