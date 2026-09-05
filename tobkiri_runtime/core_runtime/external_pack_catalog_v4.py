"""Host-owned signed external Normal Pack catalog and quarantine CAS."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import ExitStack, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import tempfile
from typing import Any, Callable, Iterator, Mapping
import unicodedata

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document, validate_file

from .hmac_key_manager import generate_or_load_signing_key
from .pack_artifact_integrity import (
    HostPolicyLock,
    exclusive_host_policy_lock,
    host_policy_identity,
    read_host_policy_snapshot,
    verify_host_install_binding,
)
from .pack_boundary import PackBoundaryError, load_pack_catalog, resolve_pack_root
from .pack_signature import SIGNED_MANIFEST_RELATIVE, verify_signed_pack


_CATALOG_VERSION = "io.tobkiri.external-normal-pack-catalog.v4"
_ENTRY_VERSION = "io.tobkiri.external-normal-pack-catalog-entry.v4"
_MAX_CATALOG_BYTES = 16 * 1024 * 1024
_MAX_PACK_FILES = 10_000
_MAX_PACK_FILE_BYTES = 128 * 1024 * 1024
_MAX_PACK_TOTAL_BYTES = 512 * 1024 * 1024
_DIGEST_PREFIX = "sha256:"
_V4_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class ExternalPackCatalogDenied(RuntimeError):
    """The Host external Normal Pack catalog or signed Pack failed closed."""


@dataclass(frozen=True)
class ExternalPackCatalogSnapshot:
    """Verified visible external Normal Pack records and immutable roots."""

    revision: str
    records: Mapping[str, Mapping[str, Any]]
    roots: Mapping[str, Path]
    entries: Mapping[str, Mapping[str, Any]]
    journal: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _TrustPolicySnapshot:
    identity: tuple[int, str]
    pack_id: str
    publisher_id: str
    key_id: str
    install_record: Mapping[str, Any]
    publisher: Mapping[str, Any]


def admit_signed_external_pack(
    source_root: Path,
    *,
    trust_store_path: Path,
    fault_injector: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Verify and atomically admit one signed external Normal v4 Pack.

    This is a Host-side installation port.  The live Pack-control Contract only
    accepts a Pack ID after this transaction has committed; no source path or
    catalog document crosses the client route.
    """

    user_data = _user_data_root()
    catalog_path = _catalog_path(user_data)
    with _admission_policy_locks(trust_store_path, catalog_path) as policy_locks:
        trust_policy_lock, _catalog_policy_lock = policy_locks
        return _admit_signed_external_pack_locked(
            source_root,
            trust_store_path=trust_store_path,
            user_data=user_data,
            catalog_path=catalog_path,
            trust_policy_lock=trust_policy_lock,
            fault_injector=fault_injector,
        )


@contextmanager
def _admission_policy_locks(
    trust_store_path: Path,
    catalog_path: Path,
) -> Iterator[tuple[HostPolicyLock, HostPolicyLock]]:
    stack = ExitStack()
    try:
        trust_policy_lock = stack.enter_context(
            exclusive_host_policy_lock(trust_store_path)
        )
        catalog_policy_lock = stack.enter_context(
            exclusive_host_policy_lock(catalog_path)
        )
    except (OSError, ValueError) as error:
        stack.close()
        raise ExternalPackCatalogDenied(
            "external Pack policy transaction is unsafe"
        ) from error
    try:
        yield trust_policy_lock, catalog_policy_lock
    finally:
        stack.close()


