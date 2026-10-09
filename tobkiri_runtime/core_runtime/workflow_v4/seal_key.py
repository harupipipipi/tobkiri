"""Platform-aware private binary keys for authenticated Workflow state."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from core_runtime.hmac_key_manager import (
    SigningKeyError,
    _secure_windows_signing_key,
    _verify_windows_signing_key_acl,
)
from core_runtime.secure_sqlite_path import (
    FileIdentity, SecureParent, SecurePathError, secure_parent,
)
from .models import WorkflowDenied


@contextmanager
def _publication_lock(parent: SecureParent) -> Iterator[None]:
    # POSIX link+unlink publication briefly has two names. Serialize all key
    # readers in this parent so they never accept or observe that intermediate
    # multi-link state. Windows uses one atomic no-replace MoveFileExW.
    if os.name == "nt" or parent.descriptor is None:
        yield
        return
    import fcntl

    fcntl.flock(parent.descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(parent.descriptor, fcntl.LOCK_UN)


def _verify_private(parent: SecureParent, name: str) -> FileIdentity:
    metadata = parent.stat_file(name, required=True)
    assert metadata is not None
    identity = FileIdentity.from_stat(metadata)
    if os.name == "nt":
        # Windows stat permission bits are synthetic, not access-control proof.
        # Reuse the Host's existing exact current-user, protected-DACL verifier.
        _verify_windows_signing_key_acl(parent.path / name)
    elif stat.S_IMODE(metadata.st_mode) & 0o077:
        raise WorkflowDenied("workflow seal key permissions are too broad")
    parent.validate_open(name, required=True, expected=identity)
    parent.assert_path_continuity()
    return identity


def _read(parent: SecureParent, name: str, *, expected: FileIdentity | None = None) -> bytes:
    identity = _verify_private(parent, name)
    if expected is not None and identity != expected:
        raise SecurePathError("published workflow key identity changed")
    descriptor = parent.open_file(name, os.O_RDONLY)
    try:
        if FileIdentity.from_stat(os.fstat(descriptor)) != identity:
            raise SecurePathError("workflow key changed while opening")
        value = os.read(descriptor, 33)
        if len(value) != 32:
            raise WorkflowDenied("workflow seal key is invalid")
    finally:
        os.close(descriptor)
    if _verify_private(parent, name) != identity:
        raise SecurePathError("workflow key changed while reading")
    return value


def load_or_create_workflow_key(path: Path, *, database_path: Path) -> bytes:
    """Read a private 32-byte key, or atomically publish one without overwrite.

    Existing unsafe keys are never silently chmodded, re-ACLed or replaced.
    A missing key for an existing database requires explicit recovery.
    """
    if path.parent != database_path.parent:
        raise WorkflowDenied("workflow key and database must share their owner directory")
    try:
        with secure_parent(path) as parent, _publication_lock(parent):
            if parent.stat_file(path.name, required=False) is not None:
                return _read(parent, path.name)
            database = parent.stat_file(database_path.name, required=False)
            if database is not None and database.st_size:
                if parent.stat_file(path.name, required=False) is not None:
                    return _read(parent, path.name)
                raise WorkflowDenied("workflow seal key is missing for existing state")
            temporary = f".workflow-key-{secrets.token_hex(16)}.tmp"
            identity = parent.create_empty_file(temporary)
            try:
                if os.name == "nt":
                    # Only the newly created empty file is hardened, before any
                    # secret bytes are written. Published ACLs are then checked.
                    _secure_windows_signing_key(parent.path / temporary)
                parent.validate_open(temporary, required=True, expected=identity)
                _verify_private(parent, temporary)
                descriptor = parent.open_file(temporary, os.O_WRONLY)
                try:
                    if FileIdentity.from_stat(os.fstat(descriptor)) != identity:
                        raise SecurePathError("workflow key changed before writing")
                    value = secrets.token_bytes(32)
                    with os.fdopen(os.dup(descriptor), "wb") as output:
                        output.write(value)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    os.close(descriptor)
                parent.validate_open(temporary, required=True, expected=identity)
                published_identity: FileIdentity | None = identity
                try:
                    parent.publish_new_file(temporary, path.name)
                except FileExistsError:
                    # Another creator won. Use its verified key; never overwrite.
                    published_identity = None
                return _read(parent, path.name, expected=published_identity)
            finally:
                if parent.stat_file(temporary, required=False) is not None:
                    parent.validate_open(temporary, required=True, expected=identity)
                    parent.unlink_file(temporary, missing_ok=False)
    except WorkflowDenied:
        raise
    except (OSError, SecurePathError, SigningKeyError) as error:
        raise WorkflowDenied("workflow seal key storage is unsafe") from error
