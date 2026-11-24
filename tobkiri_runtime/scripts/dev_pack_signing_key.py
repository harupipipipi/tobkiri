"""Create-once local development Pack signers outside runtime and Pack state.

Loading never creates, replaces, or repairs a key. The caller must retain the
public key ID from explicit initialization and supply it on subsequent loads.
This store provides signing continuity, not publisher trust or admission.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Iterator, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_KEY_FILE = "private.pem"
_IDENTITY_FILE = "identity.json"
_SCHEMA = "tobkiri.dev-pack-signer/v1"
_MAX_BYTES = 8192
_FORBIDDEN_PARTS = frozenset(
    {
        "user_data",
        "credentials",
        "native-credentials",
        "authority",
        "cas",
        "packvm",
        "packvm-vz",
        "vm",
        "dev-state",
        "generation-budget",
        "pack-artifacts-v4",
        "artifacts-v4",
        "retained-bundled-artifacts-v4",
    }
)


class DevSignerStoreError(ValueError):
    """A development signer is absent, unsafe, changed, or inconsistent."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DevSignerStoreError(message)


def _publisher(value: str) -> str:
    _require(
        isinstance(value, str) and 0 < len(value) <= 200,
        "publisher identity is required",
    )
    _require(
        value == value.strip() and all(ord(c) >= 33 for c in value),
        "publisher identity is invalid",
    )
    return value


def _store_path(store: Path, forbidden_roots: Sequence[Path]) -> Path:
    path = Path(store)
    _require(
        path.is_absolute() and len(path.parts) > 2,
        "DEV signer store requires an explicit absolute path",
    )
    _require(".." not in path.parts, "DEV signer store path traversal is denied")
    _require(
        not any(p.casefold() in _FORBIDDEN_PARTS for p in path.parts),
        "DEV signer store cannot use runtime, credential, CAS, or VM state",
    )
    for root in (REPOSITORY_ROOT, *forbidden_roots):
        boundary = Path(root).absolute()
        candidate = tuple(part.casefold() for part in path.parts)
        prefix = tuple(part.casefold() for part in boundary.parts)
        _require(
            candidate[: len(prefix)] != prefix,
            "DEV signer store must be outside the repository and caller roots",
        )
    return path


def _no_extended_acl(descriptor: int) -> None:
    if sys.platform != "darwin":
        return
    library = ctypes.CDLL(None, use_errno=True)
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(descriptor, 0x100)
    if acl:
        _require(library.acl_free(acl) == 0, "DEV signer ACL release failed")
        raise DevSignerStoreError("DEV signer cannot have extended ACL permissions")
    _require(ctypes.get_errno() in (0, errno.ENOENT), "DEV signer ACL read failed")


def _outside_boundaries(
    directories: _Directories,
    forbidden_roots: Sequence[Path],
) -> None:
    entered = {expected[:2] for _fd, _name, expected in directories.records}
    for root in (REPOSITORY_ROOT, *forbidden_roots):
        try:
            value = Path(root).stat()
        except FileNotFoundError:
            continue
        _require(
            (value.st_dev, value.st_ino) not in entered,
            "DEV signer directory aliases a forbidden caller root",
        )


def _directory_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        value.st_mode,
        getattr(value, "st_flags", 0),
    )


def _file_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        *_directory_signature(value),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


class _Directories:
    """Keep no-follow ancestor descriptors until the operation finishes."""

    def __init__(self, path: Path) -> None:
        _require(
            os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
            "DEV signer requires POSIX no-follow descriptors",
        )
        self.records: list[tuple[int, str | None, tuple[int, ...]]] = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            descriptor = os.open("/", flags)
            self.records.append(
                (descriptor, None, _directory_signature(os.fstat(descriptor)))
            )
            for name in path.parts[1:]:
                descriptor = os.open(name, flags, dir_fd=self.records[-1][0])
                value = os.fstat(descriptor)
                self.records.append((descriptor, name, _directory_signature(value)))
                _require(
                    value.st_uid in (0, os.geteuid()),
                    "DEV signer ancestor has a foreign owner",
                )
                _require(
                    not value.st_mode & 0o022 or value.st_mode & stat.S_ISVTX,
                    "DEV signer ancestor is writable by other users",
                )
        except OSError as error:
            self.close()
            raise DevSignerStoreError(
                "DEV signer path is unavailable or unsafe"
            ) from error
        except BaseException:
            self.close()
            raise

    @property
    def descriptor(self) -> int:
        _require(bool(self.records), "DEV signer operation scope is closed")
        return self.records[-1][0]

    def append(self, name: str) -> None:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.descriptor
        )
        self.records.append(
            (descriptor, name, _directory_signature(os.fstat(descriptor)))
        )

    def assert_current(self) -> None:
        _require(bool(self.records), "DEV signer operation scope is closed")
        for index, (descriptor, name, expected) in enumerate(self.records):
            _require(
                _directory_signature(os.fstat(descriptor)) == expected,
                "DEV signer directory identity or permissions changed",
            )
            if index:
                named = os.stat(
                    name, dir_fd=self.records[index - 1][0], follow_symlinks=False
                )
                _require(
                    _directory_signature(named) == expected,
                    "DEV signer directory path changed",
                )

    def close(self) -> None:
        while self.records:
            os.close(self.records.pop()[0])


