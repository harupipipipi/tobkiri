"""Bounded prebuilt Shell inputs authenticated by normal active capture.

Only the explicit development packager consumes this input. Neither supplier
paths nor their metadata grant authority: the normally captured Plan and its
raw bundle-lock digest authenticate every selected document and artifact.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import time
from typing import Any, Iterator
from weakref import WeakValueDictionary

from tobkiri_protocol.canonical import canonical_digest, strict_loads
from tobkiri_protocol.platform_artifact import verify_platform_artifact
from tobkiri_protocol.secure_persistence import SecureDirectory
from tobkiri_protocol.validation import validate_document

from .bundled_artifact_retention_v4 import (
    BundledArtifactOrigin,
    assert_retention_origin_current,
    authenticated_retention_plan,
)

_MAX_METADATA = 8 * 1024 * 1024
_MAX_FILE = 128 * 1024 * 1024
_MAX_TOTAL = 512 * 1024 * 1024
_MAX_ENTRIES = 10_000
_TOKEN = object()
_ISSUED: WeakValueDictionary[int, VerifiedPrebuiltShellInput] = WeakValueDictionary()


class PrebuiltShellDenied(ValueError):
    """The prebuilt input is not the exact currently authorized Shell."""


@dataclass(frozen=True)
class VerifiedPrebuiltShellInput:
    """A private, scope-owned snapshot, carrying no execution authority."""

    origin: BundledArtifactOrigin = field(repr=False)
    root: Path
    documents: tuple[tuple[str, bytes], ...] = field(repr=False)
    variant_json: bytes = field(repr=False)
    host_assets: tuple[tuple[str, str], ...]
    inventory: tuple[tuple[str, str, int], ...] = field(repr=False)
    _root_identity: tuple[int, int] = field(repr=False)
    _token: object = field(repr=False, compare=False)


def _deny(condition: bool, message: str) -> None:
    if condition:
        raise PrebuiltShellDenied(message)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _relative(value: Any) -> str:
    _deny(not isinstance(value, str) or len(value) > 4096, "invalid prebuilt path")
    path = PurePosixPath(value)
    _deny(
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or "\x00" in value
        or len(path.parts) > 64
        or any(part in {".", ".."} for part in path.parts),
        "unsafe prebuilt path",
    )
    return value


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _snapshot_entry(
    parent: int,
    name: str,
    destination: Path,
    budget: list[int],
    deadline: float,
    depth: int = 0,
) -> None:
    """Copy a finite descriptor-relative tree without following any links."""
    _relative(name)
    budget[1] += 1
    _deny(
        depth > 64 or budget[1] > _MAX_ENTRIES or time.monotonic() >= deadline,
        "prebuilt snapshot exceeds its work limit",
    )
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _deny(
        not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)),
        "prebuilt snapshot contains an unsupported entry",
    )
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
    )
    try:
        opened = os.fstat(descriptor)
        _deny(_fingerprint(opened) != _fingerprint(before), "prebuilt entry changed")
        if stat.S_ISDIR(opened.st_mode):
            destination.mkdir(mode=0o700)
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    _snapshot_entry(
                        descriptor,
                        entry.name,
                        destination / entry.name,
                        budget,
                        deadline,
                        depth + 1,
                    )
        else:
            _deny(
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size > _MAX_FILE,
                "prebuilt file is unsafe or exceeds its limit",
            )
            total = 0
            with destination.open("xb") as output:
                while chunk := os.read(descriptor, 1024 * 1024):
                    total += len(chunk)
                    budget[0] += len(chunk)
                    _deny(
                        total > _MAX_FILE
                        or budget[0] > _MAX_TOTAL
                        or time.monotonic() >= deadline,
                        "prebuilt snapshot exceeds its work limit",
                    )
                    output.write(chunk)
            _deny(total != opened.st_size, "prebuilt file changed while copied")
            destination.chmod(0o500 if opened.st_mode & 0o111 else 0o400)
        _deny(
            _fingerprint(os.fstat(descriptor)) != _fingerprint(opened)
            or _fingerprint(os.stat(name, dir_fd=parent, follow_symlinks=False))
            != _fingerprint(opened),
            "prebuilt entry changed while copied",
        )
    finally:
        os.close(descriptor)


def _documents(
    origin: BundledArtifactOrigin,
    bundle: SecureDirectory,
) -> tuple[tuple[tuple[str, bytes], ...], dict[str, Any], tuple[tuple[str, str], ...]]:
    plan = authenticated_retention_plan(origin)
    raw_lock = bundle.read_bytes_bounded("bundle.lock.json", max_bytes=_MAX_METADATA)
    _deny(
        _digest(raw_lock) != origin.bundle_digest, "prebuilt lock differs from origin"
    )
    lock = strict_loads(raw_lock)
    _deny(
        not isinstance(lock, dict)
        or set(lock) != {"schema", "entries"}
        or lock["schema"] != "io.tobkiri.defaultspack-bundle-lock.v1"
        or not isinstance(lock["entries"], list)
        or not 0 < len(lock["entries"]) <= _MAX_ENTRIES,
        "invalid prebuilt bundle lock",
    )
    entries = {}
    for row in lock["entries"]:
        _deny(
            not isinstance(row, dict) or set(row) != {"path", "kind", "digest"},
            "invalid prebuilt lock entry",
        )
        path = _relative(row["path"])
        _deny(path in entries, "duplicate prebuilt lock path")
        entries[path] = row
    shell_id = plan["shell"]["pack_id"]
    app_id = plan["application"]["pack_id"]
    names = (
        (f"{plan['shell']['provider_id']}.shell.v1.json", "shell"),
        (f"packs/{shell_id}.pack.v4.json", "pack"),
        (f"packs/{app_id}.pack.v4.json", "pack"),
    )
    documents = []
    parsed = []
    for path, kind in names:
        path = _relative(path)
        row = entries.get(path)
        _deny(
            row is None or row["kind"] != kind, "selected prebuilt document is absent"
        )
        raw = bundle.read_bytes_bounded(path, max_bytes=_MAX_METADATA)
        _deny(_digest(raw) != row["digest"], "prebuilt document differs from lock")
        documents.append((path, raw))
        parsed.append(validate_document(raw, kind))
    shell, shell_pack, application = parsed
    _deny(
        canonical_digest(shell) != plan["shell"]["definition_digest"]
        or shell["pack_id"] != shell_id
        or shell["provider_id"] != plan["shell"]["provider_id"]
        or shell["availability"] != "verified"
        or shell_pack["pack"]["id"] != shell_id
        or shell_pack["pack"]["kind"] != "shell"
        or shell_pack["pack"]["artifact_digest"] != plan["shell"]["artifact_digest"]
        or canonical_digest(application) != plan["application"]["definition_digest"]
        or application["pack"]["id"] != app_id
        or application["pack"]["kind"] != "application"
        or application["pack"]["artifact_digest"]
        != plan["application"]["artifact_digest"],
        "prebuilt definitions differ from the authenticated Plan",
    )
    variants = shell["launch"]["variants"]
    _deny(len(variants) != 1, "prebuilt Shell requires one exact variant")
    variant = variants[0]
    launch = plan["launch_contribution"]
    for name in (
        "platform",
        "architecture",
        "artifact_digest",
        "relative_path",
        "entrypoint",
    ):
        _deny(variant[name] != launch[name], "prebuilt launch differs from the Plan")
    _deny(
        variant["entrypoint_digest"] != plan["shell"]["executable_artifact_digest"]
        or variant["entrypoint_digest"]
        != plan["application"]["executable_artifact_digest"]
        or shell["artifact_digest"] != variant["artifact_digest"],
        "prebuilt executable differs from the Plan",
    )
    executable = {
        "path": variant["relative_path"],
        "digest": variant["artifact_digest"],
        "entrypoint_digest": variant["entrypoint_digest"],
        "kind": "executable",
        "platform": f"{variant['platform']}-{variant['architecture']}",
        "entrypoint": variant["entrypoint"],
        "argv": [],
    }
    assets: dict[str, str] = {}
    for pack in (shell_pack, application):
        _deny(
            canonical_digest(pack["artifacts"]) != pack["pack"]["artifact_digest"],
            "prebuilt Pack artifact inventory differs",
        )
        _deny(
            [row for row in pack["artifacts"] if row["kind"] == "executable"]
            != [executable],
            "prebuilt Pack executable differs",
        )
        for row in pack["artifacts"]:
            if row["kind"] == "executable":
                continue
            _deny(
                row["kind"] != "asset" or row["platform"] != "host",
                "unsupported prebuilt host asset",
            )
            path = _relative(row["path"])
            _deny(
                path in assets and assets[path] != row["digest"],
                "conflicting prebuilt host assets",
            )
            assets[path] = row["digest"]
    return tuple(documents), variant, tuple(sorted(assets.items()))


def _inventory(root: Path) -> tuple[tuple[str, str, int], ...]:
    reader = SecureDirectory(root, create=False)
    rows: list[tuple[str, str, int]] = []
    budget = [0, 0]
    deadline = time.monotonic() + 30.0

    def visit(parent: int, prefix: str = "", depth: int = 0) -> None:
        before = os.fstat(parent)
        with os.scandir(parent) as entries:
            for entry in entries:
                budget[1] += 1
                _deny(
                    depth > 64
                    or budget[1] > _MAX_ENTRIES
                    or time.monotonic() >= deadline,
                    "prebuilt inventory exceeds its work limit",
                )
                relative = _relative(f"{prefix}/{entry.name}" if prefix else entry.name)
                named = os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                _deny(
                    not (stat.S_ISDIR(named.st_mode) or stat.S_ISREG(named.st_mode)),
                    "unsafe prebuilt snapshot entry",
                )
                descriptor = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
                try:
                    opened = os.fstat(descriptor)
                    _deny(
                        _fingerprint(named) != _fingerprint(opened),
                        "snapshot entry changed",
                    )
                    if stat.S_ISDIR(opened.st_mode):
                        rows.append(
                            (relative, "directory", stat.S_IMODE(opened.st_mode))
                        )
                        visit(descriptor, relative, depth + 1)
                    else:
                        _deny(
                            opened.st_nlink != 1 or opened.st_size > _MAX_FILE,
                            "unsafe prebuilt snapshot file",
                        )
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := os.read(descriptor, 1024 * 1024):
                            size += len(chunk)
                            budget[0] += len(chunk)
                            _deny(
                                size > _MAX_FILE
                                or budget[0] > _MAX_TOTAL
                                or time.monotonic() >= deadline,
                                "prebuilt inventory exceeds its work limit",
                            )
                            digest.update(chunk)
                        _deny(size != opened.st_size, "snapshot file changed")
                        rows.append(
                            (
                                relative,
                                "sha256:" + digest.hexdigest(),
                                stat.S_IMODE(opened.st_mode),
                            )
                        )
                    _deny(
                        _fingerprint(os.fstat(descriptor)) != _fingerprint(opened)
                        or _fingerprint(
                            os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                        )
                        != _fingerprint(opened),
                        "snapshot entry changed",
                    )
                finally:
                    os.close(descriptor)
        _deny(
            _fingerprint(os.fstat(parent)) != _fingerprint(before),
            "snapshot directory changed",
        )

    with reader._root_descriptor() as descriptor:
        visit(descriptor)
    with reader._root_descriptor():
        pass
    return tuple(sorted(rows))


def assert_prebuilt_shell_current(value: VerifiedPrebuiltShellInput) -> None:
    """Recheck scope ownership, complete snapshot bytes, and active Authority."""
    _deny(
        not isinstance(value, VerifiedPrebuiltShellInput)
        or value._token is not _TOKEN
        or _ISSUED.get(id(value)) is not value,
        "prebuilt input requires an open authenticated scope",
    )
    assert_retention_origin_current(value.origin)
    metadata = value.root.lstat()
    _deny(
        value.root.is_symlink()
        or (metadata.st_dev, metadata.st_ino) != value._root_identity,
        "prebuilt snapshot root changed",
    )
    _deny(_inventory(value.root) != value.inventory, "prebuilt snapshot changed")
    verify_platform_artifact(
        value.root / "platform-artifacts",
        strict_loads(value.variant_json),
        deadline_monotonic=time.monotonic() + 30.0,
    )
    entrypoint = (
        value.root
        / "platform-artifacts"
        / strict_loads(value.variant_json)["entrypoint"]
    )
    _deny(
        not entrypoint.lstat().st_mode & 0o111, "prebuilt entrypoint is not executable"
    )
    metadata = value.root.lstat()
    _deny(
        value.root.is_symlink()
        or (metadata.st_dev, metadata.st_ino) != value._root_identity,
        "prebuilt snapshot root changed",
    )
    assert_retention_origin_current(value.origin)


@contextmanager
def verified_active_prebuilt_shell(
    origin: BundledArtifactOrigin,
    *,
    bundle_root: Path,
    artifact_root: Path,
    host_asset_root: Path,
) -> Iterator[VerifiedPrebuiltShellInput]:
    """Snapshot only Shell bytes authenticated by a normal full-capture origin."""
    _deny(
        os.name == "nt",
        "debug prebuilt snapshots require no-follow directory descriptors",
    )
    assert_retention_origin_current(origin)
    documents, variant, assets = _documents(
        origin, SecureDirectory(bundle_root, create=False)
    )
    from tobkiri_protocol.canonical import canonical_json

    temporary = Path(tempfile.mkdtemp(prefix="tobkiri-prebuilt-shell-"))
    metadata = temporary.lstat()
    root_identity = (metadata.st_dev, metadata.st_ino)
    value = None
    try:
        budget = [sum(len(raw) for _path, raw in documents), len(documents)]
        for path, raw in documents:
            destination = temporary / "documents" / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        artifact_reader = SecureDirectory(artifact_root, create=False)
        relative = _relative(variant["relative_path"])
        destination = temporary / "platform-artifacts" / relative
        destination.parent.mkdir(parents=True)
        with artifact_reader._parent_descriptor(relative, create=False) as (
            parent,
            name,
        ):
            _snapshot_entry(parent, name, destination, budget, time.monotonic() + 30.0)
        verify_platform_artifact(
            temporary / "platform-artifacts",
            variant,
            deadline_monotonic=time.monotonic() + 30.0,
        )
        asset_reader = SecureDirectory(host_asset_root, create=False)
        for path, digest in assets:
            raw = asset_reader.read_bytes_bounded(path, max_bytes=_MAX_METADATA)
            budget[0] += len(raw)
            budget[1] += 1
            _deny(
                budget[0] > _MAX_TOTAL or budget[1] > _MAX_ENTRIES,
                "prebuilt snapshot exceeds its work limit",
            )
            _deny(_digest(raw) != digest, "prebuilt host asset differs")
            destination = temporary / "host-assets" / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        value = VerifiedPrebuiltShellInput(
            origin,
            temporary,
            documents,
            canonical_json(variant),
            assets,
            _inventory(temporary),
            root_identity,
            _TOKEN,
        )
        _ISSUED[id(value)] = value
        assert_prebuilt_shell_current(value)
        yield value
        assert_prebuilt_shell_current(value)
    finally:
        if value is not None:
            _ISSUED.pop(id(value), None)
        from scripts.packaging_cleanup import remove_owned_path

        remove_owned_path(
            temporary,
            owner_root=temporary.parent,
            operation="remove authenticated prebuilt Shell snapshot",
            expected_identity=root_identity,
        )
