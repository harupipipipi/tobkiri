"""Isolated staging contracts; fixture bytes are not verified Wasm components.

No Cargo, Core, model, credential, runtime DB or network operation is performed.
Every staged manifest/profile output stays in this test's temporary directory.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PACKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKS))
import build
import build_deepthink
import build_deepthink_input
import build_deepthink_profile
import build_harness_tools


class StopBeforeStaging(Exception):
    """Capture a command/argument boundary without executing its effect."""


class BuildProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.root = self.repo / "packs"
        self.target = self.repo / "target"
        self.names = {
            "baseline": "tobkiri_pack_mock_llm.wasm",
            "deepthink": "tobkiri_pack_deepthink.wasm",
            "tools": "tobkiri_pack_harness_tools.wasm",
            "reader": "tobkiri_pack_openrouter_generation_reader.wasm",
            "input": "tobkiri_pack_deepthink_input.wasm",
        }
        for profile in ["debug", "release"]:
            for filename in self.names.values():
                content = f"NOT-WASM: public {profile} fixture {filename}".encode()
                if filename in (self.names["reader"], self.names["input"]):
                    # The staging header check is exercised, not Core ABI admission.
                    content = b"\0asm\r\0\1\0" + content
                self.write(self.target / "wasm32-wasip2" / profile / filename,
                           content)
        common = ["Cargo.toml", "Cargo.lock", "build.py", "guest-sdk/src/lib.rs",
                  "build_harness_tools.py", "build_deepthink_input.py", "shapes/harness-chat.wit",
                  "shapes/harness-agent.wit", "shapes/harness-tools.wit",
                  "shapes/deepthink.wit",
                  "recovery/build_generation_reader.py",
                  "recovery/local.openrouter-generation-reader/wit/deps/reader.wit"]
        baseline = ["Cargo.toml", "build.rs", "src/lib.rs", "wit/pack.wit",
                    "wit/deps/core.wit", "wit/deps/shape.wit"]
        deepthink = ["Cargo.toml", "build.rs", "src/lib.rs", "wit/pack.wit",
                     "wit/deps/core.wit", "wit/deps/core-v1-1.wit",
                     "wit/deps/generation-reader.wit", "wit/deps/deepthink.wit",
                     "wit/deps/harness-chat.wit", "wit/deps/harness-agent.wit",
                     "overlays/generate.overlay.json", "flows/continuation.flow.json",
                     "flows/regeneration.flow.json", "flows/control.flow.json"]
        input_source = ["Cargo.toml", "build.rs", "src/lib.rs", "src/control_flow.rs",
                        "wit/pack.wit"]
        tools = ["Cargo.toml", "build.rs", "src/lib.rs", "wit/pack.wit",
                 "wit/artifacts.wit", "wit/deps/core.wit", "wit/deps/harness-agent.wit"]
        for relative in common:
            self.write(self.root / relative, b"public source fixture")
        for name, paths in [("mock-llm", baseline), ("deepthink", deepthink),
                            ("deepthink-input", input_source),
                            ("harness-tools", tools)]:
            for relative in paths:
                self.write(self.root / ("local." + name) / relative, b"public source fixture")
        reader_source = self.root / "recovery/local.openrouter-generation-reader"
        for relative in ["Cargo.toml", "Cargo.lock", "build.rs", "src/lib.rs",
                         "wit/pack.wit", "wit/deps/reader.wit"]:
            self.write(reader_source / relative, b"public Reader source fixture")
        for version in ["core", "core-v1-1"]:
            self.write(self.repo / "crates/tobkiri-wit/wit" / version / "core.wit",
                       b"public WIT fixture; not ABI acceptance")
        for module, name, value in [
            (build, "ROOT", self.root), (build, "REPO", self.repo),
            (build_deepthink, "ROOT", self.root),
            (build_deepthink, "SOURCE", self.root / "local.deepthink"),
            (build_deepthink_input, "ROOT", self.root),
            (build_deepthink_input, "SOURCE", self.root / "local.deepthink-input"),
            (build_deepthink_input, "__file__", str(self.root / "build_deepthink_input.py")),
            (build_harness_tools, "ROOT", self.root),
            (build_harness_tools, "REPO", self.repo),
            (build_harness_tools, "__file__", str(self.root / "build_harness_tools.py")),
            (build_deepthink_profile, "ROOT", self.root),
            (build_deepthink_profile.build_generation_reader, "SOURCE", reader_source),
            (build_deepthink_profile.build_generation_reader, "PACKS", self.root),
            (build_deepthink_profile.build_generation_reader, "REPO", self.repo),
            (build_deepthink_profile.build_generation_reader, "__file__",
             str(self.root / "recovery/build_generation_reader.py")),
        ]:
            override = patch.object(module, name, value)
            override.start()
            self.addCleanup(override.stop)

    @staticmethod
    def write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def stagers(self):
        return [
            ("baseline", "local.mock-llm", lambda **kwargs: build.guest_manifest(
                "mock-llm", self.target, "http://127.0.0.1:18080", "mock", **kwargs)),
            ("deepthink", "local.deepthink", lambda **kwargs: build_deepthink.stage(
                self.target, **kwargs)),
            ("tools", "local.harness-tools", lambda **kwargs: build_harness_tools.stage(
                self.target, **kwargs)),
            ("input", "local.deepthink-input", lambda **kwargs: build_deepthink_input.stage(
                self.target, **kwargs)),
            ("reader", "local.openrouter-generation-reader",
             lambda **kwargs: build_deepthink_profile.stage_generation_reader(
                 self.target, **kwargs)),
        ]

    def test_default_debug_and_explicit_release_select_distinct_unchanged_paths(self):
        filename = self.names["baseline"]
        debug = build.component_artifact(self.target, filename)
        release = build.component_artifact(self.target, filename, "release")
        self.assertEqual(debug.parent.name, "debug")
        self.assertEqual(release.parent.name, "release")
        self.assertNotEqual(debug.read_bytes(), release.read_bytes())
        self.assertIn(b"debug", debug.read_bytes())
        self.assertIn(b"release", release.read_bytes())

    def test_missing_or_invalid_selected_profile_never_falls_back(self):
        filename = self.names["baseline"]
        release = build.component_artifact(self.target, filename, "release")
        release.unlink()
        with self.assertRaises(FileNotFoundError) as missing:
            build.component_artifact(self.target, filename, "release")
        self.assertIn(str(release), str(missing.exception))
        self.assertTrue(build.component_artifact(self.target, filename).is_file())
        for invalid in ["production", "../debug", "", "Release"]:
            with self.subTest(profile=invalid), self.assertRaises(ValueError):
                build.component_artifact(self.target, filename, invalid)

    def test_all_component_stagers_bind_selected_bytes_profile_and_hashes(self):
        originals = {path: path.read_bytes() for path in self.target.rglob("*.wasm")}
        for name, directory, stage in self.stagers():
            for selected in [None, "release"]:
                profile = selected or "debug"
                with self.subTest(stager=name, profile=profile):
                    manifest = stage(**({} if selected is None else {"build_profile": selected}))
                    output = self.root / directory
                    artifact = build.component_artifact(self.target, self.names[name], profile)
                    self.assertEqual((output / "component.wasm").read_bytes(), artifact.read_bytes())
                    provenance = json.loads((output / "component.build.json").read_text())
                    self.assertEqual(provenance["build_profile"], profile)
                    self.assertEqual(provenance["artifact_sha256"], build.sha256(artifact.read_bytes()))
                    self.assertFalse(provenance["production_enabled"])
                    for entry in manifest["files"]:
                        self.assertEqual(entry["sha256"], build.sha256((output / entry["path"]).read_bytes()))
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_all_stagers_reject_missing_release_without_touching_staged_debug(self):
        for name, directory, stage in self.stagers():
            with self.subTest(stager=name):
                stage()
                output = self.root / directory
                before = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
                build.component_artifact(self.target, self.names[name], "release").unlink()
                with self.assertRaises(FileNotFoundError):
                    stage(build_profile="release")
                self.assertEqual(before, {path: path.read_bytes() for path in output.rglob("*") if path.is_file()})

    def test_profile_stage_propagates_release_to_all_nested_components(self):
        manifest = {"id": "local.fixture", "permissions": {"ports": []},
                    "ports": {"consumes": []}, "ui": {"ports_used": []}}
        with patch.object(build_harness_tools, "stage", return_value=copy.deepcopy(manifest)) as tools, \
                patch.object(build_deepthink_profile, "stage_generation_reader",
                             return_value=copy.deepcopy(manifest)) as reader, \
                patch.object(build_deepthink_input, "stage",
                             return_value=copy.deepcopy(manifest)) as adapter, \
                patch.object(build_deepthink_profile, "variant", side_effect=lambda *a, **k: copy.deepcopy(manifest)), \
                patch.object(build_deepthink_profile.publish_presets, "publish"), \
                patch.object(build, "write_json"), \
                patch.object(build_deepthink, "stage", side_effect=StopBeforeStaging) as workflow:
            with self.assertRaises(StopBeforeStaging):
                build_deepthink_profile.stage(self.target, build_profile="release")
            tools.assert_called_once_with(self.target, build_profile="release")
            reader.assert_called_once_with(self.target, build_profile="release")
            adapter.assert_called_once_with(self.target, build_profile="release")
            self.assertEqual(workflow.call_args.kwargs["build_profile"], "release")

    def test_only_release_cargo_invocations_receive_release_flag(self):
        for module in [build, build_deepthink]:
            for profile in ["debug", "release"]:
                with self.subTest(builder=module.__name__, profile=profile), \
                        patch.object(sys, "argv", ["builder", "--target-dir", str(self.target),
                                                   "--build-profile", profile]), \
                        patch.object(module.subprocess, "run", side_effect=StopBeforeStaging) as cargo, \
                        patch.object(build.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)):
                    with self.assertRaises(StopBeforeStaging):
                        module.main()
                    command = cargo.call_args.args[0]
                    self.assertEqual(command.count("--release"), int(profile == "release"))
                    self.assertIn("wasm32-wasip2", command)

    def test_stage_only_cli_propagates_profile_and_all_clis_reject_invalid_choice(self):
        for module in [build_harness_tools, build_deepthink_profile]:
            with self.subTest(builder=module.__name__), \
                    patch.object(sys, "argv", ["builder", "--target-dir", str(self.target),
                                               "--build-profile", "release"]), \
                    patch.object(module, "stage", return_value=[]) as stage:
                module.main()
                stage.assert_called_once_with(self.target, build_profile="release")
        for module in [build, build_deepthink, build_harness_tools, build_deepthink_profile,
                       build_deepthink_input]:
            with self.subTest(invalid=module.__name__), \
                    patch.object(sys, "argv", ["builder", "--target-dir", str(self.target),
                                               "--build-profile", "production"]), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                module.main()
            self.assertEqual(error.exception.code, 2)

    def test_input_adapter_has_only_nominal_provided_interface_and_flow_views(self):
        manifest = build_deepthink_input.stage(self.target)
        self.assertEqual(manifest["shapes"], [])
        self.assertEqual(manifest["stores"], [])
        self.assertEqual(manifest["flows"], [])
        self.assertEqual(manifest["ports"]["provides"], [
            build.port("local.deepthink.continuation-input"),
        ])
        self.assertEqual(set(manifest["permissions"]["ports"]), {
            build_deepthink_input.INPUT_VIEW, *build_deepthink_input.FLOW_VIEWS,
        })
        self.assertEqual(manifest["ports"]["consumes"], build_deepthink_input.consumed_ports())
        self.assertIn(build.port("local.deepthink.workflow"), manifest["ports"]["consumes"])
        self.assertNotIn(build_deepthink_input.WORKFLOW_TYPE_VIEW,
                         manifest["permissions"]["ports"])
        for key in ("secrets", "fs", "proc", "host_io"):
            self.assertEqual(manifest["permissions"][key], [])
        self.assertFalse(any(manifest["permissions"]["storage"].values()))
        self.assertEqual(manifest["permissions"]["network"]["origins"], [])

    def test_input_adapter_rejects_noncomponent_without_creating_output(self):
        artifact = build.component_artifact(self.target, self.names["input"])
        artifact.write_bytes(b"not a component")
        destination = self.root / "missing-input"
        with self.assertRaisesRegex(ValueError, "component header"):
            build_deepthink_input.stage(self.target, destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