def _owned_store(value: os.stat_result) -> None:
    _require(
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.geteuid()
        and stat.S_IMODE(value.st_mode) == 0o700,
        "DEV signer directory must be owned and mode 0700",
    )


def _owned_file(value: os.stat_result) -> None:
    _require(
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == 0o600,
        "DEV signer files must be owned single-link regular files mode 0600",
    )
    _require(0 < value.st_size <= _MAX_BYTES, "DEV signer file size is invalid")


def _public_identity(key: Ed25519PrivateKey, publisher_id: str) -> dict[str, str]:
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(public).hexdigest()
    return {
        "publisher_id": publisher_id,
        "key_id": digest[:32],
        "fingerprint": f"sha256:{digest}",
        "public_key_base64": base64.b64encode(public).decode("ascii"),
    }


def _write_once(directory: _Directories, name: str, raw: bytes) -> None:
    directory.assert_current()
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory.descriptor,
    )
    try:
        offset = 0
        while offset < len(raw):
            count = os.write(descriptor, raw[offset:])
            _require(count > 0, "DEV signer write did not complete")
            offset += count
        os.fsync(descriptor)
        _owned_file(os.fstat(descriptor))
        _require(
            _file_signature(
                os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
            )
            == _file_signature(os.fstat(descriptor)),
            "DEV signer file path changed",
        )
        directory.assert_current()
    finally:
        os.close(descriptor)


