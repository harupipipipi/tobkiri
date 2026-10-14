"""Offline implementation-only Host Extension authoring, without Host authority."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
import stat
from pathlib import Path
import tempfile
from typing import Any, Mapping

from scripts.migrate_pack_artifacts_v4 import render_artifact_documents
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.ids import validate_artifact_digest
from tobkiri_protocol.secure_persistence import SecureDirectory

from .bundled_artifact_retention_v4 import _rename_noreplace
from .global_contracts.semver import parse_version
from .host_extension_update_v4 import (
    _VerifiedPack,
    _authority_closure,
    _relative_path,
    _verified_pack,
)
from .pack_authoring import PackAuthoringError
from .pack_signature import (
    MAX_SIGNED_FILE_BYTES,
    MAX_SIGNED_TOTAL_BYTES,
    SIGNED_MANIFEST_RELATIVE,
    _file_records,
)

_METADATA = frozenset(
    {
        "pack.v4.json",
        "contracts.v4.json",
        "executables.v4.json",
        "artifact-index.v4.json",
    }
)


def build_host_extension_successor(
    target: Path,
    *,
    predecessor_root: Path,
    expected_predecessor_digest: str,
    version: str,
    replacements: Mapping[str, bytes],
) -> Path:
    """Compile an unsigned implementation-only successor without importing code.

    This is structural authoring, never publisher authentication or authority.
    The caller must authenticate the predecessor separately. Before admission,
    the Host must normally sign/authenticate the successor and run the unchanged
    signed compatibility verifier. Every individual Contract, including historic
    provenance, and every existing authority declaration remain immutable.
    """
    validate_artifact_digest(expected_predecessor_digest)
    target = Path(target).absolute()
    if ".." in target.parts or target.exists() or target.is_symlink():
        raise PackAuthoringError("target must be a new directory")
    previous = _verified_pack(Path(predecessor_root), require_signed=False)
    artifact = previous.compiled.artifact
    if artifact.digest != expected_predecessor_digest:
        raise PackAuthoringError("Host Extension predecessor digest is stale")
    if not isinstance(version, str) or not parse_version(
        artifact.version
    ) < parse_version(version):
        raise PackAuthoringError("Host Extension successor requires a newer version")
    captured = dict(replacements)
    runtime_paths = {
        item["path"]
        for item in previous.documents["artifact-index.v4.json"]["artifacts"]
        if item["role"] == "runtime" and item["path"] not in _METADATA
    }
    if not captured or any(
        not isinstance(name, str)
        or _relative_path(name) not in runtime_paths
        or not isinstance(content, bytes)
        or len(content) > MAX_SIGNED_FILE_BYTES
        for name, content in captured.items()
    ):
        raise PackAuthoringError("only existing bounded runtime bytes may change")
    reader = SecureDirectory(Path(predecessor_root), create=False)
    inventory = _file_records(reader.root)
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    for item in inventory:
        name = item["path"]
        content = reader.read_bytes_bounded(name, max_bytes=MAX_SIGNED_FILE_BYTES)
        if (
            len(content) != item["size"]
            or hashlib.sha256(content).hexdigest() != item["sha256"]
        ):
            raise PackAuthoringError(
                "Host Extension predecessor changed during capture"
            )
        files[name], modes[name] = content, item["mode"]
    if _file_records(reader.root) != inventory:
        raise PackAuthoringError("Host Extension predecessor inventory changed")
    # Bind copied bytes to the very same metadata that was structurally checked.
    from tobkiri_protocol.validation import validate_document

    for name, schema in (
        ("pack.v4.json", "pack"),
        ("contracts.v4.json", "pack_contract_catalog"),
        ("executables.v4.json", "executable_catalog"),
        ("artifact-index.v4.json", "pack_artifact_index"),
    ):
        if validate_document(files[name], schema) != previous.documents[name]:
            raise PackAuthoringError("Host Extension predecessor metadata changed")
    for document in ("pack.v4.json", "artifact-index.v4.json"):
        for item in previous.documents[document]["artifacts"]:
            content = files.get(item["path"])
            if (
                content is None
                or "sha256:" + hashlib.sha256(content).hexdigest() != item["digest"]
            ):
                raise PackAuthoringError("Host Extension predecessor artifact changed")
    if any(name not in files for name in captured):
        raise PackAuthoringError("Host Extension runtime inventory is incomplete")
    if not any(files[name] != content for name, content in captured.items()):
        raise PackAuthoringError("successor requires changed runtime bytes")
    files.update(captured)
    if sum(map(len, files.values())) > MAX_SIGNED_TOTAL_BYTES:
        raise PackAuthoringError("Host Extension successor exceeds size limits")
    digests = {
        name: "sha256:" + hashlib.sha256(content).hexdigest()
        for name, content in captured.items()
    }
    source = host_extension_successor_source(
        predecessor_artifact_digest=artifact.digest,
        pack_id=artifact.pack_id,
        version=version,
        replacement_digests=digests,
    )
    source_identity = canonical_digest(source)
    manifest = deepcopy(previous.documents["pack.v4.json"])
    contracts = deepcopy(previous.documents["contracts.v4.json"])
    executable = deepcopy(previous.documents["executables.v4.json"])
    index = deepcopy(previous.documents["artifact-index.v4.json"])
    manifest["pack"]["version"] = version
    manifest["integrity"]["source_identity"] = source_identity
    contracts["source_identity"] = executable["source_identity"] = source_identity
    manifest["provenance"] = {
        "schema": "io.tobkiri.provenance.v1",
        "source_kind": "generated",
        "source_path": "urn:tobkiri:host-extension-successor-source:v1",
        "source_digest": source_identity,
        "repository_commit": "working-tree",
        "repository_tree": source_identity.removeprefix("sha256:"),
        "generator": "tobkiri.core_runtime.host_extension_authoring",
        "generator_version": "1.0.0",
        "normative": True,
        "evidence": [
            {
                "path": "pack.v4.json",
                "rule_id": "implementation-only-predecessor",
                "digest": artifact.digest,
            },
            *[
                {
                    "path": name,
                    "rule_id": "captured-existing-runtime-bytes",
                    "digest": digest,
                }
                for name, digest in sorted(digests.items())
            ],
        ],
    }
    implementations = {}
    for variant in executable["variants"]:
        name = variant["implementation_path"]
        if name in digests:
            variant["implementation_digest"] = digests[name]
            implementations[variant["function_id"]] = digests[name]
    for function in manifest["functions"]:
        if function["id"] in implementations:
            function["implementation_digest"] = implementations[function["id"]]
    for items in (manifest["artifacts"], index["artifacts"]):
        for item in items:
            if item["path"] in digests:
                item["digest"] = digests[item["path"]]
    rendered = render_artifact_documents(
        manifest,
        contracts,
        executable,
        index["artifacts"],
    )
    if set(rendered) != _METADATA:
        raise PackAuthoringError("metadata producer changed artifact paths")
    files.update({name: text.encode("utf-8") for name, text in rendered.items()})
    if sum(map(len, files.values())) > MAX_SIGNED_TOTAL_BYTES:
        raise PackAuthoringError("Host Extension successor exceeds size limits")
    return _publish_successor(target, files, modes, previous, version)


def host_extension_successor_source(
    *,
    predecessor_artifact_digest: str,
    pack_id: str,
    version: str,
    replacement_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Return the exact captured recipe for a caller-owned source receipt.

    Manifest provenance's ``urn:tobkiri:host-extension-successor-source:v1``
    identifies this in-memory source format, rather than a repository file or
    a JSON pointer. Its canonical digest is the successor source identity.
    This data record grants no authority and adds no path inside the Pack.
    """
    return {
        "source_api_version": "io.tobkiri.host-extension-successor-source.v1",
        "predecessor_artifact_digest": predecessor_artifact_digest,
        "pack_id": pack_id,
        "version": version,
        "replacements": dict(replacement_digests),
        "authority": "unchanged",
    }


