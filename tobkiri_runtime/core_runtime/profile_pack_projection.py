"""Verify Profile-selected content inside exact Normal Pack artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.validation import validate_file


@dataclass(frozen=True)
class ProjectionPackSource:
    """One selected exact artifact; an offline source conveys no authority."""

    artifact_digest: str
    root: Path


def admitted_projection_sources(
    selections: Iterable[Mapping[str, Any]], artifact_pins: Mapping[str, str]
) -> dict[str, ProjectionPackSource]:
    """Resolve only projection origins present in the captured selected closure."""

    from .external_pack_catalog_v4 import resolve_admitted_pack_root

    result = {}
    for selection in selections:
        pack_id = selection.get("source_pack_id")
        if pack_id is None:
            continue
        if pack_id not in artifact_pins:
            raise ValueError("projection Pack is outside the selected Profile closure")
        result[pack_id] = ProjectionPackSource(
            artifact_pins[pack_id], resolve_admitted_pack_root(pack_id)
        )
    return result


def projection_pack_root(
    pack_id: str,
    artifact_digest: str | None,
    relative: str,
    sources: Mapping[str, ProjectionPackSource] | None,
) -> tuple[Path, str, Mapping[str, str]]:
    """Resolve a selected artifact and its declared content subtree.

    Runtime callers omit ``sources``: selection then comes from the captured
    verified Profile and roots from Host admission. Source-release compilers
    supply their finite, selected, digest-pinned inputs instead.
    """

    if sources is None:
        from .external_pack_catalog_v4 import resolve_admitted_pack_root
        from .resolved_profile_scope import persisted_resolved_profile

        active = persisted_resolved_profile()
        sources = {}
        if active is not None:
            for pack in active.packs:
                if pack.pack_id == pack_id:
                    sources[pack_id] = ProjectionPackSource(
                        pack.content_hash, resolve_admitted_pack_root(pack_id)
                    )
    source = sources.get(pack_id)
    if source is None or (
        artifact_digest is not None and artifact_digest != source.artifact_digest
    ):
        raise ValueError("projection Pack is unselected or its artifact pin is stale")
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or path.as_posix() != relative
        or any(part in {".", ".."} for part in path.parts)
        or "\\" in relative
    ):
        raise ValueError("projection Pack content path is unsafe")
    root = source.root.absolute()
    for ancestor in (root, *root.parents):
        if ancestor.is_symlink():
            raise ValueError("projection Pack root contains a symlink")
    compiled = compile_pack_root(root)
    manifest = validate_file(root / "pack.v4.json", "pack")
    if (
        compiled.artifact.pack_id != pack_id
        or manifest["pack"]["kind"] != "normal_sandbox"
        or manifest["pack"]["artifact_digest"] != source.artifact_digest
    ):
        raise ValueError("projection Pack identity is stale or not a Normal Pack")
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("projection Pack content contains a symlink")
    if not current.is_dir():
        raise ValueError("projection Pack content root is unavailable")
    declared = {
        item["path"][len(relative) + 1 :]: item["digest"]
        for item in manifest["artifacts"]
        if item["path"].startswith(relative + "/")
    }
    if not declared:
        raise ValueError("projection Pack content has no declared artifacts")
    return current, source.artifact_digest, declared
