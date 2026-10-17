"""Profile source variants preserve exact pins and explicit Pack authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKS))
import build
import build_deepthink_profile as deepthink


class ProfileSourceVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        """Copy only the small data Pack; no guest or build is executed."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = json.loads(
            (PACKS / "local.harness-chat/pack.json").read_text(encoding="utf-8")
        )
        self.original_bytes = {}
        for relative in ["pack.json"] + [
            entry["path"] for entry in self.original["files"]
        ]:
            source = PACKS / "local.harness-chat" / relative
            destination = self.root / "local.harness-chat" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            self.original_bytes[relative] = source.read_bytes()
        for module in [build, deepthink]:
            override = patch.object(module, "ROOT", self.root)
            override.start()
            self.addCleanup(override.stop)

    def test_default_and_nested_override_resolve_from_each_profile(self) -> None:
        variant = self.root / "deepthink-variants/local.harness-chat"
        variant.mkdir(parents=True)
        original_pin = {"id": "local.harness-chat", "sha256": "unchanged"}
        for nested in [False, True]:
            destination = self.root / "profiles"
            if nested:
                destination /= "deepthink-presets"
            destination /= "example.profile.json"
            for overrides, expected in [
                (None, self.root / "local.harness-chat"),
                ({"local.harness-chat": variant}, variant),
            ]:
                with self.subTest(nested=nested, override=overrides is not None):
                    document = {"packs": [copy.deepcopy(original_pin)]}
                    build.portable_sources(document, destination, overrides)
                    pin = document["packs"][0]
                    self.assertEqual(pin["sha256"], original_pin["sha256"])
                    self.assertEqual(
                        (destination.parent / pin["source"][5:]).resolve(),
                        expected.resolve(),
                    )

    def test_missing_explicit_source_does_not_fall_back_to_base(self) -> None:
        document = {"packs": [{"id": "local.harness-chat"}]}
        with self.assertRaisesRegex(ValueError, "directory is missing"):
            build.portable_sources(
                document,
                self.root / "profiles/example.profile.json",
                {"local.harness-chat": self.root / "missing"},
            )

    def test_variant_keeps_identity_and_file_bytes_but_declares_workflow(self) -> None:
        variant = deepthink.workflow_harness_variant()
        self.assertEqual(variant["id"], self.original["id"])
        self.assertEqual(variant["shapes"], self.original["shapes"])
        self.assertEqual(variant["files"], self.original["files"])
        self.assertEqual(
            set(variant["permissions"]["ports"])
            - set(self.original["permissions"]["ports"]),
            {"local.deepthink.workflow@1.0"},
        )
        self.assertIn(
            {"shape": "local.deepthink.workflow", "version": "1.0",
             "optional": False},
            variant["ports"]["consumes"],
        )
        self.assertNotEqual(
            build.sha256(build.canonical(variant)),
            build.sha256(build.canonical(self.original)),
        )
        for relative, original in self.original_bytes.items():
            self.assertEqual(
                (self.root / "local.harness-chat" / relative).read_bytes(),
                original,
            )
            if relative != "pack.json":
                self.assertEqual(
                    (self.root / "deepthink-variants/local.harness-chat"
                     / relative).read_bytes(),
                    original,
                )

    def test_deepthink_profile_pins_variant_and_limits_its_workflow_call(self) -> None:
        names = [
            "terminal-input", "harness-conversation-store", "harness-prompt",
            "openai-compat-provider", "terminal-output", "harness-agent",
            "harness-tools", "deepthink", "openrouter-mimo-provider",
            "mimo-prompt", "deepthink-ui",
        ]
        manifests = {
            name: json.loads(
                (PACKS / ("local." + name) / "pack.json").read_text(
                    encoding="utf-8"
                )
            )
            for name in names
        }
        # Staging-only bytes: even a correct component header is not ABI proof.
        target = self.root / "target"
        component = (target / "wasm32-wasip2/release"
                     / "tobkiri_pack_openrouter_generation_reader.wasm")
        component.parent.mkdir(parents=True)
        component.write_bytes(b"\0asm\r\0\1\0public staging fixture; not compiled Wasm")
        reader = deepthink.stage_generation_reader(target, build_profile="release")
        manifests["openrouter-generation-reader"] = reader
        component.with_name("tobkiri_pack_deepthink_input.wasm").write_bytes(
            b"\0asm\r\0\1\0public input staging fixture; not compiled Wasm",
        )
        adapter = deepthink.build_deepthink_input.stage(
            target, self.root / "local.deepthink-input", build_profile="release",
        )
        manifests["deepthink-input"] = adapter
        variant = deepthink.workflow_harness_variant()
        manifests["harness-chat"] = variant
        for ui in [False, True]:
            with self.subTest(ui=ui):
                document = deepthink.compose(manifests, ui=ui)
                pin = next(pin for pin in document["packs"]
                           if pin["id"] == "local.harness-chat")
                self.assertEqual(pin["sha256"], build.sha256(build.canonical(variant)))
                self.assertIn(
                    "local.deepthink.workflow@1.0",
                    document["pack_limits"]["local.harness-chat"]["ports"],
                )
                self.assertIn(
                    "local.deepthink.workflow@1.0", document["ceiling"]["ports"]
                )
                reader_id = "local.openrouter-generation-reader"
                reader_view = reader_id + ".generation-reader@1.0"
                pins = [pin for pin in document["packs"] if pin["id"] == reader_id]
                self.assertEqual(len(pins), 1)
                self.assertEqual(
                    pins[0]["sha256"], build.sha256(build.canonical(reader)),
                )
                self.assertEqual(
                    document["bindings"][reader_id + ".generation-reader@1"],
                    reader_id,
                )
                self.assertEqual(
                    reader["shapes"][0]["name"], reader_view.rsplit("@", 1)[0],
                )
                self.assertEqual(
                    reader["ports"]["provides"],
                    [build.port(reader_view.rsplit("@", 1)[0])],
                )
                self.assertEqual(
                    document["pack_limits"][reader_id],
                    build.intersect_permissions(
                        reader["permissions"], document["ceiling"],
                    ),
                )
                self.assertEqual(
                    document["bindings"]["local.deepthink.continuation-input@1"],
                    "local.deepthink-input",
                )
                self.assertEqual(
                    document["bindings"]["local.deepthink.input-context@1"],
                    "local.deepthink",
                )
                self.assertEqual(adapter["shapes"], [])
                self.assertEqual(
                    document["pack_limits"]["local.deepthink-input"],
                    build.intersect_permissions(adapter["permissions"], document["ceiling"]),
                )
                for private in ("local.deepthink.input-context@1.0",
                                "local.deepthink.control-flow@1.0"):
                    self.assertNotIn(private, document["user_permissions"]["ports"])
                self.assertNotIn(
                    reader_view, document["user_permissions"]["ports"],
                )
                self.assertFalse(
                    document["pack_limits"][reader_id]["storage"]["private"],
                )
                self.assertEqual(document["pack_limits"][reader_id]["fs"], [])
                self.assertEqual(document["pack_limits"][reader_id]["proc"], [])


if __name__ == "__main__":
    unittest.main()
