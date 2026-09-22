from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load("release_gate")
INVENTORY = _load("release_inventory")


def _version_repo(root: Path, version: str) -> Path:
    launcher = root / "tobkiri_launcher"
    tauri = launcher / "src-tauri"
    frontend = launcher / "frontend"
    tauri.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (tauri / "Cargo.toml").write_text(
        f'[package]\nname = "tobkiri-launcher"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tauri / "Cargo.lock").write_text(
        f'version = 4\n\n[[package]]\nname = "tobkiri-launcher"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tauri / "tauri.conf.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    (frontend / "package.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "version": version,
                "packages": {"": {"version": version}},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_exact_tag_and_stable_prerelease_policy() -> None:
    with TemporaryDirectory(prefix="tobkiri-release-version-") as temp:
        root = _version_repo(Path(temp), "1.2.3")
        assert GATE.validate_release_tag(root, "v1.2.3")["channel"] == "stable"
        with pytest.raises(GATE.ReleaseGateError, match="exactly v1.2.3"):
            GATE.validate_release_tag(root, "v1.2.4")

        prerelease_root = _version_repo(Path(temp) / "prerelease", "1.2.3-beta.4")
        assert (
            GATE.validate_release_tag(prerelease_root, "v1.2.3-beta.4")["channel"]
            == "beta"
        )
        alpha_root = _version_repo(Path(temp) / "alpha", "0.2.3-alpha.1")
        assert (
            GATE.validate_release_tag(alpha_root, "v0.2.3-alpha.1")["channel"]
            == "alpha"
        )
        rc_root = _version_repo(Path(temp) / "rc", "1.2.3-rc.1")
        assert GATE.validate_release_tag(rc_root, "v1.2.3-rc.1")["channel"] == "rc"
        with pytest.raises(GATE.ReleaseGateError, match="canonical version"):
            _version_repo(Path(temp) / "invalid", "1.2.3-dev.4")
            GATE.canonical_version(Path(temp) / "invalid")


def test_all_version_authorities_must_match() -> None:
    with TemporaryDirectory(prefix="tobkiri-release-version-mismatch-") as temp:
        root = _version_repo(Path(temp), "1.2.3")
        tauri = root / "tobkiri_launcher/src-tauri/tauri.conf.json"
        tauri.write_text(json.dumps({"version": "1.2.4"}), encoding="utf-8")
        with pytest.raises(GATE.ReleaseGateError, match="not synchronized"):
            GATE.canonical_version(root)


def _production_environment(platform: str) -> dict[str, str]:
    if platform == "macos":
        return {
            "APPLE_CERTIFICATE_BASE64": base64.b64encode(b"certificate").decode(),
            "APPLE_CERTIFICATE_PASSWORD": "certificate-password",
            "APPLE_SIGNING_IDENTITY": "Developer ID Application: Tobkiri",
            "APPLE_ID": "release@example.invalid",
            "APPLE_PASSWORD": "app-specific-password",
            "APPLE_TEAM_ID": "TEAM123456",
        }
    return {
        "WINDOWS_CERTIFICATE_BASE64": base64.b64encode(b"certificate").decode(),
        "WINDOWS_CERTIFICATE_PASSWORD": "certificate-password",
        "WINDOWS_TIMESTAMP_URL": "https://timestamp.example.invalid",
    }


def test_signing_policy_requires_explicit_production_credentials_without_logging_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert GATE.validate_signing_policy("local-dev", "macos")["production"] is False
    with pytest.raises(GATE.ReleaseGateError, match="missing"):
        GATE.validate_signing_policy("production", "macos", {})
    with pytest.raises(GATE.ReleaseGateError, match="Developer ID"):
        environment = _production_environment("macos")
        environment["APPLE_SIGNING_IDENTITY"] = "-"
        GATE.validate_signing_policy("production", "macos", environment)
    with pytest.raises(GATE.ReleaseGateError, match="override"):
        environment = _production_environment("windows")
        environment["TOBKIRI_ALLOW_UNSIGNED_RELEASE"] = "true"
        GATE.validate_signing_policy("production", "windows", environment)

    result = GATE.main(["signing", "--mode", "production", "--platform", "windows"])
    assert result == 1
    assert "certificate-password" not in capsys.readouterr().err


def test_windows_signing_commands_are_mocked_without_running_signtool(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = _production_environment("windows")
    with TemporaryDirectory(prefix="tobkiri-signing-mock-") as temp:
        artifact = Path(temp) / "Tobkiri-setup.exe"
        artifact.write_bytes(b"unsigned fixture")
        with (
            patch.object(GATE, "_find_signtool", return_value="signtool.exe"),
            patch.object(GATE, "_run") as run,
        ):
            GATE.sign_windows_artifacts([artifact], environment)
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[0][1] == "sign"
    assert commands[1][1] == "verify"
    assert environment["WINDOWS_CERTIFICATE_PASSWORD"] in commands[0]
    assert capsys.readouterr().out == ""


def test_macos_app_signing_archives_for_notary_and_staples_the_app() -> None:
    environment = _production_environment("macos")
    with TemporaryDirectory(prefix="tobkiri-macos-app-mock-") as temp:
        app_bundle = Path(temp) / "Tobkiri.app"
        (app_bundle / "Contents").mkdir(parents=True)
        (app_bundle / "Contents/Info.plist").write_bytes(b"fixture")
        with (
            patch.object(GATE, "validate_signing_policy"),
            patch.object(GATE, "_run") as run,
        ):
            GATE.sign_macos_artifacts(
                ROOT,
                app_bundle,
                [app_bundle],
                environment,
            )

    commands = [call.args[0] for call in run.call_args_list]
    archive_commands = [command for command in commands if command[0] == "ditto"]
    assert len(archive_commands) == 1
    assert archive_commands[0][1:4] == ["-c", "-k", "--keepParent"]
    submission_commands = [
        command
        for command in commands
        if command[:3] == ["xcrun", "notarytool", "submit"]
    ]
    assert len(submission_commands) == 1
    assert submission_commands[0][3].endswith(".zip")
    assert ["xcrun", "stapler", "staple", "-v", str(app_bundle)] in commands
    assert any(
        command[:2] == ["spctl", "--assess"] and command[-1] == str(app_bundle)
        for command in commands
    )


def _create_target_upload(root: Path, target: str, revision: str) -> Path:
    platform, architecture, suffixes = INVENTORY.TARGETS[target]
    source = root / "source" / target
    source.mkdir(parents=True)
    for suffix in suffixes:
        (source / f"Tobkiri-{target}{suffix}").write_bytes(
            f"fixture:{target}:{suffix}".encode()
        )
    output = root / "uploaded" / target
    INVENTORY.collect_target(
        output,
        [source],
        revision,
        target,
        platform,
        architecture,
    )
    return output


def test_inventory_binds_all_targets_once_and_rejects_tamper_missing_duplicate() -> (
    None
):
    revision = "a" * 40
    with TemporaryDirectory(prefix="tobkiri-release-inventory-") as temp:
        root = Path(temp)
        for target in INVENTORY.TARGETS:
            _create_target_upload(root, target, revision)
        output = root / "release-inventory.json"
        assets = root / "release-assets"
        inventory = INVENTORY.create_inventory(
            root / "uploaded", output, assets, revision, "v1.2.3"
        )
        assert len(inventory["artifacts"]) == 5
        assert {item["source_revision"] for item in inventory["artifacts"]} == {
            revision
        }
        inventory_digest = f"sha256:{hashlib.sha256(output.read_bytes()).hexdigest()}"
        INVENTORY.verify_inventory(output, assets, revision, "v1.2.3", inventory_digest)

        original_inventory = output.read_bytes()
        wrong_metadata = json.loads(original_inventory)
        wrong_metadata["artifacts"][0]["size"] += 1
        output.write_text(
            json.dumps(wrong_metadata, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(INVENTORY.InventoryError, match="digest or size"):
            INVENTORY.verify_inventory(output, assets, revision, "v1.2.3")
        wrong_metadata["artifacts"][0]["size"] -= 1
        wrong_metadata["artifacts"][0]["sha256"] = "sha256:" + "0" * 64
        output.write_text(
            json.dumps(wrong_metadata, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(INVENTORY.InventoryError, match="digest or size"):
            INVENTORY.verify_inventory(output, assets, revision, "v1.2.3")
        output.write_bytes(original_inventory)

        mutated_inventory = json.loads(original_inventory)
        mutated_inventory["tag"] = "v9.9.9"
        output.write_text(
            json.dumps(mutated_inventory, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(INVENTORY.InventoryError, match="inventory digest"):
            INVENTORY.verify_inventory(
                output, assets, revision, "v1.2.3", inventory_digest
            )
        output.write_bytes(original_inventory)

        first_asset = next(assets.iterdir())
        first_asset.write_bytes(first_asset.read_bytes() + b"tamper")
        with pytest.raises(INVENTORY.InventoryError, match="digest or size"):
            INVENTORY.verify_inventory(
                output, assets, revision, "v1.2.3", inventory_digest
            )

        first_asset.unlink()
        with pytest.raises(INVENTORY.InventoryError, match="missing"):
            INVENTORY.verify_inventory(
                output, assets, revision, "v1.2.3", inventory_digest
            )

        duplicate_target = root / "uploaded" / "duplicate-target"
        shutil.copytree(root / "uploaded" / "aarch64-apple-darwin", duplicate_target)
        with pytest.raises(INVENTORY.InventoryError, match="exactly 4"):
            INVENTORY.create_inventory(
                root / "uploaded", output, assets, revision, "v1.2.3"
            )


def test_inventory_rejects_symlink_and_path_escape_fixtures() -> None:
    revision = "b" * 40
    with TemporaryDirectory(prefix="tobkiri-release-path-guards-") as temp:
        root = Path(temp)
        source = root / "source"
        source.mkdir()
        outside = root / "outside.dmg"
        outside.write_bytes(b"outside")
        symlink_asset = source / "Tobkiri-aarch64.dmg"
        try:
            symlink_asset.symlink_to(outside)
        except OSError as error:
            pytest.skip(f"symlink fixtures are unavailable: {error}")
        with pytest.raises(INVENTORY.InventoryError, match="symlink"):
            INVENTORY.collect_target(
                root / "symlink-upload",
                [source],
                revision,
                "aarch64-apple-darwin",
                "macos",
                "arm64",
            )

        for target in INVENTORY.TARGETS:
            _create_target_upload(root, target, revision)
        manifest_path = root / "uploaded/aarch64-apple-darwin/release-target.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"][0]["path"] = "../outside.dmg"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(INVENTORY.InventoryError, match="safe relative"):
            INVENTORY.create_inventory(
                root / "uploaded",
                root / "release-inventory.json",
                root / "release-assets",
                revision,
                "v1.2.3",
            )


def test_release_workflow_has_one_gather_attestation_and_no_matrix_draft_upload() -> (
    None
):
    workflow_path = ROOT / ".github/workflows/release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    permissions = workflow["permissions"]
    assert permissions == {"contents": "read"}
    gather_job = workflow["jobs"]["gather"]
    assert gather_job["permissions"] == {
        "actions": "read",
        "attestations": "write",
        "contents": "write",
        "id-token": "write",
    }
    gather_block = re.search(
        r"(?ms)^  gather:\n.*?(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        workflow_text,
    )
    assert gather_block is not None
    pinned_actions = {
        "actions/checkout": "v4.2.2",
        "actions/setup-python": "v5.6.0",
        "actions/download-artifact": "v4.3.0",
        "actions/attest-build-provenance": "v2.2.2",
        "softprops/action-gh-release": "v2.3.2",
    }
    gather_uses = [
        step["uses"]
        for step in gather_job["steps"]
        if isinstance(step, dict) and "uses" in step
    ]
    assert len(gather_uses) == len(pinned_actions)
    for uses in gather_uses:
        action, sha = uses.rsplit("@", 1)
        assert action in pinned_actions
        assert re.fullmatch(r"[0-9a-f]{40}", sha)
        version = pinned_actions[action]
        assert re.search(
            rf"(?m)^\s+uses:\s+{re.escape(action)}@{sha}\s+#\s+{re.escape(version)}\s*$",
            gather_block.group(0),
        )
    assert "github.sha" not in workflow_text
    assert workflow_text.count("fetch-depth: 0") == 2
    assert workflow_text.count('"rev-parse", "--verify", "HEAD"') == 2
    assert '[*git_arguments, "rev-parse", "--verify", "HEAD"]' in workflow_text
    assert '["git", "rev-parse", "--verify", "HEAD"]' in workflow_text
    assert "does not match the release tag" in workflow_text
    assert "scripts/release_gate.py" in workflow_text
    assert "scripts/release_inventory.py" in workflow_text
    assert "id-token: write" in workflow_text
    assert "attestations: write" in workflow_text
    assert workflow_text.index(
        "Attest the single release inventory subject"
    ) < workflow_text.index("Upload one reviewable draft release")
    assert workflow_text.index(
        "Verify final release assets after attestation"
    ) < workflow_text.index("Upload one reviewable draft release")
    assert workflow_text.index(
        "Create one SHA-256 release inventory"
    ) < workflow_text.index("Attest the single release inventory subject")
    assert "--tag" in workflow_text and "GITHUB_REF_NAME" in workflow_text


def test_build_and_sign_requires_mode_and_has_shell_syntax() -> None:
    script = ROOT / "scripts/build-and-sign.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    missing_mode = subprocess.run(
        ["bash", str(script), "--bundles", "app"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_mode.returncode == 64
    assert "requires explicit --mode" in missing_mode.stderr
    text = script.read_text(encoding="utf-8")
    assert "--mode production or --mode local-dev" in text
    assert "scripts/release_gate.py" in text
    assert "LOCAL-DEV ONLY" in text
    assert "sign-artifacts" in text
    assert 'echo "$APPLE_' not in text
