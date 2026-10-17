"""Retain authenticated bundled Host baselines for admitted-revision reads.

These public bytes are compatibility evidence, never selected execution roots.
The existing active origin keeps its exact pin rules. A baseline is instead
anchored by that origin's authenticated raw bundle-lock digest and projection.
New admission does not use this read scope or its separate immutable cache.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import threading
from typing import Any, Iterator, Mapping

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.ids import validate_artifact_digest, validate_canonical_id
from tobkiri_protocol.secure_persistence import SecureDirectory
from tobkiri_protocol.validation import validate_document

from .bundled_artifact_retention_v4 import (
    BundledArtifactOrigin,
    BundledArtifactRetentionDenied,
    _MAX_FILES,
    _MAX_METADATA_BYTES,
    _MAX_TOTAL_BYTES,
    _digest,
    _identity,
    _projection,
    _read,
    _relative,
    _rename_noreplace,
    assert_retention_origin_current,
)
from .host_extension_update_v4 import _verified_pack
from .pack_boundary import load_pack_catalog, resolve_pack_root

_STORE = "retained-bundled-baselines-v4"
_TOKEN = object()
_CURRENT: ContextVar[BundledHostBaselineRead | None] = ContextVar(
    "bundled_host_baseline_read", default=None
)


def _deny(condition: bool, message: str) -> None:
    if condition:
        raise BundledArtifactRetentionDenied(message)


def _require_owner(origin: BundledArtifactOrigin) -> None:
    from .bootstrap.profile_capture import runtime_user_data_root

    _deny(
        not isinstance(origin, BundledArtifactOrigin),
        "bundled baseline requires a captured origin",
    )
    _deny(
        runtime_user_data_root().resolve(strict=True) != origin.user_data_root.resolve(strict=True),
        "bundled baseline belongs to another Host owner",
    )
    assert_retention_origin_current(origin)


def _baseline_projection(
    origin: BundledArtifactOrigin, raw_lock: bytes, raw: bytes, pack_id: str
) -> dict[str, Any]:
    _projection(raw_lock, lambda _path: raw, pack_id, origin)
    projection = validate_document(raw, "pack")
    provenance = projection["provenance"]
    _deny(
        projection["pack"]["id"] != pack_id
        or projection["pack"]["kind"] != "host_extension"
        or projection["requirements"]["execution_boundary"] != "host_brokered"
        or provenance.get("schema") != "io.tobkiri.provenance.v2"
        or provenance.get("source_kind") != "generated"
        or provenance.get("normative") is not True
        or provenance.get("source_path") != f"ecosystem/{pack_id}/pack.v4.json"
        or provenance.get("source_digest") != projection["integrity"]["source_identity"],
        "bundled Host baseline projection is invalid",
    )
    return projection


def _capture_files(
    origin: BundledArtifactOrigin,
    pack_id: str,
    raw_lock: bytes,
    raw_projection: bytes,
    raw_record: bytes,
    root: Path,
) -> dict[str, bytes]:
    projection = _baseline_projection(origin, raw_lock, raw_projection, pack_id)
    reader = SecureDirectory(root, create=False)
    raw_manifest = _read(reader, "pack.v4.json", metadata=True)
    _deny(
        _digest(raw_manifest) != projection["integrity"]["source_identity"],
        "full bundled baseline manifest differs from its authenticated projection",
    )
    verified = _verified_pack(root)
    manifest = verified.documents["pack.v4.json"]
    _deny(
        verified.compiled.artifact.pack_id != pack_id
        or verified.compiled.artifact.digest != projection["pack"]["artifact_digest"]
        or manifest["pack"]["version"] != projection["pack"]["version"],
        "full bundled baseline identity differs from its projection",
    )
    record = strict_loads(raw_record)
    _deny(
        len(raw_record) > _MAX_METADATA_BYTES
        or not isinstance(record, dict)
        or canonical_digest(record) != manifest["integrity"]["source_identity"]
        or record.get("pack_id") != pack_id
        or record.get("kind") != "host_extension"
        or record.get("version") != manifest["pack"]["version"],
        "bundled baseline canonical source record differs",
    )
    runtime = record.get("runtime_artifacts")
    _deny(
        not isinstance(runtime, list) or not 0 < len(runtime) <= _MAX_FILES - 3,
        "bundled baseline runtime inventory is invalid",
    )
    expected = {}
    for item in runtime:
        relative = _relative(item["path"])
        _deny(
            relative in expected
            or relative
            in {
                "pack.v4.json",
                "contracts.v4.json",
                "executables.v4.json",
                "artifact-index.v4.json",
            },
            "baseline runtime path is duplicate or reserved",
        )
        validate_artifact_digest(item["digest"], field="runtime digest")
        expected[relative] = {"digest": item["digest"], "role": item.get("index_role", "runtime")}
    artifacts = {item["path"]: item["digest"] for item in manifest["artifacts"]}
    _deny(
        len(artifacts) != len(manifest["artifacts"])
        or set(artifacts) != set(expected) | {"executables.v4.json"}
        or any(artifacts[path] != item["digest"] for path, item in expected.items()),
        "baseline manifest runtime inventory differs",
    )
    expected.update(
        {
            "pack.v4.json": {"digest": _digest(raw_manifest), "role": "canonical_manifest"},
            "contracts.v4.json": {
                "digest": manifest["integrity"]["contract_catalog_digest"],
                "role": "contract_catalog",
            },
            "executables.v4.json": {"digest": artifacts["executables.v4.json"], "role": "sidecar"},
        }
    )
    raw_index = _read(reader, "artifact-index.v4.json", metadata=True)
    index = validate_document(raw_index, "pack_artifact_index")
    actual = {
        item["path"]: {"digest": item["digest"], "role": item["role"]}
        for item in index["artifacts"]
    }
    _deny(
        len(actual) != len(index["artifacts"])
        or actual != expected
        or index["pack_id"] != pack_id
        or index["source_identity"] != manifest["integrity"]["source_identity"],
        "baseline artifact index inventory or roles differ",
    )
    files = {"artifact-index.v4.json": raw_index}
    total = len(raw_index) + len(raw_lock) + len(raw_projection) + len(raw_record)
    _deny(total > _MAX_TOTAL_BYTES, "baseline metadata exceeds limits")
    for path, item in expected.items():
        content = _read(reader, path)
        total += len(content)
        _deny(
            total > _MAX_TOTAL_BYTES or _digest(content) != item["digest"],
            "bundled baseline bytes changed or exceed limits",
        )
        files[path] = content
    _deny(files.get("pack.v4.json") != raw_manifest, "baseline manifest changed")
    _verified_pack(root)
    _deny(
        _read(reader, "pack.v4.json", metadata=True) != raw_manifest
        or _read(reader, "artifact-index.v4.json", metadata=True) != raw_index,
        "bundled baseline manifest changed during capture",
    )
    return files


def _container(origin: BundledArtifactOrigin, pack_id: str, digest: str) -> Path:
    validate_canonical_id(pack_id, field="pack_id")
    validate_artifact_digest(digest, field="baseline digest")
    return Path(_STORE) / origin.bundle_digest[7:] / digest[7:] / pack_id


def _verify_tree(reader: SecureDirectory, wanted: set[str], *, sealed: bool) -> None:
    directories = {
        parent.as_posix()
        for name in wanted
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    _deny(len(wanted) + len(directories) > _MAX_FILES + 16, "baseline tree exceeds limits")
    observed: set[str] = set()
    observed_directories: set[str] = set()

    def inspect(descriptor: int, prefix: str = "") -> None:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                path = f"{prefix}/{entry.name}" if prefix else entry.name
                metadata = entry.stat(follow_symlinks=False)
                directory = stat.S_ISDIR(metadata.st_mode)
                _deny(
                    path not in (directories if directory else wanted)
                    or (not directory and not stat.S_ISREG(metadata.st_mode))
                    or (not directory and metadata.st_nlink != 1)
                    or metadata.st_uid != os.getuid()
                    or (
                        sealed and stat.S_IMODE(metadata.st_mode) != (0o500 if directory else 0o400)
                    ),
                    "bundled baseline tree contains extra or unsafe entries",
                )
                if directory:
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    try:
                        opened = os.fstat(child)
                        _deny(
                            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino),
                            "baseline directory changed",
                        )
                        observed_directories.add(path)
                        inspect(child, path)
                    finally:
                        os.close(child)
                else:
                    observed.add(path)

    with reader._root_descriptor() as descriptor:
        _deny(
            sealed and stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o500,
            "baseline root is not sealed",
        )
        inspect(descriptor)
    _deny(
        observed != wanted or observed_directories != directories,
        "baseline tree has missing entries",
    )


def _seal(descriptor: int, *, seal_root: bool = True) -> None:
    """Seal the same pinned entries without blocking on a swapped FIFO."""
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        directory = stat.S_ISDIR(metadata.st_mode)
        _deny(
            not directory and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1),
            "baseline publication entry is unsafe",
        )
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
        child = os.open(name, flags, dir_fd=descriptor)
        try:
            opened = os.fstat(child)
            _deny(
                (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino),
                "baseline publication entry changed",
            )
            if directory:
                _seal(child)
            os.fchmod(child, 0o500 if directory else 0o400)
            os.fsync(child)
        finally:
            os.close(child)
    if seal_root:
        os.fchmod(descriptor, 0o500)
    os.fsync(descriptor)


def _verify_stored(
    origin: BundledArtifactOrigin, pack_id: str, container: Path, *, sealed: bool = True
) -> Path:
    reader = SecureDirectory(container, create=False)
    raw_lock = _read(reader, "origin/bundle.lock.json", metadata=True)
    projection = _read(reader, "origin/projection.json", metadata=True)
    record = _read(reader, "origin/source-record.json", metadata=True)
    root = container / "artifact" / pack_id
    files = _capture_files(origin, pack_id, raw_lock, projection, record, root)
    wanted = {f"artifact/{pack_id}/{path}" for path in files} | {
        "origin/bundle.lock.json",
        "origin/projection.json",
        "origin/source-record.json",
    }
    _verify_tree(reader, wanted, sealed=sealed)
    _deny(
        _read(reader, "origin/bundle.lock.json", metadata=True) != raw_lock, "baseline lock changed"
    )
    return root


@dataclass(frozen=True)
class RetainedBundledHostBaseline:
    """Verified public compatibility bytes; never an execution authorization."""

    pack_id: str
    artifact_digest: str
    bundle_digest: str
    root: Path


def retain_bundled_host_baseline(
    origin: BundledArtifactOrigin,
    *,
    bundle_root: Path,
    source_root: Path,
    source_record: Mapping[str, Any],
) -> RetainedBundledHostBaseline:
    """Verify explicit untrusted suppliers and publish without clobbering.

    Only the separate public baseline cache is written. No admission, active
    pin, approval, key, credential, budget, or Authority row is changed.
    """
    _deny(os.name != "posix", "baseline retention requires POSIX primitives")
    _require_owner(origin)
    source = SecureDirectory(Path(source_root), create=False)
    manifest = validate_document(_read(source, "pack.v4.json", metadata=True), "pack")
    pack_id = str(manifest["pack"]["id"])
    bundle = SecureDirectory(Path(bundle_root), create=False)
    raw_lock = _read(bundle, "bundle.lock.json", metadata=True)
    projection = _projection(
        raw_lock, lambda path: _read(bundle, path, metadata=True), pack_id, origin
    )
    record = canonical_json(dict(source_record))
    files = _capture_files(origin, pack_id, raw_lock, projection, record, Path(source_root))
    _deny(_read(bundle, "bundle.lock.json", metadata=True) != raw_lock, "baseline bundle changed")
    _require_owner(origin)
    relative = _container(origin, pack_id, str(manifest["pack"]["artifact_digest"]))
    owner = SecureDirectory(origin.user_data_root, create=False)
    with owner._parent_descriptor(relative, create=True) as (parent_fd, name):
        parent = origin.user_data_root / relative.parent
        parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
        _deny(_identity(parent, directory=True) != parent_identity, "baseline cache parent changed")
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            temporary_name = f".{name}.stage-{secrets.token_hex(16)}"
            os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
            temporary = parent / temporary_name
            writer = SecureDirectory(temporary, create=False)
            with writer._root_descriptor() as descriptor:
                stage_identity = (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
                _deny(
                    _identity(temporary, directory=True) != stage_identity, "baseline stage changed"
                )
                for path, content in files.items():
                    writer.write_bytes_atomic(f"artifact/{pack_id}/{path}", content)
                writer.write_bytes_atomic("origin/bundle.lock.json", raw_lock)
                writer.write_bytes_atomic("origin/projection.json", projection)
                writer.write_bytes_atomic("origin/source-record.json", record)
                _verify_stored(origin, pack_id, temporary, sealed=False)
                _seal(descriptor, seal_root=False)
                _require_owner(origin)
                _deny(
                    _identity(parent, directory=True) != parent_identity
                    or _identity(temporary, directory=True) != stage_identity,
                    "baseline publication root changed",
                )
                try:
                    _rename_noreplace(parent_fd, temporary_name, name)
                    published = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    _deny(
                        (published.st_dev, published.st_ino) != stage_identity,
                        "baseline publication identity changed",
                    )
                except OSError as error:
                    if error.errno != errno.EEXIST:
                        raise
                os.fchmod(descriptor, 0o500)
                os.fsync(descriptor)
                os.fsync(parent_fd)
        _deny(
            _identity(parent, directory=True) != parent_identity,
            "baseline cache parent changed during publication",
        )
    root = _verify_stored(origin, pack_id, origin.user_data_root / relative)
    _require_owner(origin)
    return RetainedBundledHostBaseline(
        pack_id, str(manifest["pack"]["artifact_digest"]), origin.bundle_digest, root
    )


class BundledHostBaselineRead:
    """One internal, nonescaping scope for frozen Host baseline reads."""

    def __init__(self, token: object, origin: BundledArtifactOrigin, bundle_root: Path) -> None:
        _deny(token is not _TOKEN, "bundled baseline read must be internally issued")
        self._origin = origin
        self._bundle = SecureDirectory(bundle_root, create=False)
        self._raw_lock = _read(self._bundle, "bundle.lock.json", metadata=True)
        _deny(
            _digest(self._raw_lock) != origin.bundle_digest,
            "bundled baseline lock differs from the active origin",
        )
        self._thread = threading.get_ident()
        self._active = True
        self._used: dict[str, tuple[Path, tuple[int, int]]] = {}

    def validate(self) -> None:
        """Recheck exact owner/origin, scope lifetime and frozen lock bytes."""
        _deny(
            not self._active or _CURRENT.get() is not self or threading.get_ident() != self._thread,
            "bundled baseline read is closed or foreign",
        )
        _require_owner(self._origin)
        _deny(
            _read(self._bundle, "bundle.lock.json", metadata=True) != self._raw_lock,
            "bundled baseline lock changed during read",
        )

    def resolve(self, pack_id: str) -> Path:
        """Resolve only a full baseline authenticated by the frozen projection."""
        self.validate()
        projection = _projection(
            self._raw_lock,
            lambda path: _read(self._bundle, path, metadata=True),
            pack_id,
            self._origin,
        )
        manifest = _baseline_projection(self._origin, self._raw_lock, projection, pack_id)
        container = self._origin.user_data_root / _container(
            self._origin, pack_id, str(manifest["pack"]["artifact_digest"])
        )
        try:
            container.lstat()
        except FileNotFoundError:
            root = resolve_pack_root(pack_id)
            record = canonical_json(dict(load_pack_catalog()[pack_id]))
            _capture_files(self._origin, pack_id, self._raw_lock, projection, record, root)
        else:
            root = _verify_stored(self._origin, pack_id, container)
        identity = _identity(root, directory=True)
        previous = self._used.setdefault(pack_id, (root, identity))
        _deny(previous != (root, identity), "bundled baseline root changed during read")
        self.validate()
        return root

    def verify_current(self) -> None:
        """Repeat every used full-byte proof before the read result escapes."""
        self.validate()
        for pack_id, (root, identity) in tuple(self._used.items()):
            _deny(
                self.resolve(pack_id) != root or _identity(root, directory=True) != identity,
                "bundled baseline changed during read",
            )
        self.validate()


def current_bundled_host_baseline_read() -> BundledHostBaselineRead | None:
    """Borrow only a scope already issued by normal active Host composition."""
    current = _CURRENT.get()
    if current is not None:
        current.validate()
    return current


@contextmanager
def canonical_admission_baseline() -> Iterator[None]:
    """Keep new admission isolated from an active read's baseline context."""
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def bundled_host_baseline_read(
    origin: BundledArtifactOrigin, *, bundle_root: Path
) -> Iterator[BundledHostBaselineRead]:
    """Read authenticated baselines without publishing or minting an origin."""
    _deny(_CURRENT.get() is not None, "nested bundled baseline read is denied")
    _require_owner(origin)
    read = BundledHostBaselineRead(_TOKEN, origin, bundle_root)
    token = _CURRENT.set(read)
    try:
        read.validate()
        yield read
        read.verify_current()
    finally:
        read._active = False
        _CURRENT.reset(token)
