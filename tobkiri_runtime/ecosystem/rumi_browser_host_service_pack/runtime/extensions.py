"""Bounded UTF-8 extension packages created or selected by the user."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .cdp import CDPError
from .storage import checked_path, private_directory

MAX_EXTENSION_FILES = 256
MAX_EXTENSION_FILE_BYTES = 1024 * 1024
MAX_EXTENSION_BYTES = 4 * 1024 * 1024


def prepare_extension(
    root: Path, profile_id: str, payload: Mapping[str, Any]
) -> tuple[Path, dict[str, Any]]:
    """Validate all files and permissions before materializing a new package."""

    files = payload.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_EXTENSION_FILES:
        raise ValueError("An extension needs 1 to 256 UTF-8 files")
    total = 0
    validated: dict[str, bytes] = {}
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("Extension files must map relative names to UTF-8 text")
        path = PurePosixPath(name)
        if (
            not name
            or len(name) > 256
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or any(part.endswith((".", " ")) for part in path.parts)
            or any(_windows_device(part) for part in path.parts)
        ):
            raise ValueError("Extension filenames must stay within the package")
        body = content.encode("utf-8")
        total += len(body)
        if len(body) > MAX_EXTENSION_FILE_BYTES or total > MAX_EXTENSION_BYTES:
            raise ValueError("Extension files exceed the 1 MiB/file or 4 MiB/package limit")
        validated[name] = body
    manifest_body = validated.get("manifest.json")
    if manifest_body is None or len(manifest_body) > 65_536:
        raise ValueError("A root manifest.json of at most 64 KiB is required")
    manifest = json.loads(manifest_body.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 3:
        raise ValueError("The managed browser supports Manifest V3 extensions")
    name = manifest.get("name")
    version = manifest.get("version")
    if not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise ValueError("Extension name is required and must be bounded")
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){0,3}", version):
        raise ValueError("Extension version is invalid")
    permissions = _string_list(manifest.get("permissions", []))
    host_permissions = _string_list(manifest.get("host_permissions", []))
    scripts = manifest.get("content_scripts", [])
    if not isinstance(scripts, list) or len(scripts) > 100:
        raise ValueError("Extension content scripts must be bounded")
    for script in scripts:
        if not isinstance(script, dict):
            raise ValueError("Extension content script is invalid")
        host_permissions.extend(_string_list(script.get("matches", [])))
    package_id = uuid.uuid4().hex
    directory = root / "extensions" / profile_id / package_id
    private_directory(root, directory)
    try:
        for filename, body in validated.items():
            target = checked_path(root, directory / filename)
            private_directory(root, target.parent)
            with target.open("xb") as handle:
                os.chmod(target, 0o600)
                handle.write(body)
        return directory, {
            "package_id": package_id,
            "name": name,
            "version": version,
            "permissions": sorted(set(permissions)),
            "host_permissions": sorted(set(host_permissions)),
            "file_count": len(validated),
            "size": total,
        }
    except Exception:
        remove_package(root, directory)
        raise


def remove_package(root: Path, directory: Path) -> None:
    """Remove only one validated package directory under owned storage."""

    checked_path(root, directory)
    resolved = directory.resolve()
    relative = resolved.relative_to((root / "extensions").resolve())
    if len(relative.parts) != 2 or not re.fullmatch(r"[a-f0-9]{32}", relative.parts[1]):
        raise PermissionError("Extension cleanup is outside the package directory")
    if not directory.exists():
        return
    for candidate in directory.rglob("*"):
        checked_path(root, candidate)
    shutil.rmtree(resolved)


def unsupported_result(action: str) -> dict[str, Any]:
    """Explain the optional experimental CDP extension API limitation."""

    return {
        "action": action,
        "is_error": True,
        "supported": False,
        "error_type": "browser_extensions_unsupported",
        "message": (
            "This Chromium build does not support the Extensions CDP API. "
            "Use a current Chrome for Testing or Chromium build configured "
            "through RUMI_BROWSER_EXECUTABLE."
        ),
    }


def extension_api_unavailable(exc: CDPError) -> bool:
    """Only classify missing/disabled APIs as unsupported, not all CDP errors."""

    return exc.unsupported


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Extension permissions must be a bounded list")
    if any(not isinstance(item, str) or not 1 <= len(item) <= 1024 for item in value):
        raise ValueError("Extension permission is invalid")
    return list(value)


def _windows_device(value: str) -> bool:
    stem = value.split(".", 1)[0].upper()
    return stem in {"CON", "PRN", "AUX", "NUL"} or bool(re.fullmatch(r"(?:COM|LPT)[1-9]", stem))