def initialize_dev_signer_store(
    store: Path,
    *,
    publisher_id: str,
    forbidden_roots: Sequence[Path] = (),
) -> dict[str, str]:
    """Explicitly create one signer; existing or partial stores are never replaced.

    The parent must already exist and be caller-owned without group/other write
    permission. A failed partial initialization remains for deliberate recovery;
    neither initialization nor loading silently discards or regenerates it.
    """
    path = _store_path(store, forbidden_roots)
    publisher = _publisher(publisher_id)
    directories = _Directories(path.parent)
    try:
        parent = os.fstat(directories.descriptor)
        _outside_boundaries(directories, forbidden_roots)
        _no_extended_acl(directories.descriptor)
        _require(
            parent.st_uid == os.geteuid() and not parent.st_mode & 0o022,
            "DEV signer parent must be owned and not writable by others",
        )
        directories.assert_current()
        os.mkdir(path.name, 0o700, dir_fd=directories.descriptor)
        os.fsync(directories.descriptor)
        directories.append(path.name)
        _owned_store(os.fstat(directories.descriptor))
        _no_extended_acl(directories.descriptor)
        directories.assert_current()
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public = _public_identity(key, publisher)
        _write_once(directories, _KEY_FILE, raw)
        metadata = {
            "schema": _SCHEMA,
            "algorithm": "Ed25519",
            **public,
            "private_key_sha256": hashlib.sha256(raw).hexdigest(),
        }
        _write_once(
            directories,
            _IDENTITY_FILE,
            (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        os.fsync(directories.descriptor)
        directories.assert_current()
        return {k: public[k] for k in ("publisher_id", "key_id", "fingerprint")}
    except (OSError, UnicodeError) as error:
        raise DevSignerStoreError(
            "DEV signer initialization failed; no replacement"
        ) from error
    finally:
        directories.close()


@dataclass(frozen=True)
class DevPackSigner:
    """One operation-bound signer with public identity and fresh file fences."""

    publisher_id: str
    key_id: str
    fingerprint: str
    private_key: Ed25519PrivateKey = field(repr=False)
    _directories: _Directories = field(repr=False)
    _files: tuple[tuple[str, int, tuple[int, ...]], ...] = field(repr=False)

    def assert_current(self) -> None:
        """Reject path, permissions, hardlink, content, or lifetime drift."""
        self._directories.assert_current()
        _owned_store(os.fstat(self._directories.descriptor))
        _no_extended_acl(self._directories.descriptor)
        _require(
            set(os.listdir(self._directories.descriptor))
            == {_KEY_FILE, _IDENTITY_FILE},
            "DEV signer store inventory changed",
        )
        for name, descriptor, expected in self._files:
            opened = os.fstat(descriptor)
            _owned_file(opened)
            _no_extended_acl(descriptor)
            _require(
                _file_signature(opened) == expected,
                "DEV signer file changed during signing",
            )
            named = os.stat(
                name, dir_fd=self._directories.descriptor, follow_symlinks=False
            )
            _require(
                _file_signature(named) == expected,
                "DEV signer file path changed during signing",
            )


def _read_file(directories: _Directories, name: str) -> tuple[bytes, int, tuple]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directories.descriptor
    )
    try:
        before = os.fstat(descriptor)
        _owned_file(before)
        _no_extended_acl(descriptor)
        chunks = bytearray()
        while block := os.read(descriptor, _MAX_BYTES + 1):
            chunks.extend(block)
            _require(len(chunks) <= _MAX_BYTES, "DEV signer file exceeds its bound")
        _require(
            _file_signature(os.fstat(descriptor)) == _file_signature(before),
            "DEV signer file changed while read",
        )
        _require(
            _file_signature(
                os.stat(name, dir_fd=directories.descriptor, follow_symlinks=False)
            )
            == _file_signature(before),
            "DEV signer file name changed while read",
        )
        return bytes(chunks), descriptor, _file_signature(before)
    except BaseException:
        os.close(descriptor)
        raise


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        _require(key not in result, "DEV signer metadata has duplicate fields")
        result[key] = value
    return result


@contextmanager
def open_dev_signer(
    store: Path,
    *,
    publisher_id: str,
    expected_key_id: str,
    forbidden_roots: Sequence[Path] = (),
) -> Iterator[DevPackSigner]:
    """Load an existing exact signer without creating or repairing any state."""
    path = _store_path(store, forbidden_roots)
    publisher = _publisher(publisher_id)
    _require(
        isinstance(expected_key_id, str)
        and re.fullmatch(r"[0-9a-f]{32}", expected_key_id) is not None,
        "the previously recorded DEV signer key ID is required",
    )
    directories = _Directories(path)
    files: list[tuple[str, int, tuple]] = []
    try:
        _owned_store(os.fstat(directories.descriptor))
        _outside_boundaries(directories, forbidden_roots)
        _no_extended_acl(directories.descriptor)
        _require(
            set(os.listdir(directories.descriptor)) == {_KEY_FILE, _IDENTITY_FILE},
            "DEV signer store is incomplete",
        )
        private, fd, stamp = _read_file(directories, _KEY_FILE)
        files.append((_KEY_FILE, fd, stamp))
        metadata_raw, fd, stamp = _read_file(directories, _IDENTITY_FILE)
        files.append((_IDENTITY_FILE, fd, stamp))
        try:
            metadata = json.loads(metadata_raw, object_pairs_hook=_unique_object)
            key = serialization.load_pem_private_key(private, password=None)
        except (ValueError, TypeError, UnicodeError):
            raise DevSignerStoreError("DEV signer key or metadata is corrupt") from None
        _require(isinstance(key, Ed25519PrivateKey), "DEV signer requires Ed25519")
        public = _public_identity(key, publisher)
        expected = {
            "schema": _SCHEMA,
            "algorithm": "Ed25519",
            **public,
            "private_key_sha256": hashlib.sha256(private).hexdigest(),
        }
        _require(metadata == expected, "DEV signer identity or key material changed")
        _require(
            public["key_id"] == expected_key_id,
            "DEV signer differs from the previously recorded key ID",
        )
        signer = DevPackSigner(
            publisher,
            public["key_id"],
            public["fingerprint"],
            key,
            directories,
            tuple(files),
        )
        signer.assert_current()
        yield signer
        signer.assert_current()
    except (OSError, UnicodeError) as error:
        raise DevSignerStoreError(
            "DEV signer is unavailable or changed; not regenerated"
        ) from error
    finally:
        for _name, descriptor, _stamp in files:
            os.close(descriptor)
        directories.close()
