"""Owner-only file privacy checks for pinned SecureDirectory operations."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from core_runtime.hmac_key_manager import (
    SigningKeyError,
    _verify_windows_signing_key_acl,
)


def verify_private_file(path: Path, descriptor: int) -> None:
    """Verify a pinned regular file without changing existing access controls.

    The caller must retain the SecureDirectory parent and file handles. Windows
    paths then identify the exact no-reparse, non-replaceable open file; POSIX
    permission and ownership checks use that descriptor directly.
    """
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SigningKeyError("private file identity is unsafe")
    if os.name == "nt":
        _verify_windows_signing_key_acl(path)
    elif (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise SigningKeyError("private file must be owner-only")


def prepare_private_file(path: Path, descriptor: int) -> None:
    """Secure only a newly created empty file before any secret is written.

    SecureDirectory creates Windows temporaries with an owner-only protected
    DACL atomically, plus exclusive data access. This independently verifies
    that ACL before secret bytes are written; no inherited-ACL window exists.
    """
    if os.fstat(descriptor).st_size != 0:
        raise SigningKeyError("only an empty new private file may be secured")
    verify_private_file(path, descriptor)
