"""Build the root guest runner and its finite pure support closure as a zipapp.

The existing guest-runner digest covers the entire archive. No credentials,
Host dispatcher, third-party packages or ambient source discovery are included.
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import stat
import tempfile
import zipfile

_SOURCES = {
    "tobkiri_host/bounded_child_io.py": "tobkiri_host/bounded_child_io.py",
    "__main__.py": "ecosystem/defaultspack/backend/sandbox/isolation/resources/packvm_guest_runner.py",
    "tobkiri_host/continuation_chain.py": "tobkiri_host/continuation_chain.py",
    "tobkiri_host/continuation_envelope.py": "tobkiri_host/continuation_envelope.py",
    "tobkiri_host/continuation_session.py": "tobkiri_host/continuation_session.py",
    "tobkiri_host/saved_guest_dispatch.py": "tobkiri_host/saved_guest_dispatch.py",
    "tobkiri_protocol/canonical.py": "tobkiri_protocol/canonical.py",
    "tobkiri_protocol/errors.py": "tobkiri_protocol/errors.py",
    "tobkiri_protocol/saved_conversation.py": "tobkiri_protocol/saved_conversation.py",
}
_PACKAGES = ("tobkiri_host/__init__.py", "tobkiri_protocol/__init__.py")


def build_guest_bundle(runtime_root: Path) -> bytes:
    """Capture the exact source allowlist into deterministic interpreter input."""
    root = runtime_root.resolve(strict=True)
    members = dict.fromkeys(_PACKAGES, b"")
    for name, relative in _SOURCES.items():
        source = root / relative
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise ValueError("guest bundle sources must not be symlinks")
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("guest bundle source must be a single-link regular file")
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("guest bundle source exceeds size limit")
        compile(content, relative, "exec")
        members[name] = content
    output = io.BytesIO()
    # Stored members avoid reliance on the guest's optional zlib installation.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(members.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o444) << 16
            archive.writestr(entry, content)
    return output.getvalue()


def main() -> int:
    """Write or verify one explicitly selected packaging output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = build_guest_bundle(args.runtime_root)
    destination = args.output
    if destination.is_symlink() or any(path.is_symlink() for path in destination.parents):
        raise ValueError("guest bundle destination must not be a symlink")
    if destination.exists():
        metadata = destination.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("guest bundle destination must be a single-link regular file")
    if args.check:
        if destination.read_bytes() != content:
            raise ValueError("guest bundle does not match its canonical source closure")
        return 0
    descriptor, name = tempfile.mkstemp(dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
