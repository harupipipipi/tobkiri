"""Verify content projections selected by one exact Profile revision."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from tobkiri_protocol.canonical import canonical_digest

from .profile_pack_projection import ProjectionPackSource, projection_pack_root


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PROJECTION_ROOT = RUNTIME_ROOT / "profile_projections"


class ProfileContentProjectionError(ValueError):
    """A Profile content projection is unavailable, unsafe, or stale."""


def resolve_intent_projection(
    selection: Mapping[str, Any],
    *,
    pack_sources: Mapping[str, ProjectionPackSource] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve an author intent entry into a digest-bound Profile entry."""

    if selection.get("content_digest") is not None:
        raise ProfileContentProjectionError("author intent projection digest must be unresolved")
    root, pack_digest, declared = _selection_root(selection, pack_sources)
    files = _inventory(root)
    _verify_declared(files, declared)
    resolved = {
        "projection_id": str(selection.get("projection_id") or ""),
        "kind": str(selection.get("kind") or ""),
        "artifact_root": str(selection["artifact_root"]),
        "content_digest": canonical_digest(
            [{"path": path, "digest": digest} for path, digest in files.items()]
        ),
        "file_count": len(files),
    }
    if pack_digest is not None:
        resolved["source_pack_id"] = selection["source_pack_id"]
        resolved["source_artifact_digest"] = pack_digest
    legacy_id = selection.get("source_legacy_pack_id")
    if legacy_id is not None:
        resolved["source_legacy_pack_id"] = str(legacy_id)
    return resolved, files


def verify_resolved_projection(
    selection: Mapping[str, Any],
    *,
    pack_sources: Mapping[str, ProjectionPackSource] | None = None,
) -> Path:
    """Verify and resolve one Profile-bound projection root."""

    root, _pack_digest, declared = _selection_root(selection, pack_sources)
    files = _inventory(root)
    _verify_declared(files, declared)
    digest = canonical_digest([{"path": path, "digest": value} for path, value in files.items()])
    if digest != selection.get("content_digest") or len(files) != selection.get("file_count"):
        raise ProfileContentProjectionError("Profile projection binding is stale")
    return root


def resolve_profile_projection(
    selection: Mapping[str, Any],
    *,
    pack_sources: Mapping[str, ProjectionPackSource] | None = None,
) -> dict[str, Any]:
    """Resolve an intent entry or reverify an already resolved entry."""

    if selection.get("content_digest") is None:
        resolved, _files = resolve_intent_projection(selection, pack_sources=pack_sources)
        return resolved
    verify_resolved_projection(selection, pack_sources=pack_sources)
    return dict(selection)


def selected_projection_roots(
    selections: Iterable[Mapping[str, Any]],
    *,
    kind: str | None = None,
    pack_sources: Mapping[str, ProjectionPackSource] | None = None,
) -> tuple[tuple[str, Path], ...]:
    """Resolve only projections selected by the captured Profile."""

    roots = []
    seen: set[str] = set()
    seen_roots: set[Path] = set()
    for selection in selections:
        projection_id = str(selection.get("projection_id") or "")
        if not projection_id or projection_id in seen:
            raise ProfileContentProjectionError("Profile projection ID is invalid")
        seen.add(projection_id)
        if kind is not None and selection.get("kind") != kind:
            continue
        root = verify_resolved_projection(selection, pack_sources=pack_sources)
        resolved_root = root.resolve()
        if resolved_root in seen_roots:
            raise ProfileContentProjectionError(
                "multiple projection IDs alias the same artifact root"
            )
        seen_roots.add(resolved_root)
        roots.append((projection_id, root))
    return tuple(sorted(roots, key=lambda item: item[0]))


def _selection_root(
    selection: Mapping[str, Any],
    sources: Mapping[str, ProjectionPackSource] | None,
) -> tuple[Path, str | None, Mapping[str, str] | None]:
    relative = str(selection.get("artifact_root") or "")
    pack_id = selection.get("source_pack_id")
    if pack_id is None:
        if "source_artifact_digest" in selection:
            raise ProfileContentProjectionError("projection Pack identity is missing")
        return _resolve_root(relative), None, None
    if "source_legacy_pack_id" in selection:
        raise ProfileContentProjectionError("projection cannot mix Pack and legacy origins")
    if selection.get("content_digest") is not None and not selection.get(
        "source_artifact_digest"
    ):
        raise ProfileContentProjectionError("resolved projection Pack artifact pin is missing")
    try:
        return projection_pack_root(
            str(pack_id), selection.get("source_artifact_digest"), relative, sources
        )
    except (OSError, ValueError) as error:
        raise ProfileContentProjectionError(str(error)) from error


def _verify_declared(files: Mapping[str, str], declared: Mapping[str, str] | None) -> None:
    if declared is not None and files != declared:
        raise ProfileContentProjectionError("projection content differs from Pack artifacts")


def _resolve_root(relative_value: str) -> Path:
    relative = Path(relative_value)
    if (
        not relative_value
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[0] != "profile_projections"
    ):
        raise ProfileContentProjectionError("Profile projection path is unsafe")
    root = RUNTIME_ROOT / relative
    current = RUNTIME_ROOT
    for component in relative.parts:
        current /= component
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise ProfileContentProjectionError("Profile projection root is unavailable") from error
        if stat.S_ISLNK(mode):
            raise ProfileContentProjectionError("Profile projection path contains a symlink")
    if not root.is_dir():
        raise ProfileContentProjectionError("Profile projection root is unavailable")
    try:
        root.resolve().relative_to(PROJECTION_ROOT.resolve())
    except ValueError as error:
        raise ProfileContentProjectionError(
            "Profile projection root escapes the neutral boundary"
        ) from error
    return root


def _inventory(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProfileContentProjectionError("Profile projection contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise ProfileContentProjectionError(
                        "Profile projection entry is not a regular file"
                    )
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(descriptor)
                current = path.lstat()
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ProfileContentProjectionError(
                "Profile projection changed while being read"
            ) from error
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        identity_current = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        if identity_before != identity_after or identity_after != identity_current:
            raise ProfileContentProjectionError("Profile projection changed while being read")
        files[relative] = "sha256:" + digest.hexdigest()
    if not files:
        raise ProfileContentProjectionError("Profile projection is empty")
    return files


__all__ = [
    "ProfileContentProjectionError",
    "resolve_intent_projection",
    "resolve_profile_projection",
    "selected_projection_roots",
    "verify_resolved_projection",
]
