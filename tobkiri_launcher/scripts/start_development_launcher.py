#!/usr/bin/env python3
"""Build and run the complete Apple Silicon development desktop without Docker."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from prepare_viewer_runtime import REPO_ROOT, host_target, run_command


def build_development_launcher(repo_root: Path) -> Path:
    """Build the checkout Launcher and verify its signed native PackVM helper."""
    target = host_target()
    if target != "aarch64-apple-darwin":
        raise RuntimeError(
            "The complete development desktop requires Apple Silicon macOS"
        )
    if os.environ.get("CARGO_TARGET_DIR"):
        raise RuntimeError(
            "Use the checkout's default Cargo target directory for desktop development"
        )
    launcher = repo_root / "tobkiri_launcher"
    python = repo_root / ".venv/bin/python3"
    if not python.is_file():
        raise RuntimeError("Create .venv and install the README dependencies first")

    run_command(
        [
            "npm",
            "exec",
            "--prefix",
            "frontend",
            "--",
            "tauri",
            "build",
            "--debug",
            "--target",
            target,
            "--config",
            "src-tauri/tauri.macos.dev.conf.json",
            "--bundles",
            "app",
            "--ci",
        ],
        cwd=launcher,
    )
    app = (
        launcher
        / "src-tauri/target"
        / target
        / "debug/bundle/macos"
        / "Tobkiri Launcher Developer.app"
    )
    executable = app / "Contents/MacOS/tobkiri-launcher"
    if not executable.is_file():
        raise RuntimeError(f"Development Launcher was not produced: {executable}")
    run_command(
        [
            "bash",
            launcher / "scripts/build_packvm_vz_helper.sh",
            "--target",
            target,
            "--app-bundle",
            app,
        ],
        cwd=repo_root,
    )
    helper = app / "Contents/MacOS/tobkiri-packvm-vz-helper"
    run_command(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--identifier",
            "dev.tobkiri.launcher.packvm-vz-helper",
            "--options",
            "runtime",
            "--timestamp=none",
            "--entitlements",
            launcher
            / "packvm-vz-helper/Entitlements"
            / "tobkiri-packvm-vz-helper.entitlements",
            helper,
        ]
    )
    run_command(
        ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", helper]
    )
    verifier = repo_root / ".github/scripts/macos_ci_artifact.py"
    run_command(
        [
            python,
            "-B",
            verifier,
            "write-packvm-bundle-manifest",
            "--app-bundle",
            app,
            "--expected-signing-mode",
            "ad-hoc",
        ],
        cwd=repo_root,
    )
    run_command(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--options",
            "runtime",
            "--timestamp=none",
            app,
        ]
    )
    run_command(
        ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", app]
    )
    run_command(
        [python, "-B", verifier, "verify-packvm-bundle", "--app-bundle", app],
        cwd=repo_root,
    )
    return executable


def main() -> int:
    """Start the verified local desktop, or only build it when requested."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    try:
        executable = build_development_launcher(REPO_ROOT)
        print(f"Verified Tobkiri development desktop: {executable}", flush=True)
        if not args.build_only:
            # Launch the enclosing .app's executable so the existing Launcher
            # binding can discover and authenticate its own PackVM resources.
            run_command([executable], cwd=REPO_ROOT)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Tobkiri development desktop failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
