"""Derive bounded expanded input identities directly from pinned tar archives.

Only metadata and chunked hashes are retained; large SDK/toolchain members are
never expanded to disk or loaded into memory by verification.
"""

from __future__ import annotations

import hashlib
from collections import deque
from pathlib import Path, PurePosixPath
import stat
import tarfile
import zipfile

from build_native import fingerprint
from fetch_sdk import link_target, member_name


def _links(files: dict, directories: set[str], links: dict[str, str]) -> None:
    """Resolve literal links against the virtual tree, including directory hops."""
    for name in links:
        pending = deque(name.split("/"))
        resolved: list[str] = []
        active: set[str] = set()
        while pending:
            part = pending.popleft()
            if isinstance(part, tuple):
                active.remove(part[0])
                continue
            if part in ("", "."):
                continue
            if part == "..":
                if not resolved:
                    raise ValueError("archive link escapes root")
                resolved.pop()
                continue
            resolved.append(part)
            current = "/".join(resolved)
            if current in links:
                if current in active:
                    raise ValueError("archive link cycle")
                active.add(current)
                resolved.pop()
                pending.appendleft((current,))
                pending.extendleft(reversed(links[current].split("/")))
            elif current in files:
                if any(isinstance(item, str) for item in pending):
                    raise ValueError("archive link traverses a regular file")
            elif current not in directories:
                raise ValueError("archive link has a missing target")


def inspect(
    archive: Path,
    prefix: str,
    sha256: str,
    *,
    maximum: int,
    max_file: int,
    max_expanded: int,
    max_members: int,
    allow_links: bool = False,
    embedded_type_modes: bool = False,
    expected_bytes: int | None = None,
) -> dict:
    """Hash a pinned archive's complete safe expansion without trusting receipts."""
    if (
        not prefix
        or prefix in (".", "..")
        or "/" in prefix
        or "\\" in prefix
        or min(maximum, max_file, max_members) <= 0
        or max_expanded < 0
    ):
        raise ValueError("invalid archive inspection bounds or prefix")
    identity = fingerprint(archive, maximum)
    if identity["sha256"] != sha256 or (
        expected_bytes is not None and identity["bytes"] != expected_bytes
    ):
        raise ValueError("pinned archive identity differs")
    files: dict[str, dict] = {}
    kinds: dict[str, str] = {}
    directories = {""}
    symlinks: dict[str, str] = {}
    hardlinks: dict[str, str] = {}
    seen: set[str] = set()
    expanded = 0
    try:
        with tarfile.open(archive, "r|*") as bundle:
            for member in bundle:
                name = member_name(member.name, prefix)
                if name in seen or len(seen) >= max_members:
                    raise ValueError(
                        "duplicate archive member or member bound exceeded"
                    )
                seen.add(name)
                mode = member.mode
                if embedded_type_modes:
                    expected_type = stat.S_IFREG if member.isfile() else stat.S_IFDIR
                    if mode & ~(0o170000 | 0o777) or stat.S_IFMT(mode) not in (
                        0,
                        expected_type,
                    ):
                        raise ValueError("special archive mode")
                    mode &= 0o777
                if (
                    mode & ~0o777
                    or not 0 <= member.size <= max_file
                    or (not member.isfile() and member.size)
                ):
                    raise ValueError("archive mode or member size bound exceeded")
                if not name:
                    if not member.isdir():
                        raise ValueError("archive root is not a directory")
                    continue
                parents = {str(p) for p in PurePosixPath(name).parents if str(p) != "."}
                if any(kinds.get(parent, "dir") != "dir" for parent in parents):
                    raise ValueError("archive member traverses a non-directory")
                if name in directories and not member.isdir():
                    raise ValueError("archive file/directory collision")
                directories.update(parents)
                if member.isdir():
                    kinds[name] = "dir"
                    directories.add(name)
                elif member.isfile():
                    kinds[name] = "file"
                    expanded += member.size
                    if expanded > max_expanded:
                        raise ValueError("archive expanded size bound exceeded")
                    incoming = bundle.extractfile(member)
                    if incoming is None:
                        raise ValueError("missing archive member bytes")
                    digest = hashlib.sha256()
                    remaining = member.size
                    with incoming:
                        while remaining:
                            data = incoming.read(min(65536, remaining))
                            if not data:
                                raise ValueError("truncated archive member")
                            digest.update(data)
                            remaining -= len(data)
                    files[name] = {
                        "bytes": member.size,
                        "sha256": digest.hexdigest(),
                        "mode": oct(mode),
                    }
                elif allow_links and member.issym():
                    kinds[name] = "link"
                    link_target(name, member.linkname)
                    symlinks[name] = member.linkname
                elif allow_links and member.islnk():
                    kinds[name] = "link"
                    other = member_name(member.linkname, prefix)
                    if not other:
                        raise ValueError("archive hardlink targets root")
                    hardlinks[name] = other
                else:
                    raise ValueError("unsupported archive member kind")
    except (tarfile.TarError, EOFError) as error:
        raise ValueError("invalid pinned tar archive") from error
    if not seen:
        raise ValueError("empty archive")
    # fetch_sdk.extract only admits hardlinks to actual regular archive members.
    for name, target in hardlinks.items():
        if kinds.get(target) != "file":
            raise ValueError("archive hardlink requires a regular member")
        files[name] = dict(files[target])
    _links(files, directories, symlinks)
    files.update({name: {"link": target} for name, target in symlinks.items()})
    if fingerprint(archive, maximum) != identity:
        raise ValueError("pinned archive changed during inspection")
    return {
        "archive": identity,
        "files": dict(sorted(files.items())),
        "members": len(seen),
        "expanded_bytes": expanded,
        "directories": directories,
    }


