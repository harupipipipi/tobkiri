"""Explicit admission of an existing active, admitted Host Extension revision.

Frozen public baselines are compatibility evidence only. This port grants no
signature trust, capability approval, Profile selection or execution authority.
It never installs an ambient bundled-baseline read context.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import threading
from typing import Any, Iterator, Mapping
from weakref import WeakKeyDictionary, WeakValueDictionary

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.secure_persistence import SecureDirectory

from . import bundled_host_baseline_v4 as baseline
from .bundled_artifact_retention_v4 import (
    BundledArtifactOrigin,
    _digest,
    _identity,
    _projection,
    _read,
)
from .global_contracts.semver import parse_version
from .host_extension_update_v4 import _verified_pack, verify_host_extension_successor
from .pack_boundary import load_pack_catalog
from .pack_signature import SIGNED_MANIFEST_RELATIVE, verify_signed_manifest

_TOKEN = object()
# Identity issuance only: no cached authorization or baseline decision is reused.
_ISSUED: WeakValueDictionary[int, _ExistingHostExtensionProof] = WeakValueDictionary()
_SEALS: WeakKeyDictionary[_ExistingHostExtensionProof, str | None] = WeakKeyDictionary()


class _ExistingHostExtensionProof:
    __slots__ = (
        "origin",
        "pack_id",
        "predecessor_digest",
        "_token",
        "_thread",
        "_process",
        "_active",
        "_trust_path",
        "_policy_lock",
        "_bundle",
        "_lock",
        "_used",
        "baseline_digest",
        "_snapshot",
        "_selected_root",
        "_selected_entry",
        "revision",
        "__weakref__",
    )

    def __init__(
        self,
        token: object,
        origin: BundledArtifactOrigin,
        bundle_root: Path,
        pack_id: str,
    ) -> None:
        if type(self) is not _ExistingHostExtensionProof or token is not _TOKEN:
            raise ValueError("existing Host Extension proof must be issued")
        baseline._require_owner(origin)
        record = load_pack_catalog().get(pack_id)
        if record is None or record.get("kind") != "host_extension":
            raise ValueError("existing Host Extension slot is unknown")
        self.origin = origin
        self.pack_id = pack_id
        self.predecessor_digest = dict(origin.pack_pins).get(pack_id)
        if self.predecessor_digest is None:
            raise ValueError("Host Extension slot is not active")
        self._token = token
        self._thread = threading.get_ident()
        self._process = os.getpid()
        self._active = True
        self._trust_path: Path | None = None
        self._policy_lock: Any = None
        self._bundle = SecureDirectory(bundle_root, create=False)
        self._lock = _read(self._bundle, "bundle.lock.json", metadata=True)
        if _digest(self._lock) != origin.bundle_digest:
            raise ValueError("existing Host Extension bundle differs")
        self._used: dict[str, tuple[Path, tuple[int, int]]] = {}
        _ISSUED[id(self)] = self
        _SEALS[self] = None  # Internal construction; never accepted by admission.
        self.baseline_digest = _verified_pack(self.resolve(pack_id)).compiled.artifact.digest
        self._snapshot = self._load_snapshot()
        entry = self._snapshot.entries.get(pack_id)
        if (
            entry is None
            or entry["artifact_digest"] != self.predecessor_digest
            or entry["catalog_record"].get("kind") != "host_extension"
            or entry["catalog_record"].get("authority") != "host-signed-external-host-extension-v4"
        ):
            raise ValueError("Host Extension predecessor is not already admitted")
        self._selected_root = self._snapshot.roots[pack_id]
        self._selected_entry = dict(entry)
        self.revision = canonical_digest(self.binding())
        _SEALS[self] = self._capture_digest()
        self.check_current()

    def validate(self) -> None:
        if (
            type(self) is not _ExistingHostExtensionProof
            or getattr(self, "_token", None) is not _TOKEN
            or _ISSUED.get(id(self)) is not self
        ):
            raise ValueError("existing Host Extension proof is not issued")
        if (
            not self._active
            or self._thread != threading.get_ident()
            or self._process != os.getpid()
        ):
            raise ValueError("existing Host Extension proof is closed or foreign")
        seal = _SEALS.get(self)
        if seal is not None:
            from .external_pack_catalog_v4 import ExternalPackCatalogSnapshot

            if (
                type(self._snapshot) is not ExternalPackCatalogSnapshot
                or self._capture_digest() != seal
            ):
                raise ValueError("existing Host Extension captured proof changed")
        if dict(self.origin.pack_pins).get(self.pack_id) != self.predecessor_digest:
            raise ValueError("existing Host Extension active pin differs")
        # The owner check precedes every Authority access.
        baseline._require_owner(self.origin)
        if _read(self._bundle, "bundle.lock.json", metadata=True) != self._lock:
            raise ValueError("existing Host Extension bundle changed")

    def resolve(self, pack_id: str) -> Path:
        self.validate()
        raw = _projection(
            self._lock,
            lambda path: _read(self._bundle, path, metadata=True),
            pack_id,
            self.origin,
        )
        manifest = baseline._baseline_projection(self.origin, self._lock, raw, pack_id)
        container = self.origin.user_data_root / baseline._container(
            self.origin,
            pack_id,
            str(manifest["pack"]["artifact_digest"]),
        )
        # Admission requires the already published immutable public cache.
        root = baseline._verify_stored(self.origin, pack_id, container)
        identity = _identity(root, directory=True)
        if self._used.setdefault(pack_id, (root, identity)) != (root, identity):
            raise ValueError("existing Host Extension baseline root changed")
        # Every retained baseline was captured before issuance. Never reseal
        # authority fields while validating a published cache.
        self.validate()
        return root

    def _capture_digest(self) -> str:
        return canonical_digest(
            {
                "origin": id(self.origin),
                "pack_id": self.pack_id,
                "predecessor": self.predecessor_digest,
                "baseline": self.baseline_digest,
                "bundle": (
                    id(self._bundle),
                    str(self._bundle.root),
                    _digest(self._lock),
                ),
                "selected_root": str(self._selected_root),
                "selected_entry": self._selected_entry,
                "snapshot": {
                    "revision": self._snapshot.revision,
                    "entries": self._snapshot.entries,
                    "versions": self._snapshot.versions,
                    "journal": self._snapshot.journal,
                    "roots": {key: str(path) for key, path in self._snapshot.roots.items()},
                },
                "used": {
                    key: (str(path), identity) for key, (path, identity) in self._used.items()
                },
                "thread": self._thread,
                "process": self._process,
                "revision": self.revision,
            }
        )

    def _load_snapshot(self) -> Any:
        from .external_pack_catalog_v4 import _load_external_pack_catalog

        return _load_external_pack_catalog(
            {self.pack_id: self.predecessor_digest},
            baseline_read=self,
        )

    def binding(self) -> dict[str, Any]:
        return {
            "schema": "io.tobkiri.existing-host-extension-admission.v1",
            "owner": canonical_digest({"root": str(self.origin.user_data_root)}),
            "profile_binding": list(self.origin.profile_binding),
            "active_pointer": canonical_digest(self.origin._pointer.payload()),
            "owner_binding": canonical_digest(
                {
                    "owner": self.origin._owner_identity,
                    "workspace": self.origin._workspace_identity,
                    "authority": self.origin._authority_identity,
                }
            ),
            "bundle_digest": self.origin.bundle_digest,
            "security_epoch": self.origin.security_epoch,
            "fencing_token": self.origin.fencing_token,
            "pack_id": self.pack_id,
            "baseline_digest": self.baseline_digest,
            "predecessor_digest": self.predecessor_digest,
            "external_revision": self._snapshot.revision,
        }

    def review_binding(self, source: Path) -> dict[str, Any]:
        self.check_current()
        reader = SecureDirectory(source, create=False)
        return {
            **self.binding(),
            "raw_signed_manifest_digest": _digest(
                reader.read_bytes_bounded(SIGNED_MANIFEST_RELATIVE, max_bytes=4 * 1024 * 1024)
            ),
        }

    def check_current(self) -> None:
        self.validate()
        for pack_id in tuple(self._used):
            self.resolve(pack_id)
        current = self._load_snapshot()
        if (
            current.revision != self._snapshot.revision
            or current.entries != self._snapshot.entries
            or current.roots != self._snapshot.roots
        ):
            raise ValueError("existing Host Extension catalog changed")
        self.validate()
        if self._trust_path is not None:
            self.check_selected_trust(self._trust_path)

    def check_selected_trust(self, trust_store_path: Path) -> None:
        from .external_pack_catalog_v4 import _read_json_nofollow
        from .pack_artifact_integrity import (
            read_host_policy_snapshot,
            host_policy_identity,
        )
        from .publisher_key_versions import resolve_publisher_key

        self.validate()
        policy = read_host_policy_snapshot(
            trust_store_path,
            policy_lock=self._policy_lock,
        )
        install = policy.get("install_records", {}).get(self.pack_id, {})
        if (
            not host_policy_identity(policy)[1]
            or install.get("signature_required") is not True
            or install.get("publisher_id") != self._selected_entry["publisher_id"]
        ):
            raise ValueError("existing Host Extension install policy differs")
        signed = _read_json_nofollow(
            self._selected_root / SIGNED_MANIFEST_RELATIVE,
            4 * 1024 * 1024,
        )
        key = resolve_publisher_key(
            policy,
            publisher_id=self._selected_entry["publisher_id"],
            key_id=self._selected_entry["key_id"],
            pack_id=self.pack_id,
            artifact_digest=str(self.predecessor_digest),
            signed_manifest_digest=canonical_digest(signed),
        )
        # CAS inventory was checked above; verify its envelope under current trust.
        verify_signed_manifest(
            signed,
            key,
            expected_pack_id=self.pack_id,
            expected_publisher_id=self._selected_entry["publisher_id"],
            expected_key_id=self._selected_entry["key_id"],
            expected_version=self._selected_entry["version_string"],
        )
        self.validate()

    def verify_candidate(
        self,
        source: Path,
        signed: Mapping[str, Any],
    ) -> Any:
        self.check_current()
        compiled = _verified_pack(source, signed_manifest=signed).compiled.artifact
        if compiled.pack_id != self.pack_id:
            raise ValueError("existing Host Extension candidate slot differs")
        publisher = str(signed.get("publisher_id") or "")
        if publisher != self._selected_entry["publisher_id"]:
            raise ValueError("existing Host Extension publisher differs")
        frozen = verify_host_extension_successor(
            self.resolve(self.pack_id),
            source,
            expected_predecessor_digest=self.baseline_digest,
            expected_successor_digest=compiled.digest,
            publisher_id=publisher,
            signed_manifest=signed,
        )
        selected = verify_host_extension_successor(
            self._selected_root,
            source,
            expected_predecessor_digest=str(self.predecessor_digest),
            expected_successor_digest=compiled.digest,
            publisher_id=publisher,
            predecessor_publisher_id=self._selected_entry["publisher_id"],
            signed_manifest=signed,
        )
        if frozen.compatibility_digest != selected.compatibility_digest:
            raise ValueError("existing Host Extension compatibility differs")
        for entry in self._snapshot.versions.get(self.pack_id, {}).values():
            if entry["publisher_id"] != publisher:
                raise ValueError("retained Host Extension publisher differs")
            if parse_version(compiled.version) <= parse_version(entry["version_string"]):
                raise ValueError("Host Extension must exceed all retained versions")
        self.check_current()
        return selected

    def check_committed(self, entry: Mapping[str, Any]) -> None:
        self.validate()
        for pack_id in tuple(self._used):
            self.resolve(pack_id)
        current = self._load_snapshot()
        observed = current.versions.get(self.pack_id, {}).get(entry["artifact_digest"])
        if observed != entry:
            raise ValueError("committed Host Extension admission differs")
        versions = {key: dict(rows) for key, rows in current.versions.items()}
        versions[self.pack_id].pop(entry["artifact_digest"])
        if (
            versions != self._snapshot.versions
            or current.journal[:-1] != self._snapshot.journal
            or len(current.journal) != len(self._snapshot.journal) + 1
        ):
            raise ValueError("Host Extension catalog changed after admission")
        self.validate()
        if self._trust_path is not None:
            self.check_selected_trust(self._trust_path)

    def admit(self, source: Path, **arguments: Any) -> Mapping[str, Any]:
        _require_issued_existing_slot_proof(self)
        from .external_pack_catalog_v4 import (
            _admission_policy_locks,
            _admit_signed_external_pack_locked,
            _catalog_path,
            _user_data_root,
        )

        if arguments.pop("expected_catalog_revision") != self.revision:
            raise ValueError("existing Host Extension review is stale")
        if arguments.get("expected_predecessor_digest") != self.predecessor_digest:
            raise ValueError("existing Host Extension selected predecessor differs")
        self._trust_path = arguments["trust_store_path"]
        user_data = _user_data_root()
        catalog_path = _catalog_path(user_data)
        with _admission_policy_locks(arguments["trust_store_path"], catalog_path) as locks:
            self._policy_lock = locks[0]
            self.check_current()
            entry = _admit_signed_external_pack_locked(
                source,
                user_data=user_data,
                catalog_path=catalog_path,
                trust_policy_lock=locks[0],
                existing_slot_proof=self,
                fault_injector=arguments.pop("fault_injector", None),
                **arguments,
            )
            return entry


@contextmanager
def _existing_slot_proof(
    origin: BundledArtifactOrigin,
    *,
    bundle_root: Path,
    pack_id: str,
) -> Iterator[_ExistingHostExtensionProof]:
    proof = _ExistingHostExtensionProof(_TOKEN, origin, bundle_root, pack_id)
    try:
        yield proof
    finally:
        proof._active = False
        _ISSUED.pop(id(proof), None)
        _SEALS.pop(proof, None)


def admit_signed_existing_host_extension_version(
    source_root: Path,
    *,
    origin: BundledArtifactOrigin,
    bundle_root: Path,
    trust_store_path: Path,
    expected_predecessor_digest: str,
    expected_catalog_revision: str,
    expected_artifact_digest: str,
    expected_signed_manifest_digest: str,
) -> Mapping[str, Any]:
    """Admit one exact reviewed existing slot; never select or activate it.

    A committed result with ``reconciliation_needed`` remains an admission.
    An unproved atomic publication raises ``ExternalPackAdmissionOutcomeUnknown``
    and preserves its CAS bytes; callers must reconcile before retrying.
    """
    pack_id = _verified_pack(source_root).compiled.artifact.pack_id
    with _existing_slot_proof(origin, bundle_root=bundle_root, pack_id=pack_id) as proof:
        return proof.admit(
            source_root,
            trust_store_path=trust_store_path,
            expected_predecessor_digest=expected_predecessor_digest,
            expected_catalog_revision=expected_catalog_revision,
            expected_artifact_digest=expected_artifact_digest,
            expected_signed_manifest_digest=expected_signed_manifest_digest,
        )


@contextmanager
def _capture_native_existing_slot(
    source: Path,
) -> Iterator[_ExistingHostExtensionProof]:
    from .bootstrap.profile_capture import _bundle_root, runtime_user_data_root
    from .bundled_artifact_retention_v4 import capture_retention_origin

    # Neither the owner nor the frozen bundle comes from Native request fields.
    origin = capture_retention_origin(runtime_user_data_root())
    with _existing_slot_proof(
        origin,
        bundle_root=_bundle_root(),
        pack_id=_verified_pack(source).compiled.artifact.pack_id,
    ) as proof:
        yield proof


def _native_active_admitted_slot(pack_id: str) -> bool:
    """Select the separate port only for an authenticated active admitted slot."""
    from .bootstrap.profile_capture import _bundle_root, runtime_user_data_root
    from .bundled_artifact_retention_v4 import capture_retention_origin
    from .external_pack_catalog_v4 import (
        _catalog_path,
        _read_authenticated_catalog,
        _validate_entry,
        _visible_entry,
    )
    from .pack_version_selection import catalog_key_identity

    origin = capture_retention_origin(runtime_user_data_root())
    baseline._require_owner(origin)
    selected = dict(origin.pack_pins).get(pack_id)
    catalog = _read_authenticated_catalog(
        _catalog_path(origin.user_data_root),
        allow_missing=True,
    )
    for key, raw in catalog["entries"].items():
        if not isinstance(raw, Mapping):
            raise ValueError("external Host Extension catalog entry is malformed")
        entry = _visible_entry(raw)
        if entry is None:
            continue
        if catalog_key_identity(key, entry) != pack_id:
            continue
        if entry.get("artifact_digest") != selected:
            continue
        _validate_entry(pack_id, entry)
        record = entry["catalog_record"]
        if (
            record.get("kind") != "host_extension"
            or record.get("authority") != "host-signed-external-host-extension-v4"
        ):
            raise ValueError("active Host Extension admission identity differs")
        baseline._require_owner(origin)
        return True  # The issued port next verifies exact CAS, signature and trust.
    reader = SecureDirectory(_bundle_root(), create=False)
    lock = _read(reader, "bundle.lock.json", metadata=True)
    if _digest(lock) != origin.bundle_digest:
        raise ValueError("Native Host Extension bundle differs")
    raw = _projection(
        lock,
        lambda path: _read(reader, path, metadata=True),
        pack_id,
        origin,
    )
    manifest = baseline._baseline_projection(origin, lock, raw, pack_id)
    if selected is not None and selected != manifest["pack"]["artifact_digest"]:
        raise ValueError("active Host Extension predecessor is not admitted")
    baseline._require_owner(origin)
    return False  # Initial known bundled slots keep canonical admission semantics.


def _require_issued_existing_slot_proof(value: Any) -> _ExistingHostExtensionProof:
    if (
        type(value) is not _ExistingHostExtensionProof
        or _ISSUED.get(id(value)) is not value
        or _SEALS.get(value) is None
    ):
        raise ValueError("existing Host Extension admission requires an issued proof")
    value.validate()
    return value


def _compile_captured_pack(root: Path, *, host_extension: bool = True) -> Any:
    """Use bounded descriptor reads for every compiler access in this port."""
    if host_extension:
        return _verified_pack(root).compiled
    from tobkiri_host.artifact_compiler import compile_pack_root

    reader = SecureDirectory(root, create=False)
    captured: dict[str, bytes] = {}
    total = 0

    def read(path: str) -> bytes:
        nonlocal total
        if path not in captured:
            value = reader.read_bytes_bounded(path, max_bytes=128 * 1024 * 1024)
            total += len(value)
            if len(captured) >= 10_000 or total > 512 * 1024 * 1024:
                raise ValueError("existing admission compiler capture exceeds limits")
            captured[path] = value
        return captured[path]

    compiled = compile_pack_root(root, read_artifact=read)
    for path, value in captured.items():
        if reader.read_bytes_bounded(path, max_bytes=128 * 1024 * 1024) != value:
            raise ValueError("existing admission compiler bytes changed")
    return compiled


def _is_existing_slot_read(value: Any) -> bool:
    return isinstance(value, _ExistingHostExtensionProof)
