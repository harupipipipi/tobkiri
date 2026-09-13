#!/usr/bin/env python3
"""Generate wheel-only dependency locks for formal packaging targets."""

from __future__ import annotations

import argparse
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename


PYTHON_VERSION = (3, 13)
SUPPORTED_TARGETS = {"aarch64-apple-darwin": "arm64"}


class LockGenerationError(ValueError):
    """Raised when a formal wheel-only lock cannot be generated safely."""


@dataclass(frozen=True)
class ExportedRequirement:
    """One exact requirement retained from the universal runtime export."""

    requirement: Requirement
    source: str


def _exported_requirements(path: Path) -> list[ExportedRequirement]:
    """Read exact requirements from a uv export without trusting its hashes."""
    requirements: list[ExportedRequirement] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        source = line.removesuffix("\\").strip()
        if source.startswith("--"):
            continue
        requirement = Requirement(source)
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==":
            raise LockGenerationError(f"non-exact runtime requirement: {source}")
        requirements.append(ExportedRequirement(requirement, source))
    if not requirements:
        raise LockGenerationError("runtime requirements export is empty")
    return requirements


def _locked_packages(path: Path) -> dict[tuple[str, str], dict[str, object]]:
    """Index uv lock records by canonical distribution name and version."""
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    packages: dict[tuple[str, str], dict[str, object]] = {}
    for package in document.get("package", []):
        if not isinstance(package, dict):
            raise LockGenerationError("uv.lock contains a malformed package record")
        key = (canonicalize_name(str(package.get("name"))), str(package.get("version")))
        if key in packages:
            raise LockGenerationError(
                f"uv.lock contains duplicate package identity: {key}"
            )
        packages[key] = package
    return packages


def _tag_supports_target(tag: Tag, architecture: str) -> bool:
    """Return whether a wheel tag supports CPython 3.13 on one macOS arch."""
    if tag.platform == "any":
        return tag.interpreter.startswith("py") and tag.abi == "none"
    if not tag.platform.startswith("macosx_"):
        return False
    platform_architecture = next(
        (
            candidate
            for candidate in ("x86_64", "arm64", "universal2")
            if tag.platform.endswith(f"_{candidate}")
        ),
        "",
    )
    if platform_architecture not in {architecture, "universal2"}:
        return False
    if tag.interpreter == "cp313":
        return tag.abi in {"cp313", "abi3"}
    if tag.abi != "abi3" or not tag.interpreter.startswith("cp"):
        return False
    try:
        minimum = int(tag.interpreter.removeprefix("cp"))
    except ValueError:
        return False
    return minimum <= int("".join(str(value) for value in PYTHON_VERSION))


def compatible_wheel_hashes(
    package: dict[str, object], architecture: str
) -> tuple[str, ...]:
    """Return only hashes of wheels compatible with the formal target."""
    hashes: set[str] = set()
    wheels = package.get("wheels")
    if not isinstance(wheels, list):
        raise LockGenerationError("uv.lock package has no wheel inventory")
    for wheel in wheels:
        if not isinstance(wheel, dict):
            raise LockGenerationError("uv.lock contains a malformed wheel record")
        url = wheel.get("url")
        digest = wheel.get("hash")
        if not isinstance(url, str) or not isinstance(digest, str):
            raise LockGenerationError("uv.lock wheel identity is incomplete")
        filename = Path(unquote(urlparse(url).path)).name
        try:
            _, _, _, tags = parse_wheel_filename(filename)
        except ValueError as exc:
            raise LockGenerationError(
                f"invalid wheel filename in uv.lock: {filename}"
            ) from exc
        if not any(_tag_supports_target(tag, architecture) for tag in tags):
            continue
        algorithm, separator, value = digest.partition(":")
        if algorithm != "sha256" or not separator or len(value) != 64:
            raise LockGenerationError(f"invalid wheel digest in uv.lock: {filename}")
        hashes.add(value)
    return tuple(sorted(hashes))


def render_lock(export_path: Path, uv_lock_path: Path, target: str) -> str:
    """Render one deterministic target-specific, wheel-only requirements lock."""
    try:
        architecture = SUPPORTED_TARGETS[target]
    except KeyError as exc:
        raise LockGenerationError(
            f"unsupported formal packaging target: {target}"
        ) from exc
    packages = _locked_packages(uv_lock_path)
    lines = [
        "# Generated by .github/scripts/generate_packaging_dependency_locks.py",
        f"# Target: CPython {PYTHON_VERSION[0]}.{PYTHON_VERSION[1]} {target}",
        "--only-binary :all:",
    ]
    for exported in _exported_requirements(export_path):
        version = next(iter(exported.requirement.specifier)).version
        key = (canonicalize_name(exported.requirement.name), version)
        package = packages.get(key)
        if package is None:
            raise LockGenerationError(f"runtime export is absent from uv.lock: {key}")
        hashes = compatible_wheel_hashes(package, architecture)
        if not hashes:
            raise LockGenerationError(
                f"{key[0]}=={key[1]} has no CPython 3.13 macOS {architecture} wheel"
            )
        lines.append(f"{exported.source} \\")
        for index, digest in enumerate(hashes):
            suffix = " \\" if index + 1 < len(hashes) else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines) + "\n"


def verify_lock(
    output: Path, export_path: Path, uv_lock_path: Path, target: str
) -> None:
    """Reject a stale, tampered, or cross-target formal packaging lock."""
    if output.read_text(encoding="utf-8") != render_lock(
        export_path, uv_lock_path, target
    ):
        raise LockGenerationError("formal packaging dependency lock is stale")


def main() -> int:
    """Generate or verify one formal target lock."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=tuple(SUPPORTED_TARGETS))
    parser.add_argument("--runtime-export", required=True, type=Path)
    parser.add_argument("--uv-lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_lock(args.runtime_export, args.uv_lock, args.target)
    if args.check:
        verify_lock(args.output, args.runtime_export, args.uv_lock, args.target)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