def merge_component(merged: dict, inspected: dict, component: str) -> None:
    """Mirror fetch_toolchain.merge_component, omitting its install manifest."""
    prefix = component + "/"
    selected = {
        name[len(prefix) :]: entry
        for name, entry in inspected["files"].items()
        if name.startswith(prefix)
    }
    if "manifest.in" not in selected or "link" in selected["manifest.in"]:
        raise ValueError("Rust component has no regular installation inventory")
    component_dirs = {
        name[len(prefix) :]
        for name in inspected["directories"]
        if name.startswith(prefix)
    } | {""}
    # Component-relative resolution must not rely on a file outside the component.
    _links(
        {name: entry for name, entry in selected.items() if "link" not in entry},
        component_dirs,
        {name: entry["link"] for name, entry in selected.items() if "link" in entry},
    )
    directories = {""}
    for name in merged:
        directories.update(str(p) for p in PurePosixPath(name).parents if str(p) != ".")
    for name, entry in selected.items():
        if name == "manifest.in":
            continue
        parents = {
            str(parent) for parent in PurePosixPath(name).parents if str(parent) != "."
        }
        if parents & merged.keys():
            raise ValueError("Rust component merge traverses a non-directory")
        if name in directories:
            raise ValueError("Rust component output directory collision")
        if name in merged and merged[name] != entry:
            raise ValueError("Rust component output collision")
        merged[name] = dict(entry)
        directories.update(parents)
    _links(
        {name: entry for name, entry in merged.items() if "link" not in entry},
        directories,
        {name: entry["link"] for name, entry in merged.items() if "link" in entry},
    )


def inspect_wheel(
    archive: Path,
    sha256: str,
    *,
    maximum: int,
    expected_bytes: int,
    max_file: int,
    max_expanded: int,
    max_members: int,
) -> dict:
    """Mirror the validation-wheel extraction policy using streamed member hashes."""
    identity = fingerprint(archive, maximum)
    if identity != {"bytes": expected_bytes, "sha256": sha256}:
        raise ValueError("pinned validation wheel identity differs")
    files, seen, directories = {}, set(), {""}
    expanded = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            if len(bundle.infolist()) > max_members:
                raise ValueError("validation wheel entry bound exceeded")
            for member in bundle.infolist():
                name = member.filename.rstrip("/")
                if (
                    not name
                    or "\\" in name
                    or ":" in name
                    or "\0" in name
                    or PurePosixPath(name).is_absolute()
                    or any(p in ("", ".", "..") for p in name.split("/"))
                    or name.casefold() in seen
                ):
                    raise ValueError("unsafe or duplicate validation wheel path")
                seen.add(name.casefold())
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                allowed = (0, stat.S_IFDIR) if member.is_dir() else (0, stat.S_IFREG)
                if (
                    kind not in allowed
                    or mode & 0o7000
                    or member.flag_bits & 1
                    or not 0 <= member.file_size <= max_file
                ):
                    raise ValueError("unsafe validation wheel entry")
                parents = {str(p) for p in PurePosixPath(name).parents if str(p) != "."}
                if parents & files.keys() or (
                    name in directories and not member.is_dir()
                ):
                    raise ValueError("validation wheel file/directory collision")
                directories.update(parents)
                if member.is_dir():
                    if member.file_size:
                        raise ValueError("nonempty validation wheel directory")
                    directories.add(name)
                    continue
                expanded += member.file_size
                if expanded > max_expanded:
                    raise ValueError("validation wheel expanded bound exceeded")
                count, digest = 0, hashlib.sha256()
                with bundle.open(member) as incoming:
                    while data := incoming.read(
                        min(65536, member.file_size - count + 1)
                    ):
                        count += len(data)
                        if count > member.file_size:
                            raise ValueError("validation wheel member grew")
                        digest.update(data)
                if count != member.file_size:
                    raise ValueError("truncated validation wheel member")
                files[name] = {
                    "bytes": count,
                    "sha256": digest.hexdigest(),
                    "mode": "0o644",
                }
    except (zipfile.BadZipFile, EOFError) as error:
        raise ValueError("invalid pinned validation wheel") from error
    if fingerprint(archive, maximum) != identity:
        raise ValueError("pinned validation wheel changed during inspection")
    return files
