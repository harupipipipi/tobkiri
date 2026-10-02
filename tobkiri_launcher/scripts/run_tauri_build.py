#!/usr/bin/env python3
"""Run a Tauri build with deterministic macOS system-tool binding."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


MACOS_XATTR = Path("/usr/bin/xattr")
MACOS_SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
CI_E2E_POLICY = "ci-e2e-v1"


def _verify_macos_xattr(path: Path = MACOS_XATTR) -> None:
    """Require Apple's canonical root-owned xattr executable."""
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        path.resolve(strict=True) != path
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or mode & 0o022
        or not mode & 0o111
    ):
        raise RuntimeError("canonical macOS xattr tool identity is unsafe")


def _bound_path(original: str) -> str:
    """Put immutable system tool roots first without dropping build tools."""
    entries = list(MACOS_SYSTEM_PATH)
    for entry in original.split(os.pathsep):
        if entry and entry not in entries:
            entries.append(entry)
    return os.pathsep.join(entries)


def _bundle_targets(arguments: Sequence[str]) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.startswith("--bundles="):
            return argument.split("=", 1)[1]
        if argument == "--bundles" and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def build_environment(
    environ: Mapping[str, str], *, platform: str = sys.platform
) -> dict[str, str]:
    """Return the child environment for one canonical Tauri build."""
    result = dict(environ)
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    if platform == "darwin":
        _verify_macos_xattr()
        result["PATH"] = _bound_path(environ.get("PATH", ""))
        resolved = shutil.which("xattr", path=result["PATH"])
        if resolved != os.fspath(MACOS_XATTR):
            raise RuntimeError("macOS Tauri xattr binding is not deterministic")
    return result


def validate_arguments(
    arguments: Sequence[str], environ: Mapping[str, str]
) -> None:
    """Reject non-build commands and publishable CI/E2E bundle targets."""
    if not arguments or arguments[0] != "build":
        raise RuntimeError("the Tauri runner accepts only the build subcommand")
    if environ.get("TOBKIRI_MACOS_ARTIFACT_POLICY") == CI_E2E_POLICY:
        if "--ci" not in arguments or _bundle_targets(arguments) != "app":
            raise RuntimeError(
                "ci-e2e-v1 is non-publishable and requires --ci --bundles app"
            )


def run(arguments: Sequence[str], environ: Mapping[str, str] = os.environ) -> int:
    """Execute the installed Tauri CLI with the verified child environment."""
    validate_arguments(arguments, environ)
    environment = build_environment(environ)
    cargo_tauri = shutil.which("cargo-tauri", path=environ.get("PATH", ""))
    if cargo_tauri is None:
        raise RuntimeError("cargo-tauri is unavailable")
    return subprocess.run(
        [cargo_tauri, *arguments],
        check=False,
        env=environment,
    ).returncode


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(tuple(sys.argv[1:] if argv is None else argv))
    except (OSError, RuntimeError) as exc:
        print(f"Tauri build binding failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
