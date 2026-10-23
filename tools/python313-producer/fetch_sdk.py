"""Acquire the exact historical SDK on CI, with bounded private extraction.

SDK limits are independent of the smaller source-archive bounds. No downloaded
program runs here. Links are created last and must resolve inside the new tree.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from build_native import fingerprint

PREFIX = "wasi-sdk-33.0-x86_64-linux"
URL = f"https://github.com/WebAssembly/wasi-sdk/releases/download/wasi-sdk-33/{PREFIX}.tar.gz"
SHA256 = "0ba8b5bfaeb2adf3f29bab5841d76cf5318ab8e1642ea195f88baba1abd47bce"
ARCHIVE_BYTES = 193563606
MAX_FILE = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 ** 3
MAX_MEMBERS = 50000
DISK_FLOOR = 8 * 1024 ** 3


def member_name(raw: str, prefix: str = PREFIX) -> str:
    """Require a canonical archive name within the one fixed SDK prefix."""
    parts = raw.rstrip("/").split("/")
    if ("\\" in raw or parts[0] != prefix
            or any(p in ("", ".", "..") for p in parts)):
        raise ValueError("unsafe SDK member path")
    return "/".join(parts[1:])


def link_target(name: str, raw: str) -> str:
    """Normalize an SDK-relative symlink without permitting an outward hop."""
    if not raw or raw.startswith("/") or "\\" in raw:
        raise ValueError("unsafe SDK link")
    parts = list(PurePosixPath(name).parent.parts)
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError("SDK link escapes root")
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def extract(archive: Path, root: Path, *, prefix: str = PREFIX,
            max_expanded: int = MAX_EXPANDED) -> dict:
    """Extract only regular files/directories and finally checked internal links."""
    if (not 0 <= max_expanded <= MAX_EXPANDED or not prefix or prefix in (".", "..")
            or "/" in prefix or "\\" in prefix):
        raise ValueError("invalid bounded archive extraction parameters")
    root.mkdir(mode=0o700)
    seen = set()
    regular_names = set()
    links = []
    expanded = 0
    with tarfile.open(archive, "r|*") as bundle:
        for member in bundle:
            name = member_name(member.name, prefix)
            if name in seen or len(seen) >= MAX_MEMBERS:
                raise ValueError("duplicate SDK member or member bound exceeded")
            seen.add(name)
            if (member.mode & ~0o777 or not 0 <= member.size <= MAX_FILE
                    or (not member.isfile() and member.size)):
                raise ValueError("SDK mode or member size bound exceeded")
            if not name:
                if not member.isdir():
                    raise ValueError("SDK root is not a directory")
                continue
            target = root / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                expanded += member.size
                if expanded > max_expanded:
                    raise ValueError("SDK expanded size bound exceeded")
                target.parent.mkdir(parents=True, exist_ok=True)
                incoming = bundle.extractfile(member)
                if incoming is None:
                    raise ValueError("missing SDK member bytes")
                remaining = member.size
                with incoming, target.open("xb") as output:
                    while remaining:
                        data = incoming.read(min(65536, remaining))
                        if not data:
                            raise ValueError("truncated SDK member")
                        output.write(data)
                        remaining -= len(data)
                target.chmod(member.mode)
                regular_names.add(name)
            elif member.issym():
                link_target(name, member.linkname)
                links.append((name, member.linkname, False))
            elif member.islnk():
                other = member_name(member.linkname, prefix)
                if not other:
                    raise ValueError("SDK hardlink targets root")
                links.append((name, other, True))
            else:
                raise ValueError("unsupported SDK member kind")
    if not seen:
        raise ValueError("empty SDK archive")
    # No path is traversed through an archive-created link while writing files.
    # Reject links colliding with implicit parents before creating any links.
    link_names = {name for name, _, _ in links}
    for name, _, _ in links:
        target = root / name
        if target.exists() or any(str(p) in link_names
                                  for p in PurePosixPath(name).parents):
            raise ValueError("SDK link collides with an ancestor or directory")
    for name, other, hard in links:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if hard:
            original = root / other
            if other not in regular_names or not stat.S_ISREG(original.lstat().st_mode):
                raise ValueError("SDK hardlink requires an existing regular file")
            os.link(original, target, follow_symlinks=False)
        else:
            target.symlink_to(other)
    # Lexical checks alone are insufficient with chained directory symlinks.
    for name, _, _ in links:
        try:
            (root / name).resolve(strict=True).relative_to(root)
        except (ValueError, RuntimeError, OSError) as error:
            raise ValueError("SDK link is missing, cyclic or outside root") from error
    return inventory(root)


def inventory(root: Path) -> dict:
    """Record actual immutable inputs, including the literal internal links."""
    entries = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            path.resolve(strict=True).relative_to(root)
            entries[name] = {"link": os.readlink(path)}
        elif stat.S_ISREG(mode):
            entries[name] = {**fingerprint(path, MAX_FILE),
                             "mode": oct(stat.S_IMODE(mode))}
        elif not stat.S_ISDIR(mode):
            raise ValueError("non-file SDK input")
    return entries


def capture(stream, output) -> None:
    """Bind complete archive length and digest before parsing or executing it."""
    digest = hashlib.sha256()
    count = 0
    while data := stream.read(min(65536, ARCHIVE_BYTES - count + 1)):
        count += len(data)
        if count > ARCHIVE_BYTES:
            raise ValueError("SDK archive size exceeds recorded bytes")
        digest.update(data)
        output.write(data)
    if count != ARCHIVE_BYTES or digest.hexdigest() != SHA256:
        raise ValueError("SDK archive differs from recorded identity")


def acquire(destination: Path) -> tuple[Path, dict]:
    """Create a fresh SDK tree after verifying the recorded upstream archive."""
    if (not destination.is_absolute()
            or destination.parent.resolve() != destination.parent):
        raise ValueError("absolute destination under physical parent required")
    if (shutil.disk_usage(destination.parent).free
            < DISK_FLOOR + MAX_EXPANDED + ARCHIVE_BYTES):
        raise ValueError("SDK acquisition disk floor not met")
    destination.mkdir(mode=0o700)
    archive = destination / "sdk.tar.gz"
    request = Request(URL, headers={"User-Agent": "Tobkiri-source-replay"})
    with urlopen(request, timeout=60) as incoming, archive.open("xb") as output:
        if urlsplit(incoming.geturl()).scheme != "https":
            raise ValueError("SDK redirect must remain HTTPS")
        capture(incoming, output)
    root = destination / "sdk"
    entries = extract(archive, root)
    receipt = {"format": "tobkiri.python313-sdk-inputs/1", "url": URL,
               "archive_sha256": SHA256, "archive_bytes": ARCHIVE_BYTES,
               "files": entries, "tool_execution": False}
    (destination / "sdk-manifest.json").write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return root, entries
