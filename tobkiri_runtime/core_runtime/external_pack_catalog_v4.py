"""Host-owned signed external Normal Pack catalog and quarantine CAS."""

from __future__ import annotations

from dataclasses import dataclass
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
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document, validate_file

from .hmac_key_manager import generate_or_load_signing_key
from .pack_boundary import PackBoundaryError, load_pack_catalog, resolve_pack_root
from .pack_signature import SIGNED_MANIFEST_RELATIVE, verify_signed_pack


_CATALOG_VERSION = "io.tobkiri.external-normal-pack-catalog.v4"
_ENTRY_VERSION = "io.tobkiri.external-normal-pack-catalog-entry.v4"
_MAX_CATALOG_BYTES = 16 * 1024 * 1024
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

    source = Path(source_root)
    source_identity = _directory_identity(source)
    signed_manifest, install_record = _verify_signed_source(source, trust_store_path)
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
    user_data = _user_data_root()
    catalog_path = _catalog_path(user_data)
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
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    source_identity = _directory_identity(source)
    trust_path = Path(trust_store_path)
    try:
        trust_metadata = trust_path.lstat()
        trust_parent_metadata = trust_path.parent.lstat()
    except OSError as exc:
        raise ExternalPackCatalogDenied("publisher trust store is unavailable") from exc
    if (
        trust_path.is_symlink()
        or not stat.S_ISREG(trust_metadata.st_mode)
        or stat.S_IMODE(trust_metadata.st_mode) & 0o022
        or stat.S_IMODE(trust_parent_metadata.st_mode) & 0o022
    ):
        raise ExternalPackCatalogDenied("publisher trust store permissions are unsafe")
    manifest_path = source / SIGNED_MANIFEST_RELATIVE
    signed_manifest = _read_json_nofollow(manifest_path, _MAX_CATALOG_BYTES)
    if not isinstance(signed_manifest, dict):
        raise ExternalPackCatalogDenied("signed Pack manifest is invalid")
    trust_store = _read_json_nofollow(trust_path, _MAX_CATALOG_BYTES)
    if not isinstance(trust_store, Mapping):
        raise ExternalPackCatalogDenied("publisher trust store is invalid")
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
    if _directory_identity(source) != source_identity:
        raise ExternalPackCatalogDenied("signed Pack root changed during verification")
    return signed_manifest, install_record


def _project_catalog_record(
    root: Path,
    signed_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = validate_file(root / "pack.v4.json", "pack")
    executable = validate_file(root / "executables.v4.json", "executable_catalog")
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
    for item in signed_manifest["files"]:
        relative = _safe_relative(str(item["path"]))
        content, source_mode = _read_regular_nofollow(source, relative)
        if (
            len(content) != item["size"]
            or hashlib.sha256(content).hexdigest() != item["sha256"]
            or stat.S_IMODE(source_mode) != item["mode"]
        ):
            raise ExternalPackCatalogDenied("signed Pack changed while copying")
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(content)
        os.chmod(target, int(item["mode"]))
    signed_target = destination / SIGNED_MANIFEST_RELATIVE
    signed_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    signed_target.write_text(
        json.dumps(signed_manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(signed_target, 0o400)


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
) -> None:
    del install_record
    copied_manifest, _ = _verify_signed_source(root, trust_store_path)
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


def _read_json_nofollow(path: Path, max_bytes: int) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExternalPackCatalogDenied(f"Host JSON file is unreadable: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ExternalPackCatalogDenied("Host JSON file identity is invalid")
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
    "load_admitted_pack_catalog",
    "load_external_pack_catalog",
    "resolve_admitted_pack_root",
    "resolve_admitted_pack_roots",
]