def _admit_signed_external_pack_locked(
    source_root: Path,
    *,
    trust_store_path: Path,
    user_data: Path,
    catalog_path: Path,
    trust_policy_lock: HostPolicyLock,
    fault_injector: Callable[[str], None] | None,
) -> Mapping[str, Any]:
    source = Path(source_root)
    source_identity = _directory_identity(source)
    signed_manifest, install_record, trust_snapshot = _verify_signed_source(
        source,
        trust_store_path,
        trust_policy_lock=trust_policy_lock,
    )
    compiled = compile_pack_root(source)
    pack_id = compiled.artifact.pack_id
    if pack_id != str(signed_manifest["pack_id"]):
        raise ExternalPackCatalogDenied("signed and v4 Pack identities differ")
    if pack_id in load_pack_catalog():
        raise ExternalPackCatalogDenied(
            "external Normal Pack cannot shadow the immutable bundled catalog"
        )
    record = _project_catalog_record(source, signed_manifest)
    artifact_digest = compiled.artifact.digest
    content_digest = canonical_digest(
        {
            "signed_manifest": signed_manifest,
            "artifact_digest": artifact_digest,
        }
    )
    artifact_parent = _artifact_store(user_data) / artifact_digest.removeprefix(_DIGEST_PREFIX)
    final_root = artifact_parent / pack_id
    artifact_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if artifact_parent.is_symlink() or final_root.is_symlink():
        raise ExternalPackCatalogDenied("external Pack artifact store is symlinked")
    temporary = Path(tempfile.mkdtemp(prefix=f".{pack_id}.", dir=artifact_parent))
    previous: Mapping[str, Any] | None = None
    prepared_written = False
    promoted = False
    try:
        _copy_signed_pack(source, temporary, signed_manifest)
        _verify_cas_copy(
            temporary,
            signed_manifest,
            install_record,
            artifact_digest=artifact_digest,
            expected_content_digest=content_digest,
            trust_store_path=trust_store_path,
            authorized_install_path=source,
            expected_trust_snapshot=trust_snapshot,
            trust_policy_lock=trust_policy_lock,
        )
        if _directory_identity(source) != source_identity:
            raise ExternalPackCatalogDenied("signed Pack root changed during installation")
        unsigned = _read_authenticated_catalog(catalog_path, allow_missing=True)
        entries = dict(unsigned["entries"])
        journal = list(unsigned["journal"])
        current = entries.get(pack_id)
        if isinstance(current, Mapping):
            previous = _visible_entry(current)
            if previous is not None and previous.get("artifact_digest") == artifact_digest:
                existing_root = _entry_root(previous, user_data)
                _verify_entry_root(pack_id, previous, existing_root)
                _require_current_trust_policy(
                    trust_store_path,
                    trust_snapshot,
                    trust_policy_lock=trust_policy_lock,
                )
                return dict(previous)
            if previous is not None:
                raise ExternalPackCatalogDenied(
                    "external Normal Pack ID is already bound to a different digest"
                )
        entry = {
            "version": _ENTRY_VERSION,
            "state": "prepared",
            "pack_id": pack_id,
            "version_string": str(signed_manifest["version"]),
            "artifact_digest": artifact_digest,
            "content_digest": content_digest,
            "publisher_id": str(signed_manifest["publisher_id"]),
            "key_id": str(signed_manifest["signature"]["key_id"]),
            "catalog_record": record,
            "store_token": f"{artifact_digest.removeprefix(_DIGEST_PREFIX)}/{pack_id}",
            "previous": dict(previous) if previous is not None else None,
            "transaction_nonce": secrets.token_hex(32),
        }
        entries[pack_id] = entry
        _write_authenticated_catalog(catalog_path, entries, journal)
        prepared_written = True
        _inject(fault_injector, "prepared")
        if final_root.exists():
            _verify_entry_root(pack_id, {**entry, "state": "committed"}, final_root)
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, final_root)
            promoted = True
        _chmod_read_only_tree(final_root)
        _verify_entry_root(pack_id, {**entry, "state": "committed"}, final_root)
        _inject(fault_injector, "promoted")
        committed: dict[str, Any] = {
            key: value
            for key, value in entry.items()
            if key not in {"previous", "transaction_nonce"}
        }
        committed["state"] = "committed"
        committed["admission_id"] = str(entry["transaction_nonce"])
        root_metadata = final_root.lstat()
        committed["root_device"] = int(root_metadata.st_dev)
        committed["root_inode"] = int(root_metadata.st_ino)
        _require_current_trust_policy(
            trust_store_path,
            trust_snapshot,
            trust_policy_lock=trust_policy_lock,
        )
        latest = _read_authenticated_catalog(catalog_path, allow_missing=False)
        latest_entries = dict(latest["entries"])
        latest_journal = list(latest["journal"])
        current_prepared = latest_entries.get(pack_id)
        if current_prepared != entry:
            raise ExternalPackCatalogDenied("external Pack catalog transaction was replaced")
        latest_entries[pack_id] = committed
        latest_journal.append(_install_journal_event(committed, latest_journal))
        _write_authenticated_catalog(catalog_path, latest_entries, latest_journal)
        _inject(fault_injector, "committed")
        return committed
    except Exception:
        if prepared_written:
            try:
                latest = _read_authenticated_catalog(catalog_path, allow_missing=False)
                entries = dict(latest["entries"])
                journal = list(latest["journal"])
                current = entries.get(pack_id)
                if isinstance(current, Mapping) and current.get("state") == "prepared":
                    if previous is None:
                        entries.pop(pack_id, None)
                    else:
                        entries[pack_id] = dict(previous)
                    _write_authenticated_catalog(catalog_path, entries, journal)
            except Exception:
                # A prepared entry is never visible to readers; recovery can
                # safely leave it as an inert transaction marker.
                pass
        if promoted and final_root.exists() and previous is None:
            try:
                _make_tree_writable(final_root)
                shutil.rmtree(final_root)
            except OSError:
                pass
        raise
    finally:
        if temporary.exists():
            _make_tree_writable(temporary)
            shutil.rmtree(temporary)


def load_external_pack_catalog() -> ExternalPackCatalogSnapshot:
    """Load only committed, authenticated external Normal Pack entries."""

    user_data = _user_data_root()
    unsigned = _read_authenticated_catalog(_catalog_path(user_data), allow_missing=True)
    records: dict[str, Mapping[str, Any]] = {}
    roots: dict[str, Path] = {}
    entries: dict[str, Mapping[str, Any]] = {}
    for pack_id, raw_entry in sorted(unsigned["entries"].items()):
        if not isinstance(raw_entry, Mapping):
            raise ExternalPackCatalogDenied("external Pack catalog entry is malformed")
        entry = _visible_entry(raw_entry)
        if entry is None:
            continue
        _validate_entry(pack_id, entry)
        root = _entry_root(entry, user_data)
        _verify_entry_root(pack_id, entry, root)
        record = entry["catalog_record"]
        if not isinstance(record, Mapping) or record.get("pack_id") != pack_id:
            raise ExternalPackCatalogDenied("external Pack catalog record identity is invalid")
        records[pack_id] = dict(record)
        roots[pack_id] = root
        entries[pack_id] = dict(entry)
    revision = canonical_digest(
        {"version": _CATALOG_VERSION, "entries": entries, "journal": unsigned["journal"]}
    )
    return ExternalPackCatalogSnapshot(
        revision, records, roots, entries, tuple(unsigned["journal"])
    )


