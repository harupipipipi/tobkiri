"""DeepThink authored assets survive repeated staging; no guest/Core is run."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKS))
import build
import build_deepthink_profile as deepthink


class DeepThinkUiAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "local.harness-ui"
        self.destination = self.root / "local.deepthink-ui"
        self.paths = {
            "ui/app.js": b"old base Harness app",
            "ui/style.css": b"base CSS",
            "ui/index.html": b"base HTML",
        }
        for relative, content in self.paths.items():
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest = build.base_manifest(
            "harness-ui", {"kind": "data"}, build.permission_set(),
        )
        build.bind_files(self.base, manifest, list(self.paths))
        self.authored = self.destination / "ui/app.js"
        self.authored.parent.mkdir(parents=True)
        self.authored.write_bytes(b"new authored DeepThink continuation app")
        override = patch.object(deepthink, "ROOT", self.root)
        override.start()
        self.addCleanup(override.stop)

    def test_two_stages_preserve_authored_app_and_bind_its_actual_hash(self):
        expected = self.authored.read_bytes()
        pins = []
        for _ in range(2):
            manifest = deepthink.variant(
                "harness-ui", "deepthink-ui",
                asset_overrides={"ui/app.js": self.authored},
            )
            self.assertEqual(self.authored.read_bytes(), expected)
            for relative, base_content in self.paths.items():
                self.assertEqual((self.base / relative).read_bytes(), base_content)
                selected = expected if relative == "ui/app.js" else base_content
                self.assertEqual((self.destination / relative).read_bytes(), selected)
                entry = next(item for item in manifest["files"]
                             if item["path"] == relative)
                self.assertEqual(entry["sha256"], build.sha256(selected))
            pins.append(build.sha256(build.canonical(manifest)))
        self.assertEqual(pins[0], pins[1])
        default = deepthink.variant("harness-ui", "ordinary-copy")
        copied = self.root / "local.ordinary-copy/ui/app.js"
        self.assertEqual(copied.read_bytes(), self.paths["ui/app.js"])
        entry = next(item for item in default["files"]
                     if item["path"] == "ui/app.js")
        self.assertEqual(entry["sha256"], build.sha256(copied.read_bytes()))

    def test_unknown_and_missing_overrides_reject_before_copying_any_asset(self):
        before = self.authored.read_bytes()
        for overrides, error in [
            ({"ui/not-declared.js": self.authored}, ValueError),
            ({"ui/app.js": self.root / "missing.js"}, FileNotFoundError),
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(error):
                deepthink.variant("harness-ui", "deepthink-ui",
                                  asset_overrides=overrides)
            self.assertEqual(self.authored.read_bytes(), before)
            self.assertFalse((self.destination / "ui/style.css").exists())
            self.assertFalse((self.destination / "pack.json").exists())


if __name__ == "__main__":
    unittest.main()
