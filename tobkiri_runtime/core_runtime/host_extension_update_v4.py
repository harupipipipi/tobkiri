"""Conservative compatibility checks for signed Host Extension successors.

The caller authenticates the publisher signature and Host install policy first.
This module compares finite, verified Pack artifacts without importing their code;
compatibility neither grants authority nor replaces normal Host approval checks.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from tobkiri_host.artifact_compiler import CompiledPack, compile_pack_root
from tobkiri_host.models import ExecutionKind, PackageKind
from tobkiri_protocol.canonical import canonical_digest, strict_loads
from tobkiri_protocol.ids import validate_artifact_digest, validate_canonical_id
from tobkiri_protocol.secure_persistence import SecureDirectory
from tobkiri_protocol.validation import validate_document

from .global_contracts.semver import parse_version
from .pack_signature import SIGNED_MANIFEST_RELATIVE, SIGNED_PACK_VERSION


_METADATA = {
    "pack.v4.json": "pack",
    "contracts.v4.json": "pack_contract_catalog",
    "executables.v4.json": "executable_catalog",
    "artifact-index.v4.json": "pack_artifact_index",
}
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_FILES = 10_000


class HostExtensionUpdateDenied(ValueError):
    """A signed revision cannot inherit an existing Host Extension boundary."""


@dataclass(frozen=True)
class HostExtensionUpdateBinding:
    """Exact identities checked for one compatible signed successor."""

    pack_id: str
    predecessor_digest: str
    artifact_digest: str
    publisher_id: str
    predecessor_version: str
    version: str
    compatibility_digest: str


@dataclass(frozen=True)
class _VerifiedPack:
    compiled: CompiledPack
    documents: Mapping[str, Mapping[str, Any]]
    signed_manifest: Mapping[str, Any] | None


def verify_host_extension_successor(
    predecessor_root: Path,
    successor_root: Path,
    *,
    expected_predecessor_digest: str,
    publisher_id: str,
    predecessor_publisher_id: str | None = None,
    expected_successor_digest: str | None = None,
    signed_manifest: Mapping[str, Any] | None = None,
) -> HostExtensionUpdateBinding:
    """Require an exact, same-publisher Host Extension authority closure.

    ``publisher_id`` must come from signature verification under Host-owned
    publisher policy, not from a client claim. A retained predecessor publisher,
    when available, must come from its authenticated catalog entry. The first
    bundled update is bound to the compiled bundled publisher lineage. The
    caller also verifies that this predecessor is retained or bundled and that
    the successor version has not already been assigned to another digest.

    Only executable bytes and their resulting artifact identities may change.
    Function exports, Contract revisions/schemas/effects, dependency edges,
    capability/network/secret requirements, contribution metadata, executable
    paths, and variant backends/domains remain identical. Base, Shell, Normal,
    and mixed-execution artifacts fail closed. No Pack code is executed here.
    """

    validate_artifact_digest(
        expected_predecessor_digest, field="predecessor_artifact_digest"
    )
    validate_canonical_id(publisher_id, field="publisher_id")
    if expected_successor_digest is not None:
        validate_artifact_digest(
            expected_successor_digest, field="successor_artifact_digest"
        )
    previous = _verified_pack(Path(predecessor_root))
    successor = _verified_pack(
        Path(successor_root), signed_manifest=signed_manifest, require_signed=True
    )
    old = previous.compiled.artifact
    new = successor.compiled.artifact
    if old.digest != expected_predecessor_digest:
        raise HostExtensionUpdateDenied("Host Extension predecessor digest is stale")
    if (
        expected_successor_digest is not None
        and new.digest != expected_successor_digest
    ):
        raise HostExtensionUpdateDenied("Host Extension successor digest is stale")
    if old.pack_id != new.pack_id:
        raise HostExtensionUpdateDenied(
            "Host Extension successor Pack identity differs"
        )
    if (
        old.publisher_lineage != publisher_id
        or new.publisher_lineage != publisher_id
        or (
            predecessor_publisher_id is not None
            and predecessor_publisher_id != publisher_id
        )
    ):
        raise HostExtensionUpdateDenied("Host Extension successor publisher differs")
    old_manifest = previous.documents["pack.v4.json"]
    new_manifest = successor.documents["pack.v4.json"]
    if old_manifest["pack"].get("signing_lineage_digest") != new_manifest["pack"].get(
        "signing_lineage_digest"
    ):
        raise HostExtensionUpdateDenied("Host Extension signing lineage differs")
    _require_signed_identity(successor, publisher_id)
    if previous.signed_manifest is not None:
        _require_signed_identity(previous, publisher_id)
    try:
        old_version = parse_version(old.version)
        new_version = parse_version(new.version)
    except ValueError as error:
        raise HostExtensionUpdateDenied(
            "Host Extension update version is invalid"
        ) from error
    # Re-admission of identical signed bytes is harmless, but cannot rename a
    # retained artifact version. Every changed implementation must move forward.
    if new.digest == old.digest:
        if new.version != old.version:
            raise HostExtensionUpdateDenied("Host Extension artifact version differs")
    elif not old_version < new_version:
        raise HostExtensionUpdateDenied(
            "Host Extension update must have a newer version"
        )
    old_closure = _authority_closure(previous)
    new_closure = _authority_closure(successor)
    if canonical_digest(old_closure) != canonical_digest(new_closure):
        raise HostExtensionUpdateDenied("Host Extension authority closure differs")
    return HostExtensionUpdateBinding(
        pack_id=new.pack_id,
        predecessor_digest=old.digest,
        artifact_digest=new.digest,
        publisher_id=publisher_id,
        predecessor_version=old.version,
        version=new.version,
        compatibility_digest=canonical_digest(new_closure),
    )


def _verified_pack(
    root: Path,
    *,
    signed_manifest: Mapping[str, Any] | None = None,
    require_signed: bool = False,
) -> _VerifiedPack:
    reader = SecureDirectory(root, create=False)
    raw = {
        path: reader.read_bytes_bounded(path, max_bytes=_MAX_FILE_BYTES)
        for path in _METADATA
    }
    documents = {
        path: validate_document(content, _METADATA[path])
        for path, content in raw.items()
    }
    manifest = documents["pack.v4.json"]
    if (
        manifest["pack"]["kind"] != "host_extension"
        or manifest["requirements"]["execution_boundary"] != "host_brokered"
    ):
        raise HostExtensionUpdateDenied("update requires an existing Host Extension")
    index = documents["artifact-index.v4.json"]
    references: dict[str, str] = {}
    for inventory in (manifest["artifacts"], index["artifacts"]):
        paths: set[str] = set()
        for item in inventory:
            path = _relative_path(item["path"])
            if path in paths:
                raise HostExtensionUpdateDenied(
                    "Host Extension artifact path is duplicated"
                )
            paths.add(path)
            digest = item["digest"]
            if path in references and references[path] != digest:
                raise HostExtensionUpdateDenied(
                    "Host Extension artifact inventories disagree"
                )
            references[path] = digest
    if not references or len(references) > _MAX_FILES:
        raise HostExtensionUpdateDenied("Host Extension artifact inventory is unsafe")
    total_bytes = 0
    for path, expected in references.items():
        content = raw.get(path)
        if content is None:
            content = reader.read_bytes_bounded(path, max_bytes=_MAX_FILE_BYTES)
        total_bytes += len(content)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise HostExtensionUpdateDenied(
                "Host Extension artifact size limit exceeded"
            )
        if "sha256:" + hashlib.sha256(content).hexdigest() != expected:
            raise HostExtensionUpdateDenied("Host Extension artifact bytes differ")
    if manifest["integrity"]["contract_catalog_digest"] != (
        "sha256:" + hashlib.sha256(raw["contracts.v4.json"]).hexdigest()
    ):
        raise HostExtensionUpdateDenied(
            "Host Extension Contract catalog digest differs"
        )
    compiled = compile_pack_root(root)
    if (
        compiled.artifact.package_kind is not PackageKind.HOST_EXTENSION
        or not compiled.artifact.functions
        or not compiled.artifact.variants
        or any(
            variant.execution_kind is not ExecutionKind.HOST_EXTENSION
            for variant in compiled.artifact.variants
        )
    ):
        raise HostExtensionUpdateDenied(
            "Host Extension executable inventory is invalid"
        )
    for path, content in raw.items():
        if reader.read_bytes_bounded(path, max_bytes=_MAX_FILE_BYTES) != content:
            raise HostExtensionUpdateDenied(
                "Host Extension metadata changed during validation"
            )
    if signed_manifest is None and require_signed:
        try:
            value = strict_loads(
                reader.read_bytes_bounded(
                    SIGNED_MANIFEST_RELATIVE, max_bytes=4 * 1024 * 1024
                )
            )
        except OSError as error:
            raise HostExtensionUpdateDenied(
                "Host Extension signed manifest is unavailable"
            ) from error
        if not isinstance(value, Mapping):
            raise HostExtensionUpdateDenied("Host Extension signed manifest is invalid")
        signed_manifest = value
    return _VerifiedPack(compiled, documents, signed_manifest)


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
    ):
        raise HostExtensionUpdateDenied("Host Extension artifact path is unsafe")
    return value


def _require_signed_identity(pack: _VerifiedPack, publisher_id: str) -> None:
    signed = pack.signed_manifest
    signature = signed.get("signature") if signed is not None else None
    artifact = pack.compiled.artifact
    manifest = pack.documents["pack.v4.json"]
    expected_contracts: dict[str, str] = {}
    for function in artifact.functions:
        for operation in function.operations:
            previous = expected_contracts.setdefault(
                operation.contract_id, operation.contract_version
            )
            if previous != operation.contract_version:
                raise HostExtensionUpdateDenied(
                    "Host Extension Contract versions are ambiguous"
                )
    if (
        signed is None
        or signed.get("api_version") != SIGNED_PACK_VERSION
        or not isinstance(signature, Mapping)
        or signature.get("algorithm") != "Ed25519"
        or not isinstance(signature.get("key_id"), str)
        or not signature.get("key_id")
        or not isinstance(signature.get("value"), str)
        or not signature.get("value")
        or signed.get("pack_id") != artifact.pack_id
        or signed.get("version") != artifact.version
        or signed.get("publisher_id") != publisher_id
        or signed.get("authority_granted") is not False
        or signed.get("contract_versions") != expected_contracts
        or signed.get("requested_capabilities")
        != sorted(manifest["requirements"]["capabilities"])
    ):
        raise HostExtensionUpdateDenied(
            "Host Extension signed identity or requirements differ"
        )


def _authority_closure(pack: _VerifiedPack) -> Mapping[str, Any]:
    manifest = pack.documents["pack.v4.json"]
    executable = pack.documents["executables.v4.json"]
    index = pack.documents["artifact-index.v4.json"]
    artifact = pack.compiled.artifact
    mutable_paths = {
        item["path"] for item in index["artifacts"] if item["role"] == "runtime"
    } | {"pack.v4.json", "contracts.v4.json", "executables.v4.json"}

    def inventory(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    key: value
                    for key, value in item.items()
                    if not (
                        item["path"] in mutable_paths
                        and key in {"digest", "entrypoint_digest"}
                    )
                }
                for item in items
            ),
            key=lambda item: item["path"],
        )

    return {
        "manifest": {
            key: manifest[key]
            for key in (
                "pack_api_version",
                "contracts",
                "requirements",
                "operation_catalog",
                "provider_catalog",
                "migration",
            )
        },
        "functions": sorted(
            (
                {
                    key: value
                    for key, value in item.items()
                    if key != "implementation_digest"
                }
                for item in manifest["functions"]
            ),
            key=lambda item: item["id"],
        ),
        "contracts": pack.documents["contracts.v4.json"]["contracts"],
        "variants": sorted(
            (
                {
                    key: value
                    for key, value in item.items()
                    if key != "implementation_digest"
                }
                for item in executable["variants"]
            ),
            key=lambda item: item["variant_id"],
        ),
        "materialization_catalog_digest": executable.get(
            "materialization_catalog_digest"
        ),
        "artifacts": inventory(manifest["artifacts"]),
        "artifact_index": inventory(index["artifacts"]),
        "compiled_functions": sorted(
            (
                {
                    key: value
                    for key, value in asdict(item).items()
                    if key != "implementation_digest"
                }
                for item in artifact.functions
            ),
            key=lambda item: item["function_id"],
        ),
        "compiled_variants": sorted(
            (
                {key: value for key, value in asdict(item).items() if key != "digest"}
                for item in artifact.variants
            ),
            key=lambda item: item["variant_id"],
        ),
        "routes": [
            {
                "contract_id": key[0],
                "operation_id": key[1],
                **{
                    name: value
                    for name, value in route.items()
                    if name != "catalog_digest"
                },
            }
            for key, route in sorted(pack.compiled.routes.items())
        ],
    }


__all__ = [
    "HostExtensionUpdateBinding",
    "HostExtensionUpdateDenied",
    "verify_host_extension_successor",
]
