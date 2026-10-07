"""Describe actual prebuilt Shell bytes; never install or confer authority."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import time
from typing import Any, Mapping

from .canonical import canonical_digest, canonical_json
from .errors import ProtocolError
from .platform_artifact import artifact_digest, verify_platform_artifact
from .validation import validate_document


def describe_shell_variant(
    shell: Mapping[str, Any],
    artifact_root: Path,
    *,
    platform: str,
    architecture: str,
) -> dict[str, Any]:
    """Verify one declared build target and return the existing variant shape.

    This is authoring evidence only. The trusted packager must still capture
    these bytes, publish its authenticated catalog, and perform normal approval
    and activation. A returned descriptor is not an installed Shell definition.
    """
    definition = validate_document(canonical_json(dict(shell)), "shell")
    if definition["definition_revision"] != canonical_digest({
        key: value for key, value in definition.items() if key != "definition_revision"
    }):
        raise ProtocolError("Shell source definition revision is stale")
    targets = [
        target for target in definition["launch"].get("build_targets", [])
        if target["platform"] == platform and target["architecture"] == architecture
    ]
    if len(targets) != 1:
        raise ProtocolError("Shell build target is missing or duplicated")
    target = targets[0]
    root = Path(artifact_root).absolute()
    deadline = time.monotonic() + 10
    relative, entry = Path(target["artifact_ref"]), Path(target["entrypoint"])
    if any(path.is_absolute() or ".." in path.parts for path in (relative, entry)):
        raise ProtocolError("Shell build target escapes artifact root")
    for candidate in (root / relative, root / entry):
        for parent in (candidate, *candidate.parents):
            if parent.is_symlink():
                raise ProtocolError("Shell build target contains a symlink")
            if parent == root:
                break
    try:
        (root / entry).resolve(strict=True).relative_to((root / relative).resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ProtocolError("Shell entrypoint is outside its declared artifact") from error
    entrypoint = root / entry
    hasher = hashlib.sha256()
    with os.fdopen(os.open(entrypoint, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 512 * 1024 * 1024:
            raise ProtocolError("Shell entrypoint is not a bounded regular file")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if time.monotonic() >= deadline:
                raise ProtocolError("Shell entrypoint verification deadline exceeded")
            hasher.update(chunk)
    # The ordinary verifier performs stable reads, symlink, architecture,
    # entrypoint, bundle identity and (for macOS) strict code-signature checks.
    variant = {
        "platform": platform,
        "architecture": architecture,
        "relative_path": relative.as_posix(),
        "entrypoint": entry.as_posix(),
        "bundle_identity": target["bundle_identity"],
        "artifact_digest": artifact_digest(root / relative, deadline_monotonic=deadline),
        "entrypoint_digest": "sha256:" + hasher.hexdigest(),
    }
    verify_platform_artifact(
        root,
        variant,
        require_macos_code_signature=platform == "macos",
        deadline_monotonic=deadline,
    )
    return variant