def load_admitted_pack_catalog() -> dict[str, Mapping[str, Any]]:
    """Return bundled records plus admitted Normal Packs without mutation."""

    bundled = load_pack_catalog()
    external = load_external_pack_catalog()
    collisions = set(bundled) & set(external.records)
    if collisions:
        raise ExternalPackCatalogDenied(
            f"external Pack catalog shadows bundled Pack IDs: {sorted(collisions)}"
        )
    result = dict(bundled)
    result.update(external.records)
    return result


def control_catalog_revision() -> str:
    """Bind the immutable generated catalog and Host external Normal Pack revision."""

    bundled = load_pack_catalog()
    external = load_external_pack_catalog()
    return canonical_digest(
        {
            "bundled": bundled,
            "external_normal_packs": external.entries,
        }
    )


def external_pack_content_digest(pack_id: str) -> str | None:
    """Return a reverified CAS content digest for one external Normal Pack."""

    entry = load_external_pack_catalog().entries.get(str(pack_id or "").strip())
    if entry is None:
        return None
    return str(entry["content_digest"])


def load_admitted_external_executable_catalog(
    pack_id: str,
    expected_manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Load one verified executable catalog for an admitted external Pack.

    External Packs are not part of the sealed base-bundle lock.  Their
    executable catalogs therefore enter Profile resolution only through the
    Host's signed, content-addressed admission registry.  The caller supplies
    the manifest it selected for the closure; this prevents a catalog for a
    different admitted revision from being silently combined with it.
    """

    normalized = str(pack_id or "").strip()
    if not normalized or not isinstance(expected_manifest, Mapping):
        raise ExternalPackCatalogDenied("external Pack executable request is invalid")
    snapshot = load_external_pack_catalog()
    entry = snapshot.entries.get(normalized)
    root = snapshot.roots.get(normalized)
    if entry is None or root is None:
        raise ExternalPackCatalogDenied("external Pack is not admitted")
    try:
        manifest = validate_file(root / "pack.v4.json", "pack")
        executable = validate_file(root / "executables.v4.json", "executable_catalog")
        _require_external_catalog_identity(executable)
        compiled = compile_pack_root(root)
    except Exception as error:
        raise ExternalPackCatalogDenied(
            "external Pack executable catalog verification failed"
        ) from error
    # Reverify the signed CAS root after all individual reads.  A mutation or
    # root replacement is an admission failure, never a fallback to an
    # unverified catalog document.
    _verify_entry_root(normalized, entry, root)
    if (
        canonical_digest(manifest) != canonical_digest(expected_manifest)
        or manifest["pack"]["id"] != normalized
        or manifest["pack"]["artifact_digest"] != entry["artifact_digest"]
        or compiled.artifact.pack_id != normalized
        or compiled.artifact.digest != entry["artifact_digest"]
        or executable["pack_id"] != normalized
        or executable["source_identity"]
        != manifest["integrity"]["source_identity"]
    ):
        raise ExternalPackCatalogDenied(
            "external Pack executable catalog does not match the admitted manifest"
        )
    return dict(executable)


def resolve_admitted_pack_root(
    pack_id: str,
    bundled_root: Path | None = None,
) -> Path:
    """Resolve one exact bundled or committed external Normal Pack root."""

    normalized = str(pack_id or "").strip()
    bundled = load_pack_catalog()
    if normalized in bundled:
        return resolve_pack_root(normalized, bundled_root)
    snapshot = load_external_pack_catalog()
    root = snapshot.roots.get(normalized)
    if root is None:
        raise PackBoundaryError(
            f"Pack is absent from the Host v4 catalog: {normalized}"
        )
    return root


def resolve_admitted_pack_roots(
    pack_ids: tuple[str, ...],
    bundled_root: Path | None = None,
) -> dict[str, Path]:
    """Resolve exactly the selected Host catalog Pack IDs."""

    normalized = tuple(sorted(str(item).strip() for item in pack_ids))
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise PackBoundaryError("selected Pack IDs must be unique and non-empty")
    return {
        pack_id: resolve_admitted_pack_root(pack_id, bundled_root)
        for pack_id in normalized
    }


def _verify_signed_source(
    source: Path,
    trust_store_path: Path,
    *,
    authorized_install_path: Path | None = None,
    trust_policy_lock: HostPolicyLock | None = None,
) -> tuple[dict[str, Any], Mapping[str, Any], _TrustPolicySnapshot]:
    source_identity = _directory_identity(source)
    unresolved_trust_path = Path(trust_store_path).expanduser()
    trust_path = Path(os.path.abspath(unresolved_trust_path))
    if trust_path.is_symlink():
        raise ExternalPackCatalogDenied("publisher trust store permissions are unsafe")
    manifest_path = source / SIGNED_MANIFEST_RELATIVE
    signed_manifest = _read_json_nofollow(manifest_path, _MAX_CATALOG_BYTES)
    if not isinstance(signed_manifest, dict):
        raise ExternalPackCatalogDenied("signed Pack manifest is invalid")
    try:
        trust_store = read_host_policy_snapshot(
            trust_path,
            policy_lock=trust_policy_lock,
        )
        policy_identity = host_policy_identity(trust_store)
    except (OSError, ValueError) as error:
        raise ExternalPackCatalogDenied(
            "publisher trust store permissions are unsafe"
        ) from error
    if not isinstance(trust_store, Mapping):
        raise ExternalPackCatalogDenied("publisher trust store is invalid")
    if not policy_identity[1]:
        raise ExternalPackCatalogDenied(
            "publisher trust store has no authenticated generation"
        )
    pack_id = str(signed_manifest.get("pack_id") or "")
    install_records = trust_store.get("install_records")
    install_records = install_records if isinstance(install_records, Mapping) else {}
    install_record = install_records.get(pack_id)
    if not isinstance(install_record, Mapping):
        raise ExternalPackCatalogDenied("signed Pack has no Host install policy")
    if install_record.get("signature_required") is not True:
        raise ExternalPackCatalogDenied("external v4 Pack signature is required")
    publisher_id = str(install_record.get("publisher_id") or "")
    publishers = trust_store.get("publishers")
    publishers = publishers if isinstance(publishers, Mapping) else {}
    publisher = publishers.get(publisher_id)
    if not isinstance(publisher, Mapping):
        raise ExternalPackCatalogDenied("external Pack publisher is untrusted")
    namespaces = tuple(str(item) for item in publisher.get("allowed_pack_namespaces") or ())
    if namespaces and not any(
        pack_id == namespace or pack_id.startswith(f"{namespace}.")
        for namespace in namespaces
    ):
        raise ExternalPackCatalogDenied("publisher cannot sign this Pack namespace")
    public_key = serialization.load_pem_public_key(
        str(publisher.get("public_key_pem") or "").encode("utf-8")
    )
    if not isinstance(public_key, Ed25519PublicKey):
        raise ExternalPackCatalogDenied("publisher trust key is not Ed25519")
    from rumi_ai import __version__ as core_version

    verify_signed_pack(
        source,
        signed_manifest,
        public_key,
        expected_publisher_id=publisher_id,
        expected_pack_id=pack_id,
        expected_version=str(install_record.get("installed_version") or ""),
        expected_key_id=str(install_record.get("key_id") or ""),
        expected_contract_versions=dict(install_record.get("contract_versions") or {}),
        expected_capabilities=[
            str(item) for item in install_record.get("requested_capabilities") or ()
        ],
        revoked_key_ids={
            str(item) for item in publisher.get("revoked_key_ids") or ()
        },
        core_version=str(core_version),
    )
    try:
        verify_host_install_binding(
            source,
            install_record,
            signed_manifest,
            authorized_install_path=authorized_install_path,
        )
    except ValueError as error:
        raise ExternalPackCatalogDenied(
            "signed Pack does not match its Host install binding"
        ) from error
    if _directory_identity(source) != source_identity:
        raise ExternalPackCatalogDenied("signed Pack root changed during verification")
    return (
        signed_manifest,
        dict(install_record),
        _TrustPolicySnapshot(
            identity=policy_identity,
            pack_id=pack_id,
            publisher_id=publisher_id,
            key_id=str(signed_manifest["signature"]["key_id"]),
            install_record=dict(install_record),
            publisher=dict(publisher),
        ),
    )


def _require_current_trust_policy(
    trust_store_path: Path,
    expected: _TrustPolicySnapshot,
    *,
    trust_policy_lock: HostPolicyLock,
) -> None:
    try:
        trust_store = read_host_policy_snapshot(
            trust_store_path,
            policy_lock=trust_policy_lock,
        )
        current_identity = host_policy_identity(trust_store)
    except (OSError, ValueError) as error:
        raise ExternalPackCatalogDenied(
            "publisher trust policy changed during admission"
        ) from error
    install_records = trust_store.get("install_records")
    install_records = install_records if isinstance(install_records, Mapping) else {}
    publishers = trust_store.get("publishers")
    publishers = publishers if isinstance(publishers, Mapping) else {}
    install_record = install_records.get(expected.pack_id)
    publisher = publishers.get(expected.publisher_id)
    revoked = (
        {str(item) for item in publisher.get("revoked_key_ids") or ()}
        if isinstance(publisher, Mapping)
        else set()
    )
    if (
        current_identity != expected.identity
        or install_record != expected.install_record
        or publisher != expected.publisher
        or expected.key_id in revoked
    ):
        raise ExternalPackCatalogDenied(
            "publisher trust policy changed during admission"
        )


def _project_catalog_record(
    root: Path,
    signed_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = validate_file(root / "pack.v4.json", "pack")
    executable = validate_file(root / "executables.v4.json", "executable_catalog")
    _require_external_catalog_identity(executable)
    pack_id = str(manifest["pack"]["id"])
    if manifest["pack"]["kind"] != "normal_sandbox":
        raise ExternalPackCatalogDenied(
            "external admission accepts only Normal Sandbox Packs"
        )
    if (
        pack_id != signed_manifest.get("pack_id")
        or manifest["pack"]["version"] != signed_manifest.get("version")
    ):
        raise ExternalPackCatalogDenied("signed Pack and v4 manifest disagree")
    operations_by_contract: dict[str, list[dict[str, Any]]] = {}
    provider_by_contract: dict[str, str] = {}
    for variant in executable["variants"]:
        for operation in variant["operations"]:
            contract_id = str(operation["contract_id"])
            provider = str(variant["function_id"])
            existing = provider_by_contract.setdefault(contract_id, provider)
            if existing != provider:
                raise ExternalPackCatalogDenied("external Contract provider is ambiguous")
            operations_by_contract.setdefault(contract_id, []).append(
                {
                    "id": str(operation["operation_id"]),
                    "entrypoint_id": str(operation["operation_id"]),
                    "implementation_digest": str(variant["implementation_digest"]),
                }
            )
    provided_contracts: list[dict[str, Any]] = []
    for contract in manifest["contracts"]:
        contract_id = str(contract["contract_id"])
        contract_operations: list[dict[str, Any]] = operations_by_contract.get(
            contract_id, []
        )
        provider_id = provider_by_contract.get(contract_id, "")
        if not contract_operations or not provider_id:
            raise ExternalPackCatalogDenied("external v4 Contract executable is missing")
        provided_contracts.append(
            {
                "contract_id": contract_id,
                "provider_id": provider_id,
                "owner": pack_id,
                "cardinality": "one",
                "failure": "fail_closed",
                "isolation": "process",
                "version": "1.0.0",
                "required_capabilities": sorted(
                    str(item).removeprefix("capability:")
                    for item in manifest["requirements"]["capabilities"]
                ),
                "operations": sorted(
                    contract_operations,
                    key=_catalog_operation_id,
                ),
            }
        )
    return {
        "pack_id": pack_id,
        "display_name": str(manifest["pack"]["display_name"]),
        "description": "Signed external Tobkiri v4 Pack.",
        "version": str(manifest["pack"]["version"]),
        "kind": str(manifest["pack"]["kind"]),
        "artifact_digest": str(manifest["pack"]["artifact_digest"]),
        "authority": "host-signed-external-normal-v4",
        "approval_policy": "explicit",
        "capabilities": sorted(
            str(item) for item in manifest["requirements"]["capabilities"]
        ),
        "dependencies": dict(manifest["requirements"]["pack_dependencies"]),
        "provided_contracts": sorted(
            provided_contracts, key=lambda item: item["contract_id"]
        ),
        "required_contracts": list(
            manifest["requirements"]["contract_dependencies"]
        ),
        "runtime_artifacts": list(manifest["artifacts"]),
        "execution_boundary": str(manifest["requirements"]["execution_boundary"]),
        "workspace_boundary": str(manifest["requirements"]["workspace_boundary"]),
        "network": dict(manifest["requirements"]["network"]),
        "secrets": list(manifest["requirements"]["secrets"]),
        "legacy_ids": [],
        "legacy_operations": [],
        "source_provenance": {
            "mode": "host-signed-external-normal-catalog",
            "publisher_id": str(signed_manifest["publisher_id"]),
            "key_id": str(signed_manifest["signature"]["key_id"]),
        },
    }


def _copy_signed_pack(
    source: Path,
    destination: Path,
    signed_manifest: Mapping[str, Any],
) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source, directory_flags)
    destination_descriptor = os.open(destination, directory_flags)
    source_identity = _stat_identity(os.fstat(source_descriptor))
    total_bytes = 0
    collision_paths: dict[str, str] = {}
    try:
        files = signed_manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > _MAX_PACK_FILES:
            raise ExternalPackCatalogDenied("signed Pack file inventory is unsafe")
        for item in files:
            if not isinstance(item, Mapping):
                raise ExternalPackCatalogDenied("signed Pack file inventory is unsafe")
            relative = _safe_relative(str(item["path"]))
            _require_normalized_copy_path(relative, collision_paths)
            content, source_mode = _read_regular_from_root_descriptor(
                source_descriptor,
                relative,
            )
            total_bytes += len(content)
            if (
                len(content) > _MAX_PACK_FILE_BYTES
                or total_bytes > _MAX_PACK_TOTAL_BYTES
                or len(content) != item["size"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
                or stat.S_IMODE(source_mode) != item["mode"]
            ):
                raise ExternalPackCatalogDenied("signed Pack changed while copying")
            _write_regular_to_root_descriptor(
                destination_descriptor,
                relative,
                content,
                int(item["mode"]),
            )
        manifest_content = (
            json.dumps(signed_manifest, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        _write_regular_to_root_descriptor(
            destination_descriptor,
            SIGNED_MANIFEST_RELATIVE,
            manifest_content,
            0o400,
        )
        if _stat_identity(os.fstat(source_descriptor)) != source_identity:
            raise ExternalPackCatalogDenied(
                "signed Pack root changed while copying"
            )
        os.fsync(destination_descriptor)
    finally:
        os.close(destination_descriptor)
        os.close(source_descriptor)


def _require_normalized_copy_path(
    relative: str,
    collision_paths: dict[str, str],
) -> None:
    parts = PurePosixPath(relative).parts
    for index in range(1, len(parts) + 1):
        prefix = "/".join(parts[:index])
        normalized = unicodedata.normalize("NFC", prefix)
        if prefix != normalized:
            raise ExternalPackCatalogDenied(
                "signed Pack contains a non-canonical path"
            )
        collision_key = normalized.casefold()
        previous = collision_paths.setdefault(collision_key, prefix)
        if previous != prefix:
            raise ExternalPackCatalogDenied(
                "signed Pack contains a normalized path collision"
            )


def _read_regular_from_root_descriptor(
    root_descriptor: int,
    relative: str,
) -> tuple[bytes, int]:
    parts = PurePosixPath(relative).parts
    parent_descriptor, ancestors = _open_directory_components(
        root_descriptor,
        parts[:-1],
        create=False,
    )
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        metadata = os.stat(
            parts[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(parts[-1], flags, dir_fd=parent_descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _stat_identity(metadata) != _stat_identity(before)
            or before.st_size > _MAX_PACK_FILE_BYTES
        ):
            raise ExternalPackCatalogDenied(
                "signed Pack entry identity is unsafe"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, _MAX_PACK_FILE_BYTES + 1),
            )
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PACK_FILE_BYTES:
                raise ExternalPackCatalogDenied(
                    "signed Pack entry exceeds the size limit"
                )
            chunks.append(chunk)
        if _stat_identity(before) != _stat_identity(os.fstat(descriptor)):
            raise ExternalPackCatalogDenied(
                "signed Pack entry changed while copying"
            )
        _verify_open_ancestor_identities(ancestors)
        return b"".join(chunks), before.st_mode
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _close_open_ancestors(ancestors)


def _write_regular_to_root_descriptor(
    root_descriptor: int,
    relative: str,
    content: bytes,
    mode: int,
) -> None:
    parts = PurePosixPath(relative).parts
    parent_descriptor, ancestors = _open_directory_components(
        root_descriptor,
        parts[:-1],
        create=True,
    )
    descriptor: int | None = None
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            parts[-1],
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        _verify_open_ancestor_identities(ancestors, allow_directory_mtime_change=True)
        os.fsync(parent_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _close_open_ancestors(ancestors)


def _open_directory_components(
    root_descriptor: int,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> tuple[int, list[tuple[int, tuple[int, int, int, int]]]]:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.dup(root_descriptor)
    ancestors: list[tuple[int, tuple[int, int, int, int]]] = [
        (descriptor, _stat_identity(os.fstat(descriptor)))
    ]
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(part, directory_flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or _stat_identity(metadata) != _stat_identity(opened)
            ):
                os.close(child)
                raise ExternalPackCatalogDenied(
                    "signed Pack directory identity is unsafe"
                )
            descriptor = child
            ancestors.append((descriptor, _stat_identity(opened)))
        return descriptor, ancestors
    except Exception:
        _close_open_ancestors(ancestors)
        raise


def _verify_open_ancestor_identities(
    ancestors: list[tuple[int, tuple[int, int, int, int]]],
    *,
    allow_directory_mtime_change: bool = False,
) -> None:
    for descriptor, expected in ancestors:
        actual = _stat_identity(os.fstat(descriptor))
        if allow_directory_mtime_change:
            if actual[:2] != expected[:2]:
                raise ExternalPackCatalogDenied(
                    "signed Pack directory changed while copying"
                )
        elif actual != expected:
            raise ExternalPackCatalogDenied(
                "signed Pack directory changed while copying"
            )


def _close_open_ancestors(
    ancestors: list[tuple[int, tuple[int, int, int, int]]],
) -> None:
    for descriptor, _identity in reversed(ancestors):
        os.close(descriptor)


def _catalog_operation_id(operation: Mapping[str, Any]) -> str:
    return str(operation.get("id") or "")


def _verify_cas_copy(
    root: Path,
    signed_manifest: Mapping[str, Any],
    install_record: Mapping[str, Any],
    *,
    artifact_digest: str,
    expected_content_digest: str,
    trust_store_path: Path,
    authorized_install_path: Path,
    expected_trust_snapshot: _TrustPolicySnapshot,
    trust_policy_lock: HostPolicyLock,
) -> None:
    copied_manifest, copied_install_record, copied_trust_snapshot = _verify_signed_source(
        root,
        trust_store_path,
        authorized_install_path=authorized_install_path,
        trust_policy_lock=trust_policy_lock,
    )
    compiled = compile_pack_root(root)
    content_digest = canonical_digest(
        {
            "signed_manifest": copied_manifest,
            "artifact_digest": compiled.artifact.digest,
        }
    )
    if (
        compiled.artifact.digest != artifact_digest
        or content_digest != expected_content_digest
        or copied_manifest != signed_manifest
        or copied_install_record != install_record
        or copied_trust_snapshot != expected_trust_snapshot
    ):
        raise ExternalPackCatalogDenied("content-addressed Pack verification failed")


def _verify_entry_root(
    pack_id: str,
    entry: Mapping[str, Any],
    root: Path,
) -> None:
    metadata = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ExternalPackCatalogDenied("external Pack root is unavailable")
    expected_device = entry.get("root_device")
    expected_inode = entry.get("root_inode")
    if expected_device is not None or expected_inode is not None:
        if (
            expected_device != int(metadata.st_dev)
            or expected_inode != int(metadata.st_ino)
        ):
            raise ExternalPackCatalogDenied("external Pack root identity changed")
    try:
        executable = validate_file(root / "executables.v4.json", "executable_catalog")
        _require_external_catalog_identity(executable)
        compiled = compile_pack_root(root)
    except Exception as error:
        raise ExternalPackCatalogDenied(
            "external Pack artifact digest verification failed"
        ) from error
    if (
        compiled.artifact.pack_id != pack_id
        or compiled.artifact.digest != entry.get("artifact_digest")
    ):
        raise ExternalPackCatalogDenied("external Normal Pack root identity changed")
    signed_manifest = _read_json_nofollow(root / SIGNED_MANIFEST_RELATIVE, _MAX_CATALOG_BYTES)
    if not isinstance(signed_manifest, Mapping):
        raise ExternalPackCatalogDenied("external Normal Pack signature manifest is invalid")
    _verify_signed_file_inventory(root, signed_manifest)
    content_digest = canonical_digest(
        {
            "signed_manifest": signed_manifest,
            "artifact_digest": compiled.artifact.digest,
        }
    )
    if content_digest != entry.get("content_digest"):
        raise ExternalPackCatalogDenied("external Normal Pack content digest changed")


def _require_external_catalog_identity(executable: Mapping[str, Any]) -> None:
    """Reject bundle-only executable catalog aliases from external Packs."""

    if executable.get("materialization_catalog_digest") is not None:
        raise ExternalPackCatalogDenied(
            "external Pack cannot replace its executable catalog identity"
        )


def _validate_entry(pack_id: str, entry: Mapping[str, Any]) -> None:
    required = {
        "version",
        "state",
        "pack_id",
        "version_string",
        "artifact_digest",
        "content_digest",
        "publisher_id",
        "key_id",
        "catalog_record",
        "store_token",
        "admission_id",
        "root_device",
        "root_inode",
    }
    if set(entry) != required or entry.get("version") != _ENTRY_VERSION:
        raise ExternalPackCatalogDenied("external Normal Pack catalog entry fields are invalid")
    if entry.get("state") != "committed" or entry.get("pack_id") != pack_id:
        raise ExternalPackCatalogDenied("external Normal Pack catalog entry state is invalid")
    if _V4_ID.fullmatch(pack_id) is None:
        raise ExternalPackCatalogDenied("external Normal Pack catalog Pack ID is invalid")
    for field in ("artifact_digest", "content_digest"):
        value = str(entry.get(field) or "")
        if len(value) != 71 or not value.startswith(_DIGEST_PREFIX):
            raise ExternalPackCatalogDenied("external Normal Pack catalog digest is invalid")
    if not all(
        isinstance(entry.get(field), int) and int(entry[field]) >= 0
        for field in ("root_device", "root_inode")
    ):
        raise ExternalPackCatalogDenied("external Pack root identity is invalid")
    admission_id = str(entry.get("admission_id") or "")
    if len(admission_id) != 64 or any(
        character not in "0123456789abcdef" for character in admission_id
    ):
        raise ExternalPackCatalogDenied("external Pack admission identity is invalid")


def _visible_entry(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if entry.get("state") == "committed":
        return entry
    if entry.get("state") != "prepared":
        raise ExternalPackCatalogDenied("external Normal Pack catalog transaction state is invalid")
    previous = entry.get("previous")
    if previous is None:
        return None
    if not isinstance(previous, Mapping) or previous.get("state") != "committed":
        raise ExternalPackCatalogDenied("external Normal Pack catalog rollback entry is invalid")
    return previous


def _entry_root(entry: Mapping[str, Any], user_data: Path) -> Path:
    token = _safe_relative(str(entry.get("store_token") or ""))
    root = _artifact_store(user_data)
    candidate = root.joinpath(*PurePosixPath(token).parts)
    current = root
    for part in PurePosixPath(token).parts:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise ExternalPackCatalogDenied(
                "external Pack artifact path is unavailable"
            ) from error
        if current.is_symlink() or (
            current != candidate and not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ExternalPackCatalogDenied(
                "external Pack artifact path contains a symlink"
            )
    resolved = candidate.resolve(strict=True)
    if root.resolve() not in resolved.parents:
        raise ExternalPackCatalogDenied("external Normal Pack artifact store token escapes")
    return resolved


def _read_authenticated_catalog(path: Path, *, allow_missing: bool) -> dict[str, Any]:
    if not path.exists():
        if allow_missing:
            return {"version": _CATALOG_VERSION, "entries": {}, "journal": []}
        raise ExternalPackCatalogDenied("external Normal Pack catalog is unavailable")
    value = _read_json_nofollow(path, _MAX_CATALOG_BYTES)
    if not isinstance(value, dict) or set(value) != {
        "version",
        "entries",
        "journal",
        "signature",
    }:
        raise ExternalPackCatalogDenied("external Normal Pack catalog fields are invalid")
    try:
        value = validate_document(value, "external_pack_catalog")
    except Exception as error:
        raise ExternalPackCatalogDenied(
            "external Pack catalog schema validation failed"
        ) from error
    signature = str(value.pop("signature") or "")
    expected = hmac.new(_catalog_key(), _canonical_bytes(value), hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise ExternalPackCatalogDenied("external Normal Pack catalog authentication failed")
    if (
        value.get("version") != _CATALOG_VERSION
        or not isinstance(value.get("entries"), dict)
        or not isinstance(value.get("journal"), list)
    ):
        raise ExternalPackCatalogDenied("external Normal Pack catalog payload is invalid")
    _validate_install_journal(value["journal"])
    return value


def _write_authenticated_catalog(
    path: Path,
    entries: Mapping[str, Any],
    journal: list[Mapping[str, Any]],
) -> None:
    payload: dict[str, Any] = {
        "version": _CATALOG_VERSION,
        "entries": dict(sorted(entries.items())),
        "journal": journal,
    }
    payload["signature"] = hmac.new(
        _catalog_key(), _canonical_bytes(payload), hashlib.sha256
    ).hexdigest()
    _atomic_private_json(path, payload)


def _install_journal_event(
    entry: Mapping[str, Any],
    journal: list[Mapping[str, Any]],
) -> dict[str, Any]:
    previous_digest = (
        str(journal[-1]["event_digest"])
        if journal
        else f"{_DIGEST_PREFIX}{'0' * 64}"
    )
    event: dict[str, Any] = {
        "sequence": len(journal) + 1,
        "event_type": "external_normal_pack_admitted",
        "admission_id": str(entry["admission_id"]),
        "pack_id": str(entry["pack_id"]),
        "artifact_digest": str(entry["artifact_digest"]),
        "content_digest": str(entry["content_digest"]),
        "previous_event_digest": previous_digest,
    }
    event["event_digest"] = canonical_digest(event)
    return event


def _validate_install_journal(journal: list[Any]) -> None:
    previous_digest = f"{_DIGEST_PREFIX}{'0' * 64}"
    seen_admissions: set[str] = set()
    for index, raw_event in enumerate(journal, start=1):
        if not isinstance(raw_event, dict):
            raise ExternalPackCatalogDenied("external Pack journal event is invalid")
        required = {
            "sequence",
            "event_type",
            "admission_id",
            "pack_id",
            "artifact_digest",
            "content_digest",
            "previous_event_digest",
            "event_digest",
        }
        if set(raw_event) != required:
            raise ExternalPackCatalogDenied("external Pack journal fields are invalid")
        event_digest = str(raw_event["event_digest"])
        unsigned = {key: value for key, value in raw_event.items() if key != "event_digest"}
        admission_id = str(raw_event["admission_id"])
        if (
            raw_event["sequence"] != index
            or raw_event["event_type"] != "external_normal_pack_admitted"
            or raw_event["previous_event_digest"] != previous_digest
            or event_digest != canonical_digest(unsigned)
            or admission_id in seen_admissions
        ):
            raise ExternalPackCatalogDenied("external Pack journal chain is invalid")
        for field in ("artifact_digest", "content_digest", "event_digest"):
            value = str(raw_event[field])
            if len(value) != 71 or not value.startswith(_DIGEST_PREFIX):
                raise ExternalPackCatalogDenied("external Pack journal digest is invalid")
        if len(admission_id) != 64 or any(
            character not in "0123456789abcdef" for character in admission_id
        ):
            raise ExternalPackCatalogDenied("external Pack journal admission is invalid")
        seen_admissions.add(admission_id)
        previous_digest = event_digest


def _atomic_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise ExternalPackCatalogDenied("external Normal Pack catalog persistence is symlinked")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_json_nofollow(
    path: Path,
    max_bytes: int,
    *,
    require_private: bool = False,
) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExternalPackCatalogDenied(f"Host JSON file is unreadable: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ExternalPackCatalogDenied("Host JSON file identity is invalid")
        if require_private and stat.S_IMODE(before.st_mode) & 0o022:
            raise ExternalPackCatalogDenied("Host JSON file permissions are unsafe")
        content = os.read(descriptor, max_bytes + 1)
        after = os.fstat(descriptor)
        if len(content) > max_bytes or _stat_identity(before) != _stat_identity(after):
            raise ExternalPackCatalogDenied("Host JSON file changed while reading")
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExternalPackCatalogDenied("Host JSON file is invalid") from exc
    finally:
        os.close(descriptor)


def _read_regular_nofollow(root: Path, relative: str) -> tuple[bytes, int]:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ExternalPackCatalogDenied("signed Pack entry is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise ExternalPackCatalogDenied("signed Pack entry changed while reading")
        return b"".join(chunks), before.st_mode
    finally:
        os.close(descriptor)


def _verify_signed_file_inventory(
    root: Path,
    signed_manifest: Mapping[str, Any],
) -> None:
    expected = {
        str(item["path"]): item
        for item in signed_manifest.get("files") or ()
        if isinstance(item, Mapping)
    }
    if not expected:
        raise ExternalPackCatalogDenied(
            "external Normal Pack signed file inventory is empty"
        )
    actual_paths: set[str] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise ExternalPackCatalogDenied("external Normal Pack artifact contains a symlink")
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise ExternalPackCatalogDenied("external Normal Pack artifact contains a symlink")
        for name in files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative == SIGNED_MANIFEST_RELATIVE:
                continue
            actual_paths.add(relative)
            item = expected.get(relative)
            if item is None or path.is_symlink():
                raise ExternalPackCatalogDenied("external Normal Pack artifact file inventory changed")
            content, _mode = _read_regular_nofollow(root, _safe_relative(relative))
            if (
                len(content) != item.get("size")
                or hashlib.sha256(content).hexdigest() != item.get("sha256")
            ):
                raise ExternalPackCatalogDenied("external Normal Pack artifact file digest changed")
    if actual_paths != set(expected):
        raise ExternalPackCatalogDenied("external Normal Pack artifact file inventory changed")


def _directory_identity(path: Path) -> tuple[int, int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ExternalPackCatalogDenied("signed Pack root must be a real directory")
    return int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mtime_ns)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns)


def _safe_relative(value: str) -> str:
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or "\\" in value
    ):
        raise ExternalPackCatalogDenied("external Normal Pack artifact path is unsafe")
    return value


def _chmod_read_only_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise ExternalPackCatalogDenied("external Normal Pack artifact contains a symlink")
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise ExternalPackCatalogDenied("external Normal Pack artifact contains a symlink")
            mode = stat.S_IMODE(path.lstat().st_mode)
            os.chmod(path, 0o500 if mode & 0o111 else 0o400)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise ExternalPackCatalogDenied("external Normal Pack artifact contains a symlink")
            os.chmod(directory, 0o500)
        os.chmod(current_path, 0o500)


def _make_tree_writable(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            os.chmod(current_path, 0o700)
        except OSError:
            continue
        for name in directories:
            try:
                os.chmod(current_path / name, 0o700)
            except OSError:
                pass
        for name in files:
            try:
                os.chmod(current_path / name, 0o600)
            except OSError:
                pass


def _inject(callback: Callable[[str], None] | None, point: str) -> None:
    if callback is not None:
        callback(point)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _user_data_root() -> Path:
    from .bootstrap.profile_capture import runtime_user_data_root

    return runtime_user_data_root()


def _catalog_path(user_data: Path) -> Path:
    return user_data / "pack_control" / "external_normal_pack_catalog.v4.json"


def _artifact_store(user_data: Path) -> Path:
    return user_data / "pack_control" / "external_normal_pack_cas"


def _catalog_key() -> bytes:
    return generate_or_load_signing_key(
        _user_data_root() / "pack_control" / ".external_normal_pack_catalog_key"
    )


__all__ = [
    "ExternalPackCatalogDenied",
    "ExternalPackCatalogSnapshot",
    "admit_signed_external_pack",
    "control_catalog_revision",
    "external_pack_content_digest",
    "load_admitted_external_executable_catalog",
    "load_admitted_pack_catalog",
    "load_external_pack_catalog",
    "resolve_admitted_pack_root",
    "resolve_admitted_pack_roots",
]
