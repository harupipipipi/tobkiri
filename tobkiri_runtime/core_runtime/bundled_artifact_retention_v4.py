"""Retain exact bundled Host Extension bytes under existing active authority.

This store is neither admission nor a source registry. A fresh origin is minted
only by normal active capture; its Authority-bound raw bundle-lock digest anchors
all retained bytes. Resolving a retained root never recaptures the active Profile.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import sys
from typing import Any, Mapping
from weakref import WeakValueDictionary

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.ids import validate_artifact_digest, validate_canonical_id
from tobkiri_protocol.secure_persistence import SecureDirectory
from tobkiri_protocol.validation import validate_document

from .active_profile_store_v4 import ActiveProfilePointer, ActiveProfileStore
from .authority.v4 import AuthorityStore

_STORE = "retained-bundled-artifacts-v4"
_MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_FILES = 10_000
_ORIGIN_TOKEN = object()
_ISSUED_ORIGINS: WeakValueDictionary[int, BundledArtifactOrigin] = WeakValueDictionary()


class BundledArtifactRetentionDenied(ValueError):
    """Retained bytes or their existing active-authority origin are invalid."""


@dataclass(frozen=True)
class BundledArtifactOrigin:
    """Private, immutable binding minted after normal active Profile capture."""

    user_data_root: Path
    profile_id: str
    activation_id: str
    plan_digest: str
    bundle_digest: str
    security_epoch: int
    fencing_token: int
    pack_pins: tuple[tuple[str, str], ...]
    _pointer: ActiveProfilePointer = field(repr=False)
    _owner_identity: tuple[int, int] = field(repr=False)
    _workspace_identity: tuple[int, int] = field(repr=False)
    _authority_identity: tuple[int, int] = field(repr=False)
    _authority_binding: tuple[Any, ...] = field(repr=False)
    _plan: bytes = field(repr=False)
    _token: object = field(repr=False, compare=False)

    @property
    def profile_binding(self) -> tuple[str, str, str, str, str]:
        """Return the exact normally captured Profile/activation binding."""
        return self._pointer.identity()

    def __post_init__(self) -> None:
        if self._token is not _ORIGIN_TOKEN:
            raise BundledArtifactRetentionDenied("origin requires normal active capture")


@dataclass(frozen=True)
class RetainedBundledArtifact:
    """One verified existing artifact; this result grants no new authority."""

    pack_id: str
    artifact_digest: str
    bundle_digest: str
    root: Path


def _deny(condition: bool, message: str) -> None:
    if condition:
        raise BundledArtifactRetentionDenied(message)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _identity(path: Path, *, directory: bool) -> tuple[int, int]:
    metadata = path.lstat()
    _deny(
        path.is_symlink()
        or (not stat.S_ISDIR(metadata.st_mode) if directory else not stat.S_ISREG(metadata.st_mode))
        or (not directory and metadata.st_nlink != 1)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid()),
        "retention owner or entry identity is unsafe",
    )
    return int(metadata.st_dev), int(metadata.st_ino)


def _authority_binding(authority: AuthorityStore, activation_id: str) -> tuple[Any, ...]:
    reservation = authority.active_activation_reservation(activation_id)
    _deny(reservation is None, "retention activation is not active in Authority")
    assert reservation is not None
    return (
        reservation["profile_id"],
        reservation["plan_digest"],
        reservation["profile_authority_digest"],
        reservation["security_epoch"],
        reservation["fencing_token"],
        authority.security_epoch,
    )


def capture_retention_origin(user_data_root: Path) -> BundledArtifactOrigin:
    """Mint an origin only after normal Authority-authenticated active capture.

    The root is supplied by Host composition, never a renderer request. No
    retained-root resolver is invoked here, and no trust/approval is created.
    """
    from .bootstrap.profile_capture import capture_active_profile, runtime_user_data_scope

    root = Path(user_data_root).absolute()
    SecureDirectory(root, create=False)
    owner_identity = _identity(root, directory=True)
    pointers = ActiveProfileStore(root)
    pointer = pointers.require(verify_snapshot=True)
    workspace_identity = _identity(root / "workspaces" / pointer.profile_id, directory=True)
    authority_identity = _identity(root / "authority/v4.sqlite3", directory=False)
    with runtime_user_data_scope(root):
        active = capture_active_profile()
    # Copy before deriving fields: a retained mutable application object cannot
    # change the pins after the canonical plan binding has been checked.
    plan = strict_loads(canonical_json(dict(active.resolved.plan)))
    _deny(
        canonical_digest({key: value for key, value in plan.items() if key != "plan_digest"})
        != pointer.plan_digest
        or (
            str(active.resolved.profile["profile_id"]),
            str(plan["profile_revision"]),
            str(active.activation["activation_id"]),
            str(plan["plan_digest"]),
            str(active.resolved.lock["lock_digest"]),
        )
        != pointer.identity(),
        "retention capture differs from the Host pointer",
    )
    validate_artifact_digest(plan["bundle_digest"], field="bundle_digest")
    with AuthorityStore(root / "authority/v4.sqlite3") as authority:
        binding = _authority_binding(authority, pointer.activation_id)
    expected = (
        pointer.profile_id,
        pointer.plan_digest,
        plan["profile_authority_snapshot_digest"],
        plan["security_epoch"],
        active.activation["fencing_token"],
        plan["security_epoch"],
    )
    _deny(binding != expected, "retention capture Authority binding is stale")
    pins = tuple(
        sorted(
            (str(row["identity"]), str(row["artifact_digest"]))
            for row in plan["effective_set"]
            if row["role"] == "pack"
        )
    )
    _deny(len(dict(pins)) != len(pins), "retention capture has duplicate Pack pins")
    origin = BundledArtifactOrigin(
        root,
        pointer.profile_id,
        pointer.activation_id,
        pointer.plan_digest,
        str(plan["bundle_digest"]),
        int(plan["security_epoch"]),
        int(active.activation["fencing_token"]),
        pins,
        pointer,
        owner_identity,
        workspace_identity,
        authority_identity,
        binding,
        canonical_json(plan),
        _ORIGIN_TOKEN,
    )
    _ISSUED_ORIGINS[id(origin)] = origin
    assert_retention_origin_current(origin)
    return origin


def assert_retention_origin_current(origin: BundledArtifactOrigin) -> None:
    """Recheck owner, exact pointer, Authority reservation, epoch, and fence.

    These normal store readers retain their established lifecycle/recovery
    behavior. This check does not publish artifacts or create admission/trust.
    """
    _deny(
        not isinstance(origin, BundledArtifactOrigin)
        or origin._token is not _ORIGIN_TOKEN
        or _ISSUED_ORIGINS.get(id(origin)) is not origin,
        "retention requires a captured origin",
    )
    plan = strict_loads(origin._plan)
    pins = tuple(
        sorted(
            (str(row["identity"]), str(row["artifact_digest"]))
            for row in plan["effective_set"]
            if row["role"] == "pack"
        )
    )
    _deny(
        canonical_digest({key: value for key, value in plan.items() if key != "plan_digest"})
        != origin.plan_digest
        or origin.plan_digest != origin._pointer.plan_digest
        or origin.profile_id != origin._pointer.profile_id
        or origin.activation_id != origin._pointer.activation_id
        or origin.bundle_digest != plan["bundle_digest"]
        or origin.pack_pins != pins
        or origin.security_epoch != plan["security_epoch"]
        or origin.security_epoch != origin._authority_binding[3]
        or origin.fencing_token != origin._authority_binding[4],
        "retention origin fields differ from the authenticated plan",
    )
    root = origin.user_data_root
    SecureDirectory(root, create=False)
    _deny(
        _identity(root, directory=True) != origin._owner_identity
        or _identity(root / "workspaces" / origin.profile_id, directory=True)
        != origin._workspace_identity
        or _identity(root / "authority/v4.sqlite3", directory=False) != origin._authority_identity,
        "retention Host owner identity changed",
    )
    pointers = ActiveProfileStore(root)
    _deny(pointers.require(verify_snapshot=True) != origin._pointer, "retention pointer is stale")
    with AuthorityStore(root / "authority/v4.sqlite3") as authority:
        _deny(
            _authority_binding(authority, origin.activation_id) != origin._authority_binding,
            "retention Authority binding is stale",
        )
    _deny(pointers.require(verify_snapshot=True) != origin._pointer, "retention pointer changed")


def authenticated_retention_plan(origin: BundledArtifactOrigin) -> dict[str, Any]:
    """Return an independent Plan copy after rechecking its captured authority."""
    assert_retention_origin_current(origin)
    return strict_loads(origin._plan)


def _relative(value: Any) -> str:
    _deny(not isinstance(value, str) or len(value) > 4096, "retention path is invalid")
    path = PurePosixPath(value)
    _deny(
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or "\x00" in value
        or len(path.parts) > 64
        or any(p in {".", ".."} for p in path.parts),
        "retention path is unsafe",
    )
    return value


def _read(reader: SecureDirectory, path: str, *, metadata: bool = False) -> bytes:
    return reader.read_bytes_bounded(
        _relative(path), max_bytes=_MAX_METADATA_BYTES if metadata else _MAX_FILE_BYTES
    )


def _projection(
    raw_lock: bytes, read_projection: Any, pack_id: str, origin: BundledArtifactOrigin
) -> bytes:
    _deny(_digest(raw_lock) != origin.bundle_digest, "retained bundle lock origin differs")
    lock = strict_loads(raw_lock)
    _deny(
        not isinstance(lock, dict)
        or set(lock) != {"schema", "entries"}
        or lock["schema"] != "io.tobkiri.defaultspack-bundle-lock.v1"
        or not isinstance(lock["entries"], list)
        or not 0 < len(lock["entries"]) <= _MAX_FILES,
        "retained bundle lock is invalid",
    )
    target = f"packs/{pack_id}.pack.v4.json"
    selected = None
    paths = set()
    for entry in lock["entries"]:
        _deny(
            not isinstance(entry, dict) or set(entry) != {"path", "kind", "digest"},
            "retained bundle entry is invalid",
        )
        path = _relative(entry["path"])
        _deny(path in paths, "retained bundle path is duplicated")
        paths.add(path)
        validate_artifact_digest(entry["digest"], field="bundle entry digest")
        if path == target:
            _deny(entry["kind"] != "pack", "retained projection role differs")
            selected = entry
    _deny(selected is None, "retained bundle does not contain the selected Pack")
    assert selected is not None
    raw = read_projection(target)
    _deny(_digest(raw) != selected["digest"], "retained projection digest differs")
    return raw


def _verify_payload(
    origin: BundledArtifactOrigin,
    pack_id: str,
    raw_lock: bytes,
    raw_projection: bytes,
    raw_record: bytes,
    reader: SecureDirectory,
) -> dict[str, bytes]:
    validate_canonical_id(pack_id, field="pack_id")
    pin = dict(origin.pack_pins).get(pack_id)
    _deny(pin is None, "retention Pack is outside the active closure")
    _projection(raw_lock, lambda _path: raw_projection, pack_id, origin)
    projection = validate_document(raw_projection, "pack")
    raw_manifest = _read(reader, "pack.v4.json", metadata=True)
    manifest = validate_document(raw_manifest, "pack")
    provenance = projection["provenance"]
    _deny(
        projection["pack"]["id"] != pack_id
        or projection["pack"]["artifact_digest"] != pin
        or provenance.get("schema") != "io.tobkiri.provenance.v2"
        or provenance.get("source_kind") != "generated"
        or provenance.get("normative") is not True
        or provenance.get("source_path") != f"ecosystem/{pack_id}/pack.v4.json"
        or provenance.get("source_digest") != _digest(raw_manifest)
        or projection["integrity"]["source_identity"] != _digest(raw_manifest)
        or manifest["pack"]["id"] != pack_id
        or manifest["pack"]["artifact_digest"] != pin
        or manifest["pack"]["kind"] != "host_extension"
        or manifest["requirements"]["execution_boundary"] != "host_brokered"
        or manifest["integrity"]["artifact_set_digest"] != pin
        or canonical_digest(manifest["artifacts"]) != pin,
        "retention materialization is not bound to the active bundled projection",
    )
    record = strict_loads(raw_record)
    _deny(
        not isinstance(record, dict)
        or canonical_digest(record) != manifest["integrity"]["source_identity"]
        or record.get("pack_id") != pack_id
        or record.get("kind") != "host_extension"
        or record.get("version") != manifest["pack"]["version"],
        "retention canonical source record differs",
    )
    runtime = record.get("runtime_artifacts")
    _deny(
        not isinstance(runtime, list) or not 0 < len(runtime) <= _MAX_FILES - 3,
        "retention runtime inventory is invalid",
    )
    expected = {}
    for entry in runtime:
        path = _relative(entry["path"])
        _deny(
            path in expected
            or path
            in {
                "pack.v4.json",
                "contracts.v4.json",
                "executables.v4.json",
                "artifact-index.v4.json",
            },
            "retention runtime path is duplicated or reserved",
        )
        validate_artifact_digest(entry["digest"], field="runtime digest")
        expected[path] = {"digest": entry["digest"], "role": entry.get("index_role", "runtime")}
    artifacts = {entry["path"]: entry["digest"] for entry in manifest["artifacts"]}
    _deny(
        len(artifacts) != len(manifest["artifacts"])
        or set(artifacts) != set(expected) | {"executables.v4.json"}
        or any(artifacts[path] != item["digest"] for path, item in expected.items()),
        "retention manifest runtime inventory differs",
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
        entry["path"]: {"digest": entry["digest"], "role": entry["role"]}
        for entry in index["artifacts"]
    }
    _deny(
        len(actual) != len(index["artifacts"])
        or actual != expected
        or index["pack_id"] != pack_id
        or index["artifact_set_digest"] != pin
        or index["source_identity"] != manifest["integrity"]["source_identity"]
        or index["integrity_seal"]["algorithm"] != "sha256-canonical-v1"
        or index["integrity_seal"]["signed_digest"]
        != canonical_digest(
            {key: value for key, value in index.items() if key != "integrity_seal"}
        ),
        "retention artifact index inventory or roles differ",
    )
    files = {"artifact-index.v4.json": raw_index}
    total = len(raw_index) + len(raw_lock) + len(raw_projection) + len(raw_record)
    for path, item in expected.items():
        content = _read(reader, path, metadata=path.endswith(".json"))
        total += len(content)
        _deny(
            total > _MAX_TOTAL_BYTES or _digest(content) != item["digest"],
            "retention artifact bytes differ or exceed limits",
        )
        files[path] = content
    for name, kind in (
        ("contracts.v4.json", "pack_contract_catalog"),
        ("executables.v4.json", "executable_catalog"),
    ):
        document = validate_document(files[name], kind)
        _deny(
            document["pack_id"] != pack_id
            or document["source_identity"] != index["source_identity"],
            "retention metadata source identity differs",
        )
    _deny(
        _read(reader, "pack.v4.json", metadata=True) != raw_manifest
        or _read(reader, "artifact-index.v4.json", metadata=True) != raw_index,
        "retention metadata changed during verification",
    )
    return files


def _container_relative(origin: BundledArtifactOrigin, pack_id: str) -> Path:
    validate_canonical_id(pack_id, field="pack_id")
    pin = dict(origin.pack_pins).get(pack_id)
    _deny(pin is None, "retention Pack is outside the active closure")
    assert pin is not None
    return Path(_STORE) / origin.bundle_digest[7:] / pin[7:] / pack_id


def _verify_stored(
    origin: BundledArtifactOrigin, pack_id: str, container: Path, *, sealed: bool = True
) -> Path:
    reader = SecureDirectory(container, create=False)
    raw_lock = _read(reader, "origin/bundle.lock.json", metadata=True)
    projection = _read(reader, "origin/projection.json", metadata=True)
    record = _read(reader, "origin/source-record.json", metadata=True)
    root = container / "artifact" / pack_id
    artifact_reader = SecureDirectory(root, create=False)
    files = _verify_payload(origin, pack_id, raw_lock, projection, record, artifact_reader)
    wanted = {f"artifact/{pack_id}/{path}" for path in files} | {
        "origin/bundle.lock.json",
        "origin/projection.json",
        "origin/source-record.json",
    }
    wanted_directories = {
        parent.as_posix()
        for name in wanted
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    _deny(
        len(wanted) + len(wanted_directories) > _MAX_FILES + 16,
        "retention store tree exceeds limits",
    )
    _deny(
        sealed and stat.S_IMODE(container.lstat().st_mode) != 0o500,
        "retention store contains an extra or unsafe entry",
    )
    observed: set[str] = set()
    observed_directories: set[str] = set()

    def inspect_tree(descriptor: int, prefix: str = "") -> None:
        # Iterate descriptor-relative entries rather than allocating a complete
        # hostile directory listing or following a swapped directory name.
        with os.scandir(descriptor) as entries:
            for entry in entries:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                metadata = entry.stat(follow_symlinks=False)
                directory = stat.S_ISDIR(metadata.st_mode)
                _deny(
                    relative not in (wanted_directories if directory else wanted)
                    or (not directory and not stat.S_ISREG(metadata.st_mode))
                    or (not directory and metadata.st_nlink != 1)
                    or metadata.st_uid != os.getuid()
                    or (
                        sealed and stat.S_IMODE(metadata.st_mode) != (0o500 if directory else 0o400)
                    ),
                    "retention store contains an extra or unsafe entry",
                )
                if directory:
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                    try:
                        opened = os.fstat(child)
                        _deny(
                            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino),
                            "retention store directory changed",
                        )
                        observed_directories.add(relative)
                        inspect_tree(child, relative)
                    finally:
                        os.close(child)
                else:
                    observed.add(relative)

    _deny(os.name != "posix", "retention verification requires POSIX directory primitives")
    with reader._root_descriptor() as descriptor:
        inspect_tree(descriptor)
    _deny(
        observed != wanted or observed_directories != wanted_directories,
        "retention store contains extra or missing entries",
    )
    _deny(
        _read(reader, "origin/bundle.lock.json", metadata=True) != raw_lock,
        "retention store root changed",
    )
    return root


def verify_retained_bundled_artifact(
    origin: BundledArtifactOrigin, *, pack_id: str, artifact_digest: str
) -> Path:
    """Verify an existing cache against its exact active pin, without publication.

    No capture occurs here. Freshness uses the normal authenticated store
    readers, including their lifecycle behavior; no artifact is adopted.
    """
    assert_retention_origin_current(origin)
    validate_artifact_digest(artifact_digest, field="artifact_digest")
    _deny(
        dict(origin.pack_pins).get(pack_id) != artifact_digest,
        "retention requested artifact differs from the active pin",
    )
    container = origin.user_data_root / _container_relative(origin, pack_id)
    root = _verify_stored(origin, pack_id, container)
    assert_retention_origin_current(origin)
    return root


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
    """Atomically publish a directory without replacing any existing name."""
    library = ctypes.CDLL(None, use_errno=True)
    name = "renameatx_np" if sys.platform == "darwin" else "renameat2"
    function = getattr(library, name, None)
    _deny(function is None, "atomic no-replace directory publication is unavailable")
    assert function is not None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    flags = 4 if sys.platform == "darwin" else 1  # RENAME_EXCL / RENAME_NOREPLACE.
    if function(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _seal_tree(descriptor: int, *, seal_root: bool = True) -> None:
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        directory = stat.S_ISDIR(metadata.st_mode)
        _deny(
            not directory and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1),
            "retention publication tree is unsafe",
        )
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
        child = os.open(name, flags, dir_fd=descriptor)
        try:
            opened = os.fstat(child)
            _deny(
                (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino),
                "retention publication entry changed",
            )
            if directory:
                _seal_tree(child)
            os.fchmod(child, 0o500 if directory else 0o400)
            os.fsync(child)
        finally:
            os.close(child)
    if seal_root:
        os.fchmod(descriptor, 0o500)
    os.fsync(descriptor)


def retain_bundled_artifact(
    origin: BundledArtifactOrigin,
    *,
    bundle_root: Path,
    source_root: Path,
    source_record: Mapping[str, Any],
) -> RetainedBundledArtifact:
    """Authenticate supplied old bytes and atomically retain their exact closure.

    Inputs are untrusted byte suppliers, not authority. Publication currently
    requires POSIX no-replace directory rename; unsupported platforms fail closed.
    A failed publication retains private staging residue rather than deleting an
    identity-swapped path. It never overwrites an existing retained artifact.
    """
    _deny(os.name != "posix", "retention publication requires POSIX directory primitives")
    assert_retention_origin_current(origin)
    source = SecureDirectory(Path(source_root), create=False)
    manifest = validate_document(_read(source, "pack.v4.json", metadata=True), "pack")
    pack_id = str(manifest["pack"]["id"])
    bundle = SecureDirectory(Path(bundle_root), create=False)
    raw_lock = _read(bundle, "bundle.lock.json", metadata=True)
    projection = _projection(
        raw_lock, lambda path: _read(bundle, path, metadata=True), pack_id, origin
    )
    record = canonical_json(dict(source_record))
    _deny(len(record) > _MAX_METADATA_BYTES, "retention source record exceeds limits")
    files = _verify_payload(origin, pack_id, raw_lock, projection, record, source)
    _deny(
        _read(bundle, "bundle.lock.json", metadata=True) != raw_lock,
        "retention bundle changed during capture",
    )
    assert_retention_origin_current(origin)
    relative = _container_relative(origin, pack_id)
    owner = SecureDirectory(origin.user_data_root, create=False)
    # Hold the complete owner/parent descriptor chain throughout staging and
    # publication. A named ancestor swap cannot redirect writes to another tree.
    with owner._parent_descriptor(relative, create=True) as (parent_fd, name):
        parent_path = origin.user_data_root / relative.parent
        parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
        _deny(
            _identity(parent_path, directory=True) != parent_identity,
            "retention cache parent changed",
        )
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            temporary_name = f".{name}.stage-{secrets.token_hex(16)}"
            os.mkdir(temporary_name, mode=0o700, dir_fd=parent_fd)
            temporary = parent_path / temporary_name
            writer = SecureDirectory(temporary, create=False)
            with writer._root_descriptor() as stage_fd:
                stage_identity = (os.fstat(stage_fd).st_dev, os.fstat(stage_fd).st_ino)
                _deny(
                    _identity(temporary, directory=True) != stage_identity,
                    "retention staging root changed",
                )
                for path, content in files.items():
                    writer.write_bytes_atomic(f"artifact/{pack_id}/{path}", content)
                for path, content in (
                    ("origin/bundle.lock.json", raw_lock),
                    ("origin/projection.json", projection),
                    ("origin/source-record.json", record),
                ):
                    writer.write_bytes_atomic(path, content)
                _verify_stored(origin, pack_id, temporary, sealed=False)
                # macOS requires a writable directory root to rename it. Seal
                # every descendant first, then seal this same pinned root
                # immediately after publication and before any successful read.
                _seal_tree(stage_fd, seal_root=False)
                assert_retention_origin_current(origin)
                _deny(
                    _identity(parent_path, directory=True) != parent_identity
                    or _identity(temporary, directory=True) != stage_identity,
                    "retention publication root changed",
                )
                try:
                    _rename_noreplace(parent_fd, temporary_name, name)
                    published = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    _deny(
                        (published.st_dev, published.st_ino) != stage_identity,
                        "retention publication directory identity changed",
                    )
                except OSError as error:
                    if error.errno != errno.EEXIST:
                        raise
                    # Another complete, no-replace publication won. Verify it
                    # normally; do not overwrite it or delete swapped residue.
                os.fchmod(stage_fd, 0o500)
                os.fsync(stage_fd)
                os.fsync(parent_fd)
        _deny(
            _identity(parent_path, directory=True) != parent_identity,
            "retention cache parent changed during publication",
        )
    root = verify_retained_bundled_artifact(
        origin, pack_id=pack_id, artifact_digest=dict(origin.pack_pins)[pack_id]
    )
    return RetainedBundledArtifact(
        pack_id, dict(origin.pack_pins)[pack_id], origin.bundle_digest, root
    )
