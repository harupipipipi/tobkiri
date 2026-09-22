#!/usr/bin/env python3
"""Prepare trusted runtime tools for Rumi Viewer development and release builds."""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_TARGET_ENV = "TAURI_ENV_TARGET_TRIPLE"
UV_PATH_ENV = "RUMI_UV_PATH"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("dev", "release"),
        required=True,
        help=(
            "Prepare a developer-managed uv for a checkout, or verified bundled tools "
            "for release."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root. Defaults to this script's repository.",
    )
    parser.add_argument(
        "--target",
        help=f"Rust target triple. Defaults to ${TAURI_TARGET_ENV}, then the host target.",
    )
    return parser.parse_args(argv)


def host_target() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        arch = "aarch64"
    else:
        raise RuntimeError(
            "Unsupported host architecture for Rumi Viewer: "
            f"{machine or '<unknown>'}"
        )

    if sys.platform == "win32":
        return f"{arch}-pc-windows-msvc"
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform.startswith("linux"):
        return f"{arch}-unknown-linux-gnu"
    raise RuntimeError(f"Unsupported host platform for Rumi Viewer: {sys.platform}")


def resolve_target(explicit: str | None, environ: Mapping[str, str] = os.environ) -> str:
    return explicit or environ.get(TAURI_TARGET_ENV) or host_target()


def is_windows_target(target: str) -> bool:
    return "windows" in target or target.endswith("-msvc")


def uv_binary_name(target: str) -> str:
    return "uv.exe" if is_windows_target(target) else "uv"


def repo_venv_uv_path(repo_root: Path, target: str) -> Path:
    if is_windows_target(target):
        return repo_root / ".venv" / "Scripts" / "uv.exe"
    return repo_root / ".venv" / "bin" / "uv"


def bundled_uv_path(repo_root: Path, target: str) -> Path:
    return repo_root / "tobkiri_runtime" / "bundled" / uv_binary_name(target)


def resolve_dev_uv_source(
    repo_root: Path,
    target: str,
    environ: Mapping[str, str] = os.environ,
) -> Path | None:
    configured = environ.get(UV_PATH_ENV)
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            configured_path = repo_root / configured_path
        candidates.append(configured_path)
    candidates.append(repo_venv_uv_path(repo_root, target))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    found = shutil.which(uv_binary_name(target)) or shutil.which("uv")
    return Path(found).resolve() if found else None


def run_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.fspath(part) for part in command],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )


def verify_uv_binary(path: Path) -> str:
    try:
        result = run_command([path, "--version"], capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"uv binary exited with status {exc.returncode}: {path}{suffix}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"uv binary is not executable: {path}: {exc}") from exc

    version = (result.stdout or "").strip()
    if not version:
        raise RuntimeError(f"uv binary did not report a version: {path}")
    return version


def copy_dev_uv(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == destination.resolve():
        return

    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary)
        if os.name != "nt":
            temporary.chmod(
                temporary.stat().st_mode
                | stat.S_IWUSR
                | stat.S_IXUSR
                | stat.S_IXGRP
                | stat.S_IXOTH
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_dev(repo_root: Path, target: str) -> Path:
    destination = bundled_uv_path(repo_root, target)
    source = resolve_dev_uv_source(repo_root, target)

    if source is None:
        if destination.is_file():
            verify_uv_binary(destination)
            print(f"Using existing development uv at {destination}")
            return destination
        expected = repo_venv_uv_path(repo_root, target)
        raise RuntimeError(
            "No trusted development uv binary was found. "
            f"Install development dependencies so {expected} exists, set {UV_PATH_ENV}, "
            "or install uv on PATH."
        )

    source_version = verify_uv_binary(source)
    copy_dev_uv(source, destination)
    staged_version = verify_uv_binary(destination)
    if staged_version != source_version:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            "Staged development uv reported a different version: "
            f"source={source_version!r}, staged={staged_version!r}"
        )
    print(f"Prepared development uv at {destination} from {source} ({staged_version})")
    return destination


def load_resource_preparer(repo_root: Path) -> ModuleType:
    path = repo_root / ".github" / "scripts" / "prepare_tauri_resources.py"
    spec = importlib.util.spec_from_file_location("prepare_tauri_resources", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load runtime resource preparer: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def remove_existing_staged_uv(repo_root: Path, target: str) -> None:
    """Remove a prior dev-stage binary before the verified release stage."""
    destination = bundled_uv_path(repo_root, target)
    if not destination.exists():
        return
    if destination.is_symlink() or not destination.is_file():
        raise RuntimeError(f"Refusing to replace unsafe staged uv path: {destination}")
    if os.name != "nt":
        destination.chmod(destination.stat().st_mode | stat.S_IWUSR)
    destination.unlink()


def prepare_release(repo_root: Path, target: str) -> None:
    preparer = load_resource_preparer(repo_root)
    if target not in preparer.UV_SHA256_BY_TARGET:
        raise RuntimeError(
            f"No pinned uv checksum is configured for release target {target!r}. "
            "Update prepare_tauri_resources.py before building this target."
        )

    manifest = repo_root / "pack-shell" / "Cargo.toml"
    if not manifest.is_file():
        raise RuntimeError(f"pack-shell manifest was not found: {manifest}")

    run_command(
        [
            "cargo",
            "build",
            "--locked",
            "--release",
            "--target",
            target,
            "--manifest-path",
            manifest,
        ],
        cwd=repo_root,
    )

    preparer.seal_pack_shell_binary(repo_root, target)
    remove_existing_staged_uv(repo_root, target)

    run_command(
        [
            sys.executable,
            repo_root / ".github" / "scripts" / "prepare_tauri_resources.py",
            "--repo-root",
            repo_root,
            "--target",
            target,
            "--uv-version",
            preparer.UV_PINNED_VERSION,
            "--require-runtime-tools",
        ],
        cwd=repo_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    target = resolve_target(args.target)

    try:
        if args.mode == "dev":
            prepare_dev(repo_root, target)
        else:
            prepare_release(repo_root, target)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Rumi Viewer runtime preparation failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