def _publish_successor(
    target: Path,
    files: Mapping[str, bytes],
    modes: Mapping[str, int],
    previous: _VerifiedPack,
    version: str,
) -> Path:
    # Pin the output parent before any staging/publication write. Exclusive
    # mkdir and descriptor-relative moves never replace another author's root.
    parent = SecureDirectory(target.parent, create=not target.parent.exists())
    with parent._root_descriptor() as parent_fd:
        with tempfile.TemporaryDirectory(
            prefix=".host-extension-", dir=parent.root
        ) as value:
            staging = Path(value)
            writer = SecureDirectory(staging, create=False)
            for name, content in sorted(files.items()):
                writer.write_bytes_atomic(name, content)
                with writer._parent_descriptor(name, create=False) as (directory, leaf):
                    before = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                    descriptor = os.open(
                        leaf,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                    try:
                        opened = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or opened.st_nlink != 1
                            or (opened.st_dev, opened.st_ino)
                            != (before.st_dev, before.st_ino)
                        ):
                            raise PackAuthoringError("staged artifact identity changed")
                        os.fchmod(descriptor, modes[name])
                    finally:
                        os.close(descriptor)
            successor = _verified_pack(staging, require_signed=False)
            immutable_pack = lambda value: {
                key: item
                for key, item in value.items()
                if key not in {"version", "artifact_digest"}
            }
            if successor.compiled.artifact.version != version or immutable_pack(
                successor.documents["pack.v4.json"]["pack"]
            ) != immutable_pack(previous.documents["pack.v4.json"]["pack"]):
                raise PackAuthoringError("Host Extension successor identity differs")
            if canonical_digest(_authority_closure(previous)) != canonical_digest(
                _authority_closure(successor)
            ):
                raise PackAuthoringError("Host Extension authority closure differs")
            if SIGNED_MANIFEST_RELATIVE in files:
                raise PackAuthoringError("authoring output must remain unsigned")
            # Recheck the pinned parent and staging identity before claiming output.
            with parent._root_descriptor(), writer._root_descriptor() as source_fd:
                identity = (os.fstat(source_fd).st_dev, os.fstat(source_fd).st_ino)
                _rename_noreplace(parent_fd, staging.name, target.name)
                published = os.stat(
                    target.name, dir_fd=parent_fd, follow_symlinks=False
                )
                if (published.st_dev, published.st_ino) != identity:
                    raise PackAuthoringError("successor publication identity changed")
                os.fsync(source_fd)
                os.fsync(parent_fd)
    return target
