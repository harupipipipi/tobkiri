#!/usr/bin/env python3
"""Prepare trusted runtime tools for Rumi Viewer development and release builds."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Mapping, Sequence

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_TARGET_ENV = "TAURI_ENV_TARGET_TRIPLE"
UV_PATH_ENV = "RUMI_UV_PATH"
SOURCE_PROVENANCE_FILENAME = "packaging-source-provenance.v1.json"
ISOLATED_MODULE_CODE = (
    "import runpy,sys;root=sys.argv[1];name=sys.argv[2];"
    "sys.path.insert(0,root);sys.argv=[name,*sys.argv[3:]];"
    "runpy.run_module(name,run_name='__main__',alter_sys=True)"
)


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
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ if env is None else env)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [os.fspath(part) for part in command],
        cwd=cwd,
        check=True,
        env=environment,
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


def _target_shell_spec(repo_root: Path, target: str) -> dict[str, str | Path]:
    target_root = repo_root / "tobkiri_launcher" / "src-tauri" / "target" / target / "debug"
    if target == "aarch64-apple-darwin":
        platform_name, architecture = "macos", "arm64"
    elif target == "x86_64-apple-darwin":
        platform_name, architecture = "macos", "x86_64"
    elif target == "x86_64-unknown-linux-gnu":
        platform_name, architecture = "linux", "x86_64"
    elif target == "x86_64-pc-windows-msvc":
        platform_name, architecture = "windows", "x86_64"
    else:
        raise RuntimeError(f"Unsupported development Shell target: {target}")

    if platform_name == "macos":
        artifact = target_root / "bundle" / "macos" / "Tobkiri.app"
        return {
            "platform": platform_name,
            "architecture": architecture,
            "bundle": "app",
            "artifact": artifact,
            "relative_path": "Tobkiri.app",
            "entrypoint": "Tobkiri.app/Contents/MacOS/tobkiri-shell",
        }
    if platform_name == "linux":
        artifact_dir = target_root / "bundle" / "appimage"
        candidates = sorted(artifact_dir.glob("*.AppImage"))
        artifact = candidates[0] if len(candidates) == 1 else artifact_dir / "Tobkiri.AppImage"
        return {
            "platform": platform_name,
            "architecture": architecture,
            "bundle": "appimage",
            "artifact": artifact,
            "relative_path": "Tobkiri.AppImage",
            "entrypoint": "Tobkiri.AppImage",
        }
    artifact = target_root / "tobkiri-shell.exe"
    return {
        "platform": platform_name,
        "architecture": architecture,
        "bundle": "nsis",
        "artifact": artifact,
        "relative_path": "tobkiri-shell.exe",
        "entrypoint": "tobkiri-shell.exe",
    }


def sign_development_macos_app(application: Path) -> None:
    """Give the checkout Shell a complete, launchable ad-hoc bundle signature."""
    if sys.platform != "darwin":
        return
    run_command(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            application,
        ]
    )
    run_command(
        [
            "/usr/bin/codesign",
            "--verify",
            "--strict",
            "--all-architectures",
            application,
        ]
    )


def prepare_dev_pack_shell(repo_root: Path, target: str) -> Path:
    """Build and stage the verified checkout Pack Shell."""
    manifest = repo_root / "pack-shell" / "Cargo.toml"
    run_command(
        ["cargo", "build", "--target", target, "--manifest-path", manifest],
        cwd=repo_root,
    )
    binary_name = "pack-shell.exe" if is_windows_target(target) else "pack-shell"
    binary = repo_root / "pack-shell" / "target" / target / "debug" / binary_name
    if not binary.is_file():
        raise RuntimeError(f"Development pack-shell was not produced: {binary}")
    digest_path = binary.with_name(f"{binary.name}.sha256")
    digest_path.write_text(hashlib.sha256(binary.read_bytes()).hexdigest() + "\n", encoding="ascii")
    bundled_root = repo_root / "tobkiri_runtime" / "bundled"
    bundled_root.mkdir(parents=True, exist_ok=True)
    copy_dev_uv(binary, bundled_root / binary_name)
    presentation_catalog = (
        repo_root
        / "tobkiri_launcher"
        / "src-tauri"
        / "bundled"
        / "presentation_catalog.json"
    )
    if not presentation_catalog.is_file():
        raise RuntimeError(f"Launcher presentation catalog is missing: {presentation_catalog}")
    shutil.copy2(presentation_catalog, bundled_root / "presentation_catalog.json")
    return binary


def _git_identity(repo_root: Path, revision: str) -> str:
    result = run_command(
        ["git", "rev-parse", "--verify", revision],
        cwd=repo_root,
        capture_output=True,
    )
    identity = result.stdout.strip()
    if len(identity) != 40 or any(character not in "0123456789abcdef" for character in identity):
        raise RuntimeError(f"Git returned an invalid identity for {revision}")
    return identity


def prepare_dev_defaults(repo_root: Path, target: str) -> Path:
    """Build a development Defaults bundle from verified clean source."""
    launcher_root = repo_root / "tobkiri_launcher"
    runtime_root = repo_root / "tobkiri_runtime"
    spec = _target_shell_spec(repo_root, target)
    run_command(
        [
            "cargo", "tauri", "build", "--debug", "--target", target,
            "--config", "src-tauri/tauri.shell.conf.json",
            "--bundles", str(spec["bundle"]), "--ci",
        ],
        cwd=launcher_root,
    )
    artifact = Path(spec["artifact"])
    if not artifact.exists():
        raise RuntimeError(f"Development Tauri Shell was not produced: {artifact}")
    if spec["platform"] == "macos":
        # Cargo's linker signature covers only the Mach-O. LaunchServices
        # requires a complete application-bundle signature, even for local
        # unsigned development. Ad-hoc-sign the exact bytes used by both the
        # Launcher and Defaults metadata so developers need no certificate.
        sign_development_macos_app(artifact)

    # The Launcher resolves presentation artifacts beneath its application
    # root.  Keep the unsigned checkout Shell in an ignored development-only
    # location there so a debug Launcher can verify the exact bytes it will
    # launch without weakening packaged release bindings.
    dev_shell_root = runtime_root / "bundled" / "dev-shell"
    if dev_shell_root.exists():
        if dev_shell_root.is_symlink() or not dev_shell_root.is_dir():
            raise RuntimeError(f"Unsafe development Shell output: {dev_shell_root}")
        shutil.rmtree(dev_shell_root)
    dev_shell_root.mkdir(parents=True)
    staged_shell = dev_shell_root / str(spec["relative_path"])
    if artifact.is_dir():
        shutil.copytree(artifact, staged_shell)
    else:
        shutil.copy2(artifact, staged_shell)

    output_root = launcher_root / "src-tauri" / "target" / "dev-defaults"
    if output_root.exists():
        if output_root.is_symlink() or not output_root.is_dir():
            raise RuntimeError(f"Unsafe development Defaults output: {output_root}")
        shutil.rmtree(output_root)
    bundle_root = output_root / "v4"
    artifact_root = output_root / "platform-artifacts"
    shutil.copytree(runtime_root / "ecosystem" / "defaultspack" / "v4", bundle_root)
    artifact_root.mkdir(parents=True)

    manifest = runtime_root / "packaged_defaultspack_source_manifest.v1.json"
    provenance = runtime_root / SOURCE_PROVENANCE_FILENAME
    if provenance.exists() or provenance.is_symlink():
        raise RuntimeError(f"Refusing to replace existing source provenance: {provenance}")
    source_status = run_command(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=repo_root,
        capture_output=True,
    )
    if source_status.stdout.strip():
        raise RuntimeError(
            "Development Defaults packaging requires committed source changes; "
            "refusing to attest a modified checkout as clean."
        )
    payload = {
        "schema": "io.tobkiri.packaging-source-provenance.v1",
        "source_commit": _git_identity(repo_root, "HEAD"),
        "source_tree": _git_identity(repo_root, "HEAD^{tree}"),
        "source_clean": True,
        "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }
    try:
        provenance.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        provenance.chmod(0o444)
        python = repo_root / ".venv" / ("Scripts/python.exe" if is_windows_target(target) else "bin/python3")
        if not python.is_file():
            raise RuntimeError(f"Development Python environment is missing: {python}")
        run_command(
            [
                python, "-I", "-B", "-c", ISOLATED_MODULE_CODE,
                runtime_root, "scripts.generate_packaged_defaultspack_v4_bundle",
                "--source-artifact", artifact,
                "--bundle-root", bundle_root,
                "--artifact-root", artifact_root,
                "--relative-path", str(spec["relative_path"]),
                "--entrypoint", str(spec["entrypoint"]),
                "--platform", str(spec["platform"]),
                "--architecture", str(spec["architecture"]),
                "--bundle-identity", "io.tobkiri.shell.tauri",
                "--source-provenance-file", SOURCE_PROVENANCE_FILENAME,
            ],
            cwd=runtime_root,
        )
    finally:
        if provenance.exists():
            provenance.chmod(0o600)
            provenance.unlink()
    print(f"Prepared unsigned development Defaults Profile at {bundle_root}")
    return bundle_root


def prepare_dev_environment(repo_root: Path, target: str) -> None:
    """Prepare development tools and the matching Defaults application."""
    prepare_dev(repo_root, target)
    prepare_dev_pack_shell(repo_root, target)
    prepare_dev_defaults(repo_root, target)


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
            "-I",
            "-B",
            repo_root / ".github" / "scripts" / "prepare_tauri_resources.py",
            "--repo-root",
            repo_root,
            "--target",
            target,
            "--uv-version",
            preparer.UV_PINNED_VERSION,
            "--require-runtime-tools",
        ],
        # The sealed interpreter resolves its relocatable home from this root.
        cwd=Path(os.environ.get("TOBKIRI_PACKAGING_PYTHON_SNAPSHOT", repo_root)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    target = resolve_target(args.target)

    try:
        if args.mode == "dev":
            prepare_dev_environment(repo_root, target)
        else:
            prepare_release(repo_root, target)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Rumi Viewer runtime preparation failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
