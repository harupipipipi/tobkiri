"""Exercise desktop route selection without invoking build or native tools."""

from __future__ import annotations

import contextlib
from html.parser import HTMLParser
import importlib.util
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "tobkiri_launcher/scripts"
TARGET = "aarch64-apple-darwin"
CREATE = (
    "cargo run --locked -p tobkiri-cli -- profile create "
    "--name NAME --definition FILE"
)
IMPORT = "cargo run --locked -p tobkiri-cli -- profile import FILE --name NAME"


class SetupDocument(HTMLParser):
    """Collect the visible setup copy and existing form boundaries."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.paragraphs: list[str] = []
        self.scripts: list[dict[str, str | None]] = []
        self.inputs: dict[str, dict[str, str | None]] = {}
        self._paragraph: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if fields.get("id") is not None:
            self.ids.append(str(fields["id"]))
        if tag == "p" and fields.get("id") in {"profile-setup", "profile-setup-cli"}:
            self._paragraph = []
        if tag == "script":
            self.scripts.append(fields)
        if tag == "input" and fields.get("id") is not None:
            self.inputs[str(fields["id"])] = fields

    def handle_data(self, data: str) -> None:
        if self._paragraph is not None:
            self._paragraph.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._paragraph is not None:
            self.paragraphs.append("".join(self._paragraph))
            self._paragraph = None


class DevelopmentCoreRouteTests(unittest.TestCase):
    """Check the real launcher functions with only their process boundary mocked."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        spec = importlib.util.spec_from_file_location(
            "development_core_route_subject", SCRIPTS / "start_development_launcher.py"
        )
        assert spec is not None and spec.loader is not None
        self.launcher = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            spec.loader.exec_module(self.launcher)
        target_patch = mock.patch.object(
            self.launcher, "host_target", return_value=TARGET
        )
        target_patch.start()
        self.addCleanup(target_patch.stop)
        environment_patch = mock.patch.dict(os.environ)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        os.environ.pop("CARGO_TARGET_DIR", None)
        self.commands: list[tuple[list[str], dict[str, object]]] = []
        command_patch = mock.patch.object(
            self.launcher, "run_command", side_effect=self.run_command
        )
        command_patch.start()
        self.addCleanup(command_patch.stop)

    def run_command(self, command, **kwargs):
        """Record the checked production boundary; execute no child process."""
        parts = [os.fspath(part) for part in command]
        self.commands.append((parts, kwargs))
        return subprocess.CompletedProcess(parts, 0)

    def app_executable(self, *, legacy: bool = False) -> Path:
        """Create only a fake build output used by the launcher's file check."""
        name = "Tobkiri Launcher Developer.app" if legacy else "Tobkiri Launcher.app"
        executable = (
            self.root
            / "tobkiri_launcher/src-tauri/target"
            / TARGET
            / "debug/bundle/macos"
            / name
            / "Contents/MacOS/tobkiri-launcher"
        )
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        return executable

    def legacy_checkout(self) -> Path:
        """Supply the legacy-only interpreter prerequisite explicitly."""
        python = self.root / ".venv/bin/python3"
        python.parent.mkdir(parents=True)
        python.touch()
        return self.app_executable(legacy=True)

    def invoke_main(self, argv: list[str]) -> tuple[int, str, str]:
        """Invoke real argument routing with an isolated checkout and output."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(self.launcher, "REPO_ROOT", self.root),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = self.launcher.main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_default_build_uses_only_core_and_needs_no_legacy_files(self) -> None:
        executable = self.app_executable()
        self.assertFalse((self.root / ".venv").exists())
        self.assertEqual(
            self.launcher.build_development_launcher(self.root), executable
        )
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(
            self.commands[0],
            (
                [
                    "npm", "exec", "--prefix", "frontend", "--", "tauri", "build",
                    "--debug", "--features", "core-host",
                    "--target", TARGET, "--config",
                    "src-tauri/tauri.core-host.macos.dev.conf.json",
                    "--bundles", "app", "--ci", "--", "--no-default-features",
                ],
                {"cwd": self.root / "tobkiri_launcher"},
            ),
        )
        app = executable.parents[2]
        self.assertEqual(
            self.commands[1],
            (
                [
                    "/usr/bin/codesign", "--verify", "--strict",
                    "--all-architectures", str(app),
                ],
                {},
            ),
        )
        self.assertFalse((self.root / ".venv").exists())

    def test_core_build_failure_never_falls_back_to_legacy(self) -> None:
        self.legacy_checkout()

        def fail(command, **kwargs):
            self.run_command(command, **kwargs)
            raise subprocess.CalledProcessError(1, command)

        self.launcher.run_command.side_effect = fail
        with self.assertRaises(subprocess.CalledProcessError):
            self.launcher.build_development_launcher(self.root)
        self.assertEqual(len(self.commands), 1)
        self.assertIn(
            "src-tauri/tauri.core-host.macos.dev.conf.json", self.commands[0][0]
        )

    def test_missing_core_output_does_not_select_existing_legacy_output(self) -> None:
        self.legacy_checkout()
        with self.assertRaisesRegex(
            RuntimeError, "Core development Launcher was not produced"
        ):
            self.launcher.build_development_launcher(self.root)
        self.assertEqual(len(self.commands), 1)

    def test_core_signature_failure_never_starts_legacy_verification(self) -> None:
        self.app_executable()
        self.legacy_checkout()

        def fail_verification(command, **kwargs):
            result = self.run_command(command, **kwargs)
            if "--verify" in command:
                raise subprocess.CalledProcessError(1, command)
            return result

        self.launcher.run_command.side_effect = fail_verification
        with self.assertRaises(subprocess.CalledProcessError):
            self.launcher.build_development_launcher(self.root)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.commands[-1][0][0], "/usr/bin/codesign")

    def test_default_build_only_verifies_core_without_running_it(self) -> None:
        self.app_executable()
        code, stdout, stderr = self.invoke_main(["--build-only"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(self.commands), 2)
        self.assertIn("Verified Tobkiri Core development desktop", stdout)
        self.assertIn(CREATE, stdout)
        self.assertIn(IMPORT, stdout)
        self.assertIn("ordinary verification and approval", stdout)

    def test_default_main_launches_only_the_verified_core_executable(self) -> None:
        executable = self.app_executable()
        code, _, stderr = self.invoke_main([])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(self.commands), 3)
        self.assertEqual(self.commands[-1], ([str(executable)], {"cwd": self.root}))

    def test_failed_default_main_does_not_run_or_fallback(self) -> None:
        self.legacy_checkout()
        code, _, stderr = self.invoke_main([])
        self.assertEqual(code, 1)
        self.assertIn("Core development Launcher was not produced", stderr)
        self.assertEqual(len(self.commands), 1)

    def test_explicit_legacy_preserves_the_complete_verification_chain(self) -> None:
        executable = self.legacy_checkout()
        self.assertEqual(
            self.launcher.build_development_launcher(self.root, legacy=True), executable
        )
        commands = [parts for parts, _ in self.commands]
        self.assertEqual(len(commands), 8)
        self.assertEqual(commands[0][-2:], ["--", "--no-default-features"])
        self.assertNotIn(
            "--no-default-features", commands[0][commands[0].index("tauri") + 1:-2]
        )
        self.assertNotIn("--features", commands[0])
        self.assertIn("src-tauri/tauri.macos.dev.conf.json", commands[0])
        self.assertTrue(commands[1][1].endswith("build_packvm_vz_helper.sh"))
        self.assertIn("--entitlements", commands[2])
        self.assertIn("--verify", commands[3])
        self.assertIn("write-packvm-bundle-manifest", commands[4])
        self.assertEqual(commands[4][-2:], ["--expected-signing-mode", "ad-hoc"])
        self.assertIn("--sign", commands[5])
        self.assertIn("--verify", commands[6])
        self.assertIn("verify-packvm-bundle", commands[7])

    def test_explicit_legacy_main_launches_only_after_legacy_verification(self) -> None:
        executable = self.legacy_checkout()
        code, stdout, stderr = self.invoke_main(["--legacy"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(self.commands), 9)
        self.assertEqual(self.commands[-1], ([str(executable)], {"cwd": self.root}))
        self.assertIn("Verified Tobkiri legacy development desktop", stdout)
        self.assertNotIn(CREATE, stdout)

    def test_explicit_legacy_still_requires_its_interpreter(self) -> None:
        self.app_executable(legacy=True)
        with self.assertRaisesRegex(RuntimeError, "Create .venv"):
            self.launcher.build_development_launcher(self.root, legacy=True)
        self.assertEqual(self.commands, [])

    def test_legacy_verification_stops_before_attestation(self) -> None:
        self.legacy_checkout()

        def fail_verification(command, **kwargs):
            result = self.run_command(command, **kwargs)
            if "--verify" in command:
                raise subprocess.CalledProcessError(1, command)
            return result

        self.launcher.run_command.side_effect = fail_verification
        with self.assertRaises(subprocess.CalledProcessError):
            self.launcher.build_development_launcher(self.root, legacy=True)
        self.assertEqual(len(self.commands), 4)
        self.assertFalse(
            any(
                "write-packvm-bundle-manifest" in command
                for command, _ in self.commands
            )
        )

    def test_routes_reject_unbound_target_directory(self) -> None:
        os.environ["CARGO_TARGET_DIR"] = str(self.root / "external")
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                with self.assertRaisesRegex(RuntimeError, "default Cargo target"):
                    self.launcher.build_development_launcher(self.root, legacy=legacy)
        self.assertEqual(self.commands, [])

    def test_each_route_preserves_the_development_host_target_boundary(self) -> None:
        self.launcher.host_target.return_value = "x86_64-unknown-linux-gnu"
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                with self.assertRaisesRegex(RuntimeError, "Apple Silicon macOS"):
                    self.launcher.build_development_launcher(self.root, legacy=legacy)
        self.assertEqual(self.commands, [])

    def test_dev_config_changes_only_the_core_bundle_development_settings(self) -> None:
        tauri = ROOT / "tobkiri_launcher/src-tauri"
        base = json.loads((tauri / "tauri.core-host.conf.json").read_text())
        development = json.loads(
            (tauri / "tauri.core-host.macos.dev.conf.json").read_text()
        )
        self.assertEqual(set(development), set(base))
        for key in set(base) - {"bundle"}:
            self.assertEqual(development[key], base[key], key)
        core_macos = {
            "hardenedRuntime": True,
            "entitlements": "Entitlements.core-host.plist",
        }
        self.assertEqual(
            base["bundle"],
            {"active": False, "resources": None, "macOS": core_macos},
        )
        self.assertEqual(
            development["bundle"],
            {
                "active": True,
                "resources": None,
                "macOS": {**core_macos, "signingIdentity": "-"},
            },
        )

    def test_core_macos_signing_allows_only_the_pinned_engine_memory_path(self) -> None:
        tauri = ROOT / "tobkiri_launcher/src-tauri"
        for name in ("tauri.core-host.conf.json", "tauri.core-host.macos.dev.conf.json"):
            with self.subTest(config=name):
                config = json.loads((tauri / name).read_text())
                macos = config["bundle"]["macOS"]
                self.assertIs(macos["hardenedRuntime"], True)
                self.assertEqual(macos["entitlements"], "Entitlements.core-host.plist")
                # Parse the actual plist, not a comment or string containing a key.
                with (tauri / macos["entitlements"]).open("rb") as reader:
                    entitlements = plistlib.load(reader)
                self.assertEqual(
                    entitlements,
                    {"com.apple.security.cs.allow-unsigned-executable-memory": True},
                )
                self.assertIs(
                    entitlements["com.apple.security.cs.allow-unsigned-executable-memory"],
                    True,
                )

    def test_cargo_defaults_core_with_explicit_feature_definition(self) -> None:
        cargo = tomllib.loads(
            (ROOT / "tobkiri_launcher/src-tauri/Cargo.toml").read_text()
        )
        self.assertEqual(cargo["features"]["default"], ["core-host"])
        self.assertEqual(
            cargo["features"]["core-host"],
            [
                "dep:tobkiri-launcher-core-host", "dep:async-trait",
                "dep:tokio", "dep:ulid",
            ],
        )

    def test_static_setup_preserves_profile_and_terminal_controls(self) -> None:
        document = SetupDocument()
        document.feed(
            (ROOT / "tobkiri_launcher/frontend/core-host/index.html").read_text()
        )
        self.assertEqual(
            document.ids,
            [
                "profile-setup", "profile-setup-cli", "profile-form", "profile-id",
                "stop", "status", "pack-ui", "terminal-output", "terminal-form",
                "terminal-prompt", "terminal-input",
            ],
        )
        self.assertEqual(len(document.paragraphs), 2)
        self.assertIn(
            "does not create a Profile or configure a provider", document.paragraphs[0]
        )
        self.assertIn(CREATE, document.paragraphs[1])
        self.assertIn(IMPORT, document.paragraphs[1])
        self.assertIn("ordinary verification and approval", document.paragraphs[1])
        self.assertEqual(document.inputs["profile-id"]["maxlength"], "128")
        self.assertIn("required", document.inputs["profile-id"])
        self.assertEqual(document.inputs["terminal-input"]["maxlength"], "65536")
        self.assertEqual(
            document.scripts, [{"type": "module", "src": "/src/core-host/main.ts"}]
        )


if __name__ == "__main__":
    unittest.main()
