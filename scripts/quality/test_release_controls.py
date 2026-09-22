from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
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
EXPECTED_INVENTORY_TARGETS = {
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "x86_64-unknown-linux-gnu",
}


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
        assert set(INVENTORY.TARGETS) == EXPECTED_INVENTORY_TARGETS
        assert len(INVENTORY.TARGETS) == 4
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


def test_inventory_binds_an_explicit_workflow_target_set() -> None:
    revision = "c" * 40
    target = "aarch64-apple-darwin"
    with TemporaryDirectory(prefix="tobkiri-release-matrix-") as temp:
        root = Path(temp)
        _create_target_upload(root, target, revision)
        output = root / "release-inventory.json"
        assets = root / "release-assets"
        inventory = INVENTORY.create_inventory(
            root / "uploaded",
            output,
            assets,
            revision,
            "v1.2.3",
            [target],
        )

        assert [item["target"] for item in inventory["targets"]] == [target]
        INVENTORY.verify_inventory(
            output,
            assets,
            revision,
            "v1.2.3",
            required_targets=[target],
        )
        with pytest.raises(INVENTORY.InventoryError, match="missing or unexpected"):
            INVENTORY.verify_inventory(output, assets, revision, "v1.2.3")


@pytest.mark.parametrize(
    ("required_targets", "message"),
    [
        ([], "at least one target"),
        (["aarch64-apple-darwin", "aarch64-apple-darwin"], "duplicated"),
        (["unknown-release-target"], "unsupported required"),
        ([None], "malformed"),
    ],
)
def test_inventory_rejects_empty_duplicate_unknown_required_targets(
    required_targets: list[object], message: str
) -> None:
    revision = "d" * 40
    target = "aarch64-apple-darwin"
    with TemporaryDirectory(prefix="tobkiri-release-target-contract-") as temp:
        root = Path(temp)
        _create_target_upload(root, target, revision)
        output = root / "release-inventory.json"
        assets = root / "release-assets"
        with pytest.raises(INVENTORY.InventoryError, match=message):
            INVENTORY.create_inventory(
                root / "uploaded",
                output,
                assets,
                revision,
                "v1.2.3",
                required_targets,
            )

        INVENTORY.create_inventory(
            root / "uploaded",
            output,
            assets,
            revision,
            "v1.2.3",
            [target],
        )
        with pytest.raises(INVENTORY.InventoryError, match=message):
            INVENTORY.verify_inventory(
                output,
                assets,
                revision,
                "v1.2.3",
                required_targets=required_targets,
            )


def test_release_workflow_inventory_target_matches_real_build_matrix() -> None:
    workflow_path = ROOT / ".github/workflows/release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    matrix = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    matrix_targets = [row["target"] for row in matrix]
    required_targets = json.loads(
        workflow["env"]["TOBKIRI_REQUIRED_RELEASE_TARGETS"]
    )

    assert isinstance(required_targets, list)
    assert required_targets
    assert len(required_targets) == len(set(required_targets))
    assert len(matrix_targets) == len(set(matrix_targets))
    assert set(matrix_targets) == set(required_targets)
    assert all(target in INVENTORY.TARGETS for target in required_targets)

    gather_job = workflow["jobs"]["gather"]
    inventory_runs = [
        step["run"]
        for step in gather_job["steps"]
        if isinstance(step, dict)
        and "scripts/release_inventory.py" in step.get("run", "")
    ]
    assert len(inventory_runs) == 3
    for run in inventory_runs:
        assert "json.loads(" in run
        assert 'os.environ["TOBKIRI_REQUIRED_RELEASE_TARGETS"]' in run
        assert "for required_target in required_targets:" in run
        assert 'command.extend(["--required-target", required_target])' in run
        assert "subprocess.run(command, check=True)" in run
        assert run.count('"--required-target"') == 1
    assert "TOBKIRI_REQUIRED_RELEASE_TARGET:" not in workflow_text


def test_release_workflow_builds_a_single_unsigned_ad_hoc_macos_target() -> None:
    """The OSS release path has no publisher credential or notary dependency."""
    workflow_path = ROOT / ".github/workflows/release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    build = workflow["jobs"]["build"]
    assert build["strategy"]["matrix"]["include"] == [
        {
            "os": "macos-latest",
            "target": "aarch64-apple-darwin",
            "shell_bundles": "app",
            "signing_args": "--no-sign",
            "presentation_variant": "shell.tauri.default.macos-arm64",
            "presentation_platform": "macos",
            "presentation_architecture": "arm64",
        }
    ]
    steps = {
        step["name"]: step
        for step in build["steps"]
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    }
    for name in (
        "Build unsigned Tauri Shell artifact",
        "Build unsigned macOS application",
    ):
        assert "${{ matrix.signing_args }}" in steps[name]["run"]
    signing = steps[
        "Stage and ad-hoc sign macOS PackVM VZ helper and application"
    ]["run"]
    assert signing.count("/usr/bin/codesign --force --sign - --timestamp=none") == 2
    assert "dev.rumiai.app" in signing
    assert "dev.tobkiri.launcher.packvm-vz-helper" in signing
    assert "--expected-signing-mode ad-hoc" in signing
    assert "--ad-hoc" in steps["Build macOS DMG installer"]["run"]

    for forbidden in (
        "APPLE_CERTIFICATE",
        "APPLE_SIGNING_IDENTITY",
        "APPLE_TEAM_ID",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "Developer ID Application:",
        "sign-artifacts",
        "--signing-identity",
        "TOBKIRI_MACOS_ARTIFACT_POLICY",
        "ci-e2e",
    ):
        assert forbidden not in workflow_text
    assert not any(
        "notar" in name.lower() or "staple" in name.lower()
        for name in steps
    )

    upload = next(
        step
        for step in workflow["jobs"]["gather"]["steps"]
        if step.get("name") == "Upload one reviewable draft release"
    )
    assert upload["with"]["draft"] is True
    assert upload["with"]["generate_release_notes"] is True
    assert "unsigned/ad-hoc" in upload["with"]["body"]


