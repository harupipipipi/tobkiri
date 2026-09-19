#!/usr/bin/env python3
"""Fail-closed release version and platform-signing policy gates."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Mapping, Sequence


VERSION_PATTERN = re.compile(
    r"^(?P<core>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"(?:-(?P<channel>alpha|beta|rc)\.(?P<serial>0|[1-9]\d*))?$"
)
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SIGNING_MODES = {"production", "local-dev"}
PLATFORMS = {"macos", "windows", "linux"}
MACOS_SIGNING_ENV = (
    "APPLE_CERTIFICATE_BASE64",
    "APPLE_CERTIFICATE_PASSWORD",
    "APPLE_SIGNING_IDENTITY",
    "APPLE_ID",
    "APPLE_PASSWORD",
    "APPLE_TEAM_ID",
)
WINDOWS_SIGNING_ENV = (
    "WINDOWS_CERTIFICATE_BASE64",
    "WINDOWS_CERTIFICATE_PASSWORD",
    "WINDOWS_TIMESTAMP_URL",
)


class ReleaseGateError(RuntimeError):
    """Raised when a release safety gate cannot be satisfied."""


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseGateError(f"failed to read release metadata: {path}") from error


def _read_toml(path: Path) -> dict[str, object]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseGateError(f"failed to read release metadata: {path}") from error
    if not isinstance(value, dict):
        raise ReleaseGateError(f"release metadata is not an object: {path}")
    return value


def _text_field(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseGateError(f"release metadata field is missing: {label}")
    return value


def _validate_version(version: str) -> tuple[str, str]:
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ReleaseGateError(
            "canonical version must be stable X.Y.Z or prerelease "
            "X.Y.Z-(alpha|beta|rc).N"
        )
    channel = match.group("channel") or "stable"
    return version, channel


def canonical_version(repo_root: Path) -> tuple[str, str]:
    """Read and compare every application release-version authority."""
    launcher_root = repo_root / "tobkiri_launcher"
    cargo_manifest = _read_toml(launcher_root / "src-tauri" / "Cargo.toml")
    cargo_package = cargo_manifest.get("package")
    if not isinstance(cargo_package, dict):
        raise ReleaseGateError("Cargo.toml has no package table")
    cargo_version = _text_field(
        cargo_package.get("version"), "Cargo.toml package.version"
    )

    cargo_lock = _read_toml(launcher_root / "src-tauri" / "Cargo.lock")
    cargo_packages = cargo_lock.get("package")
    if not isinstance(cargo_packages, list):
        raise ReleaseGateError("Cargo.lock has no package list")
    locked_launcher_versions = [
        package.get("version")
        for package in cargo_packages
        if isinstance(package, dict) and package.get("name") == "tobkiri-launcher"
    ]
    if len(locked_launcher_versions) != 1:
        raise ReleaseGateError(
            "Cargo.lock must contain exactly one Tobkiri Launcher package"
        )
    cargo_lock_version = _text_field(
        locked_launcher_versions[0], "Cargo.lock tobkiri-launcher.version"
    )

    tauri = _read_json(launcher_root / "src-tauri" / "tauri.conf.json")
    if not isinstance(tauri, dict):
        raise ReleaseGateError("Tauri configuration is not an object")
    tauri_version = _text_field(tauri.get("version"), "tauri.conf.json version")

    frontend = _read_json(launcher_root / "frontend" / "package.json")
    if not isinstance(frontend, dict):
        raise ReleaseGateError("frontend package metadata is not an object")
    frontend_version = _text_field(
        frontend.get("version"), "frontend package.json version"
    )

    frontend_lock = _read_json(launcher_root / "frontend" / "package-lock.json")
    if not isinstance(frontend_lock, dict):
        raise ReleaseGateError("frontend lock metadata is not an object")
    lock_packages = frontend_lock.get("packages")
    root_lock_package = (
        lock_packages.get("") if isinstance(lock_packages, dict) else None
    )
    if not isinstance(root_lock_package, dict):
        raise ReleaseGateError("frontend package-lock.json has no root package")
    frontend_lock_versions = (
        _text_field(frontend_lock.get("version"), "frontend package-lock.json version"),
        _text_field(root_lock_package.get("version"), "frontend lock root version"),
    )

    versions = {
        "Cargo.toml": cargo_version,
        "Cargo.lock": cargo_lock_version,
        "tauri.conf.json": tauri_version,
        "frontend/package.json": frontend_version,
        "frontend/package-lock.json": frontend_lock_versions[0],
        "frontend/package-lock.json#packages.": frontend_lock_versions[1],
    }
    if len(set(versions.values())) != 1:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise ReleaseGateError(f"release versions are not synchronized: {details}")
    return _validate_version(cargo_version)


def validate_release_tag(repo_root: Path, tag: str) -> dict[str, str]:
    """Require an exact v-prefixed tag for the synchronized canonical version."""
    if not tag or tag.startswith("refs/"):
        raise ReleaseGateError("release tag name is missing or is not a tag name")
    version, channel = canonical_version(repo_root)
    expected_tag = f"v{version}"
    if tag != expected_tag:
        raise ReleaseGateError(
            f"release tag must be exactly {expected_tag}; received {tag or '<empty>'}"
        )
    return {"tag": tag, "version": version, "channel": channel}


def _required_values(environment: Mapping[str, str], names: Sequence[str]) -> None:
    missing = [name for name in names if not environment.get(name, "").strip()]
    if missing:
        raise ReleaseGateError(
            "required production signing settings are missing: " + ", ".join(missing)
        )


def _valid_base64(value: str, label: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ReleaseGateError(f"{label} is not valid base64") from error
    if not decoded:
        raise ReleaseGateError(f"{label} is empty")


def _find_signtool(environment: Mapping[str, str]) -> str | None:
    explicit = environment.get("RELEASE_SIGNTOOL_PATH", "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for name in ("signtool.exe", "signtool"):
        found = shutil.which(name)
        if found:
            return found
    roots = [
        environment.get("ProgramFiles", ""),
        environment.get("ProgramFiles(x86)", ""),
    ]
    for root in (Path(value) for value in roots if value):
        kits_root = root / "Windows Kits" / "10" / "bin"
        if not kits_root.is_dir():
            continue
        candidates = sorted(kits_root.glob("*/x64/signtool.exe"), reverse=True)
        if candidates:
            return str(candidates[0])
    return None


def _validate_windows_certificate(environment: Mapping[str, str]) -> None:
    """Parse the configured PFX before a production Windows build starts."""
    if os.name != "nt":
        return
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        raise ReleaseGateError(
            "PowerShell is required to validate the Windows signing certificate"
        )
    temp_root = Path(tempfile.mkdtemp(prefix="tobkiri-pfx-check-"))
    certificate = temp_root / "release-signing.pfx"
    try:
        certificate.write_bytes(
            base64.b64decode(environment["WINDOWS_CERTIFICATE_BASE64"], validate=True)
        )
        child_environment = os.environ.copy()
        child_environment.update(environment)
        child_environment["RELEASE_PFX_PATH"] = str(certificate)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$password = ConvertTo-SecureString $env:WINDOWS_CERTIFICATE_PASSWORD -AsPlainText -Force; Get-PfxData -FilePath $env:RELEASE_PFX_PATH -Password $password | Out-Null",
            ],
            env=child_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ReleaseGateError("Windows production signing certificate is invalid")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def validate_signing_policy(
    mode: str,
    platform: str,
    environment: Mapping[str, str] | None = None,
    check_tools: bool = False,
) -> dict[str, str | bool]:
    """Validate explicit production/local-dev mode without exposing secrets."""
    if mode not in SIGNING_MODES:
        raise ReleaseGateError(f"unsupported release mode: {mode}")
    if platform not in PLATFORMS:
        raise ReleaseGateError(f"unsupported release platform: {platform}")
    values = os.environ if environment is None else environment
    if mode == "local-dev":
        return {"mode": mode, "platform": platform, "production": False}

    if values.get("TOBKIRI_ALLOW_UNSIGNED_RELEASE", "").lower() in {"1", "true", "yes"}:
        raise ReleaseGateError(
            "unsigned release override is forbidden in production mode"
        )
    if platform == "macos":
        _required_values(values, MACOS_SIGNING_ENV)
        _valid_base64(values["APPLE_CERTIFICATE_BASE64"], "APPLE_CERTIFICATE_BASE64")
        if not values["APPLE_SIGNING_IDENTITY"].startswith(
            "Developer ID Application: "
        ):
            raise ReleaseGateError(
                "APPLE_SIGNING_IDENTITY must be a Developer ID Application identity"
            )
        if check_tools:
            for command in ("codesign", "ditto", "spctl", "xcrun"):
                if shutil.which(command) is None:
                    raise ReleaseGateError(
                        f"required macOS release tool is unavailable: {command}"
                    )
    elif platform == "windows":
        _required_values(values, WINDOWS_SIGNING_ENV)
        _valid_base64(
            values["WINDOWS_CERTIFICATE_BASE64"], "WINDOWS_CERTIFICATE_BASE64"
        )
        if not values["WINDOWS_TIMESTAMP_URL"].startswith("https://"):
            raise ReleaseGateError("WINDOWS_TIMESTAMP_URL must use https")
        if check_tools and _find_signtool(values) is None:
            raise ReleaseGateError(
                "signtool.exe is required for production Windows releases"
            )
        if check_tools:
            _validate_windows_certificate(values)
    return {"mode": mode, "platform": platform, "production": True}


def _run(command: Sequence[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except OSError as error:
        raise ReleaseGateError(
            f"release signing tool failed to start: {command[0]}"
        ) from error
    except subprocess.CalledProcessError as error:
        raise ReleaseGateError(
            f"release signing verification failed with exit code {error.returncode}"
        ) from error


def sign_macos_artifacts(
    repo_root: Path,
    app_bundle: Path | None,
    artifacts: Sequence[Path],
    environment: Mapping[str, str] | None = None,
) -> None:
    """Verify, notarize, and staple production macOS artifacts."""
    values = os.environ if environment is None else environment
    validate_signing_policy("production", "macos", values, check_tools=True)
    if app_bundle is not None:
        verify_script = (
            repo_root / "tobkiri_launcher" / "scripts" / "verify_macos_release.sh"
        )
        _run(
            [
                "bash",
                str(verify_script),
                "--app-bundle",
                str(app_bundle),
                "--signing-identity",
                values["APPLE_SIGNING_IDENTITY"],
            ]
        )
    artifact_candidates = ([app_bundle] if app_bundle is not None else []) + list(
        artifacts
    )
    unique_artifacts = list(dict.fromkeys(Path(path) for path in artifact_candidates))
    if not unique_artifacts:
        raise ReleaseGateError("production macOS release has no notarized artifact")
    for artifact in unique_artifacts:
        if artifact.is_symlink() or not artifact.exists():
            raise ReleaseGateError(
                f"macOS release artifact is missing or symlinked: {artifact}"
            )

    temp_root = Path(tempfile.mkdtemp(prefix="tobkiri-notary-"))
    keychain = temp_root / "notary.keychain-db"
    profile = f"tobkiri-release-{os.getpid()}"
    try:
        _run(
            [
                "xcrun",
                "notarytool",
                "store-credentials",
                profile,
                "--apple-id",
                values["APPLE_ID"],
                "--password",
                values["APPLE_PASSWORD"],
                "--team-id",
                values["APPLE_TEAM_ID"],
                "--keychain",
                str(keychain),
            ]
        )
        for index, artifact in enumerate(unique_artifacts):
            submission = artifact
            if artifact.is_dir() and artifact.name.endswith(".app"):
                submission = temp_root / f"notary-submission-{index}.zip"
                _run(
                    [
                        "ditto",
                        "-c",
                        "-k",
                        "--keepParent",
                        str(artifact),
                        str(submission),
                    ]
                )
            _run(
                [
                    "xcrun",
                    "notarytool",
                    "submit",
                    str(submission),
                    "--keychain-profile",
                    profile,
                    "--keychain",
                    str(keychain),
                    "--wait",
                ]
            )
            _run(["xcrun", "stapler", "staple", "-v", str(artifact)])
            _run(["xcrun", "stapler", "validate", "-v", str(artifact)])
            _run(
                [
                    "spctl",
                    "--assess",
                    "--type",
                    "open",
                    "--context",
                    "context:primary-signature",
                    "--verbose=4",
                    str(artifact),
                ]
            )
    finally:
        security = shutil.which("security")
        if security and keychain.exists():
            subprocess.run([security, "delete-keychain", str(keychain)], check=False)
        shutil.rmtree(temp_root, ignore_errors=True)


def sign_windows_artifacts(
    artifacts: Sequence[Path], environment: Mapping[str, str] | None = None
) -> None:
    """Authenticode-sign and verify each production Windows artifact."""
    values = os.environ if environment is None else environment
    validate_signing_policy("production", "windows", values, check_tools=True)
    unique_artifacts = list(dict.fromkeys(Path(path) for path in artifacts))
    if not unique_artifacts:
        raise ReleaseGateError(
            "production Windows release has no Authenticode artifact"
        )
    signtool = _find_signtool(values)
    if signtool is None:
        raise ReleaseGateError(
            "signtool.exe is required for production Windows releases"
        )
    temp_root = Path(tempfile.mkdtemp(prefix="tobkiri-signing-"))
    certificate = temp_root / "release-signing.pfx"
    try:
        certificate.write_bytes(
            base64.b64decode(values["WINDOWS_CERTIFICATE_BASE64"], validate=True)
        )
        for artifact in unique_artifacts:
            if artifact.is_symlink() or not artifact.is_file():
                raise ReleaseGateError(
                    f"Windows release artifact is missing or symlinked: {artifact}"
                )
            _run(
                [
                    signtool,
                    "sign",
                    "/fd",
                    "SHA256",
                    "/f",
                    str(certificate),
                    "/p",
                    values["WINDOWS_CERTIFICATE_PASSWORD"],
                    "/tr",
                    values["WINDOWS_TIMESTAMP_URL"],
                    "/td",
                    "SHA256",
                    "/d",
                    "Tobkiri Launcher",
                    str(artifact),
                ]
            )
            _run([signtool, "verify", "/pa", "/all", "/tw", "/v", str(artifact)])
    finally:
        certificate.unlink(missing_ok=True)
        shutil.rmtree(temp_root, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    version = subparsers.add_parser("version")
    version.add_argument("--repo-root", type=Path, required=True)
    version.add_argument("--tag", required=True)

    signing = subparsers.add_parser("signing")
    signing.add_argument("--mode", choices=sorted(SIGNING_MODES), required=True)
    signing.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    signing.add_argument("--check-tools", action="store_true")

    sign = subparsers.add_parser("sign-artifacts")
    sign.add_argument("--repo-root", type=Path, required=True)
    sign.add_argument("--mode", choices=["production"], required=True)
    sign.add_argument("--platform", choices=["macos", "windows"], required=True)
    sign.add_argument("--app-bundle", type=Path)
    sign.add_argument("--artifact", type=Path, action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "version":
            result = validate_release_tag(args.repo_root.resolve(), args.tag)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "signing":
            result = validate_signing_policy(
                args.mode, args.platform, check_tools=args.check_tools
            )
            print(json.dumps(result, sort_keys=True))
        else:
            if args.platform == "macos":
                sign_macos_artifacts(
                    args.repo_root.resolve(), args.app_bundle, args.artifact
                )
            else:
                sign_windows_artifacts(args.artifact)
            print(f"{args.platform} production signing and verification passed")
    except ReleaseGateError as error:
        print(f"release gate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