def test_release_workflow_passes_json_target_set_to_inventory_commands() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    required_targets = json.loads(
        workflow["env"]["TOBKIRI_REQUIRED_RELEASE_TARGETS"]
    )
    inventory_steps = [
        step
        for step in workflow["jobs"]["gather"]["steps"]
        if isinstance(step, dict)
        and "scripts/release_inventory.py" in step.get("run", "")
    ]
    expected_arguments = [
        argument
        for target in required_targets
        for argument in ("--required-target", target)
    ]
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool) -> None:
        assert check is True
        calls.append(command)
        if command[2] == "create":
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"{}\n")

    with TemporaryDirectory(prefix="tobkiri-release-workflow-commands-") as temp:
        root = Path(temp)
        environment = {
            "GATHER_ROOT": str(root / "gather"),
            "GITHUB_OUTPUT": str(root / "github-output"),
            "GITHUB_REF_NAME": "v1.2.3",
            "INVENTORY_SHA256": "sha256:" + "0" * 64,
            "SOURCE_REVISION": "1" * 40,
            "TOBKIRI_REQUIRED_RELEASE_TARGETS": json.dumps(required_targets),
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(subprocess, "run", side_effect=fake_run),
        ):
            for step in inventory_steps:
                exec(step["run"], {})

    assert [command[2] for command in calls] == ["create", "verify", "verify"]
    for command in calls:
        assert command[-len(expected_arguments) :] == expected_arguments


@pytest.mark.parametrize(
    "required_targets",
    [[], ["aarch64-apple-darwin", "aarch64-apple-darwin"]],
)
def test_release_workflow_rejects_empty_or_duplicate_target_json(
    required_targets: list[str],
) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    inventory_steps = [
        step
        for step in workflow["jobs"]["gather"]["steps"]
        if isinstance(step, dict)
        and "scripts/release_inventory.py" in step.get("run", "")
    ]
    with TemporaryDirectory(prefix="tobkiri-release-workflow-invalid-") as temp:
        root = Path(temp)
        environment = {
            "GATHER_ROOT": str(root / "gather"),
            "GITHUB_OUTPUT": str(root / "github-output"),
            "GITHUB_REF_NAME": "v1.2.3",
            "INVENTORY_SHA256": "sha256:" + "0" * 64,
            "SOURCE_REVISION": "1" * 40,
            "TOBKIRI_REQUIRED_RELEASE_TARGETS": json.dumps(required_targets),
        }
        with patch.dict(os.environ, environment):
            for step in inventory_steps:
                with patch.object(subprocess, "run") as run:
                    with pytest.raises(SystemExit, match="non-empty"):
                        exec(step["run"], {})
                    run.assert_not_called()


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


def test_inventory_rejects_symlinked_target_manifest() -> None:
    revision = "e" * 40
    with TemporaryDirectory(prefix="tobkiri-release-manifest-link-") as temp:
        root = Path(temp)
        for target in INVENTORY.TARGETS:
            _create_target_upload(root, target, revision)
        manifest_path = root / "uploaded/aarch64-apple-darwin/release-target.json"
        replacement = root / "uploaded/x86_64-apple-darwin/release-target.json"
        try:
            manifest_path.unlink()
            manifest_path.symlink_to(replacement)
        except OSError as error:
            pytest.skip(f"symlink fixtures are unavailable: {error}")
        with pytest.raises(INVENTORY.InventoryError, match="regular file"):
            INVENTORY.create_inventory(
                root / "uploaded",
                root / "release-inventory.json",
                root / "release-assets",
                revision,
                "v1.2.3",
            )


def test_inventory_rejects_unexpected_artifact_target() -> None:
    revision = "f" * 40
    target = "aarch64-apple-darwin"
    with TemporaryDirectory(prefix="tobkiri-release-artifact-target-") as temp:
        root = Path(temp)
        _create_target_upload(root, target, revision)
        output = root / "release-inventory.json"
        assets = root / "release-assets"
        INVENTORY.create_inventory(
            root / "uploaded",
            output,
            assets,
            revision,
            "v1.2.3",
            [target],
        )
        inventory = json.loads(output.read_text(encoding="utf-8"))
        inventory["artifacts"][0]["target"] = "x86_64-apple-darwin"
        output.write_text(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(INVENTORY.InventoryError, match="missing or unexpected"):
            INVENTORY.verify_inventory(
                output,
                assets,
                revision,
                "v1.2.3",
                required_targets=[target],
            )


def test_inventory_rejects_unknown_target_manifest() -> None:
    revision = "0" * 40
    target = "aarch64-apple-darwin"
    with TemporaryDirectory(prefix="tobkiri-release-unknown-target-") as temp:
        root = Path(temp)
        upload_root = _create_target_upload(root, target, revision)
        manifest_path = upload_root / INVENTORY.TARGET_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["target"] = "unknown-release-target"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(INVENTORY.InventoryError, match="unsupported"):
            INVENTORY.create_inventory(
                root / "uploaded",
                root / "release-inventory.json",
                root / "release-assets",
                revision,
                "v1.2.3",
                [target],
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
