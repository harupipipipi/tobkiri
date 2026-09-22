"""Typed Host-owned lifecycle for the explicitly provisioned v4 PackVM."""

from __future__ import annotations

import secrets
import threading
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from core_runtime.hmac_key_manager import (
    SigningKeyError,
    _secure_windows_signing_key,
    generate_or_load_signing_key,
)
from ecosystem.defaultspack.backend.sandbox.isolation.lima_runtime import (
    PACKVM_CLEANUP_PREFIX,
    PackVMDoctor,
    PackVMProcessError,
    PackVMProvisioningPlan,
    PackVMProvisioningRequest,
)
from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
    default_packvm_provisioner,
)
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_cache import (
    PackVMImageCancelled,
    PackVMImageProgress,
)
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)


_MAX_ACTIVE_OPERATIONS = 128
_COMPACT_ACTIVE_OPERATIONS_TO = 96
_TERMINAL_OPERATION_STATES = frozenset({"cancelled", "succeeded", "failed", "interrupted"})
_EMPTY_ARCHIVE_DIGEST = "sha256:" + "0" * 64
PACKVM_OPERATIONS_ARCHIVE_MAX_BYTES = 8 * 1024 * 1024
PACKVM_OPERATIONS_ARCHIVE_MAX_RECORDS = 4096
_JOURNAL_LOCKS_GUARD = threading.Lock()
_JOURNAL_LOCKS: dict[str, threading.RLock] = {}
_WINDOWS_VERIFIED_LOCKS_GUARD = threading.Lock()
_WINDOWS_VERIFIED_LOCKS: set[tuple[int, int, int]] = set()


class PackVMLifecycleProvisioner(Protocol):
    """The narrow authenticated lifecycle surface shared by production backends."""

    @property
    def state_path(self) -> Path:
        """Return the durable authenticated state path."""

    def operation_gate(self, operation: str, binding: Mapping[str, str | int], **kwargs: Any) -> Any:
        """Serialize exactly one authenticated lifecycle operation."""

    def recovery_identity(self) -> dict[str, int | str]:
        """Return non-secret recovery identity facts."""

    def prepare(self) -> PackVMProvisioningPlan:
        """Return one explicit provisioning plan."""

    def provision(self, request: PackVMProvisioningRequest, **kwargs: Any) -> PackVMDoctor:
        """Provision from one consumed authorization."""

    def doctor(self) -> PackVMDoctor:
        """Return the current authenticated health projection."""

    def readiness_snapshot(self) -> dict[str, Any]:
        """Return a public readiness projection."""

    def stop(self, confirmation: str) -> None:
        """Stop one authenticated instance."""

    def cleanup(self, confirmation: str) -> None:
        """Remove one authenticated instance."""

    def cleanup_failed_provision(
        self, confirmation: str, expected_proof: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Recoverably remove an exact failed provisioning instance."""

    def recover_provision_operation(self, expected_proof: Mapping[str, Any]) -> PackVMDoctor:
        """Reconcile an in-flight provision after host restart."""


class PackVMLifecycleV4:
    """Enforce prepare, explicit consent, and one-shot provision ceremonies."""

    def __init__(
        self,
        provisioner: PackVMLifecycleProvisioner | None = None,
        *,
        archive_max_bytes: int = PACKVM_OPERATIONS_ARCHIVE_MAX_BYTES,
        archive_max_records: int = PACKVM_OPERATIONS_ARCHIVE_MAX_RECORDS,
    ) -> None:
        # Lima is deliberately not a default production path.  Tests and
        # conformance environments may still inject ``PackVMLimaProvisioner``
        # explicitly, but a normal Launcher lifecycle selects direct VZ and
        # reports unavailable if its signed prerequisites are not installed.
        self._provisioner = provisioner or default_packvm_provisioner()
        self._plans: dict[str, tuple[PackVMProvisioningPlan, str]] = {}
        self._consents: dict[str, tuple[PackVMProvisioningRequest, PackVMProvisioningPlan]] = {}
        self._operations_path = self._provisioner.state_path.parent / "packvm-operations.json"
        self._operations_key_path = self._provisioner.state_path.parent / "packvm-operations.key"
        self._operations_archive_path = (
            self._provisioner.state_path.parent / "packvm-operations-archive.jsonl"
        )
        self._operations_lock_path = self._provisioner.state_path.parent / "packvm-operations.lock"
        if (
            isinstance(archive_max_bytes, bool)
            or not isinstance(archive_max_bytes, int)
            or archive_max_bytes < 1
            or isinstance(archive_max_records, bool)
            or not isinstance(archive_max_records, int)
            or archive_max_records < 1
        ):
            raise ValueError("PackVM operation archive bounds are invalid")
        self._archive_max_bytes = archive_max_bytes
        self._archive_max_records = archive_max_records
        self._lock = _journal_thread_lock(self._operations_lock_path)
        self._operations: dict[str, dict[str, Any]] = {}
        self._archived_operations: dict[str, dict[str, Any]] = {}
        self._archive_checkpoint: dict[str, Any] = {}
        self._archive_checkpoints: dict[int, str] = {}
        self._loaded_state_digest: str | None = None
        self._loaded_archive_size = 0
        self._operations_generation = 0
        self._journal_loaded = False
        if self._operations_lock_path.parent.is_dir():
            with self._journal_transaction(recover=True):
                pass

    def prepare(self, *, session_id: str | None = None) -> Mapping[str, Any]:
        """Return pinned download and runtime facts without provisioning."""

        with self._lock:
            current_session_digest = _session_digest(session_id)
            with self._provisioner.operation_gate(
                "prepare",
                {"session_digest": current_session_digest},
            ):
                plan = self._provisioner.prepare()
            self._plans.clear()
            self._plans[plan.ceremony_nonce] = (plan, current_session_digest)
            return asdict(plan)

    def consent(
        self,
        payload: Mapping[str, object],
        *,
        session_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Capture explicit consent for one exact, previously displayed plan."""

        expected_keys = {
            "plan_digest",
            "ceremony_nonce",
            "confirmation",
            "approve_image_download",
        }
        if set(payload) != expected_keys:
            raise ValueError("PackVM consent payload does not match the typed contract")
        plan_digest = payload.get("plan_digest")
        ceremony_nonce = payload.get("ceremony_nonce")
        confirmation = payload.get("confirmation")
        approve_download = payload.get("approve_image_download")
        if (
            not isinstance(plan_digest, str)
            or not isinstance(ceremony_nonce, str)
            or not isinstance(confirmation, str)
            or not isinstance(approve_download, bool)
        ):
            raise ValueError("PackVM consent payload types are invalid")
        with self._lock:
            pending = self._plans.pop(ceremony_nonce, None)
            current_session_digest = _session_digest(session_id)
            if pending is None:
                raise ValueError("PackVM consent does not match a pending plan")
            plan, pending_session_digest = pending
            if (
                pending_session_digest != current_session_digest
                or not secrets.compare_digest(plan.plan_digest, plan_digest)
                or not secrets.compare_digest(plan.confirmation, confirmation)
            ):
                raise ValueError("PackVM consent does not match a pending plan")
            if plan.image_download_required and not approve_download:
                raise ValueError(
                    "PackVM image download requires explicit consent for the displayed source, size, and digest"
                )
            with self._provisioner.operation_gate(
                "consent",
                {
                    "session_digest": current_session_digest,
                    "plan_digest": plan_digest,
                    "ceremony_nonce_digest": _digest_text(ceremony_nonce),
                },
            ):
                consent_id = "packvm-consent." + secrets.token_hex(24)
                self._consents.clear()
                self._consents[consent_id] = (
                    PackVMProvisioningRequest(
                        plan_digest=plan_digest,
                        ceremony_nonce=ceremony_nonce,
                        confirmation=confirmation,
                        approve_image_download=approve_download,
                        session_digest=current_session_digest,
                    ),
                    plan,
                )
            return {
                "consent_id": consent_id,
                "plan_digest": plan.plan_digest,
                "image_source": plan.image_source,
                "image_digest": plan.image_digest,
                "image_size_bytes": plan.image_size_bytes,
                "image_download_approved": approve_download,
            }

    def provision(
        self,
        payload: Mapping[str, object],
        *,
        session_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Start one idempotent background provisioning operation."""

        if set(payload) != {"consent_id", "operation_id"} or not all(
            isinstance(payload.get(field), str) for field in ("consent_id", "operation_id")
        ):
            raise ValueError("PackVM provision payload does not match the typed contract")
        consent_id = str(payload["consent_id"])
        operation_id = str(payload["operation_id"])
        if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", operation_id) is None:
            raise ValueError("PackVM operation_id must be a canonical UUID")
        with self._journal_transaction():
            current_session_digest = _session_digest(session_id)
            existing = self._operation(operation_id)
            if existing is not None:
                if (
                    existing.get("operation_kind") != "provision"
                    or existing.get("consent_digest") != _digest_text(consent_id)
                    or existing.get("session_digest") != current_session_digest
                ):
                    raise ValueError("PackVM operation_id is already bound to another consent")
                return _public_operation(existing)
            self._ensure_operation_capacity()
            consent = self._consents.pop(consent_id, None)
            if consent is None:
                raise ValueError("PackVM consent is missing or already consumed")
            request, plan = consent
            if request.session_digest != current_session_digest:
                raise ValueError("PackVM consent belongs to another authenticated session")
            record: dict[str, Any] = {
                "operation_id": operation_id,
                "operation_kind": "provision",
                "consent_digest": _digest_text(consent_id),
                "session_digest": current_session_digest,
                "state": "queued",
                "plan_digest": request.plan_digest,
                "recovery_proof": {
                    "backend_id": plan.backend_id,
                    "instance": plan.instance,
                    "session_digest": current_session_digest,
                    "plan_digest": plan.plan_digest,
                    "ceremony_nonce_digest": _digest_text(plan.ceremony_nonce),
                    "config_digest": plan.config_digest,
                    "executed_config_digest": plan.config_digest,
                    "image_digest": plan.image_digest,
                    "guest_runner_digest": plan.guest_runner_digest,
                    "host_build_digest": plan.host_build_digest,
                    **self._provisioner.recovery_identity(),
                },
                "updated_unix": int(time.time()),
            }
            self._operations[operation_id] = record
            self._persist_operations()
            worker = threading.Thread(
                target=self._run_provision,
                args=(operation_id, replace(request, operation_id=operation_id)),
                daemon=True,
                name=f"packvm-provision-{operation_id[:8]}",
            )
            worker.start()
            return _public_operation(record)

    def progress(
        self,
        operation_id: str,
        *,
        session_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Return one persisted operation state across process restarts."""

        with self._journal_transaction():
            record = self._operation(operation_id)
            if record is None:
                raise ValueError("PackVM operation_id is unknown")
            if record.get("session_digest") != _session_digest(session_id):
                raise ValueError("PackVM operation belongs to another authenticated session")
            return _public_operation(record)

    def cancel(
        self,
        payload: Mapping[str, object],
        *,
        session_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Cancel only a queued operation; running provisioning is fenced."""

        if set(payload) != {"operation_id"} or not isinstance(payload.get("operation_id"), str):
            raise ValueError("PackVM cancel payload does not match the typed contract")
        operation_id = str(payload["operation_id"])
        with self._journal_transaction():
            record = self._operation(operation_id)
            if record is None:
                raise ValueError("PackVM operation_id is unknown")
            if record.get("operation_kind") != "provision" or record.get(
                "session_digest"
            ) != _session_digest(session_id):
                raise ValueError("PackVM operation cannot be cancelled by this session")
            if operation_id in self._archived_operations:
                return _public_operation(record)
            if record.get("state") == "queued":
                record["state"] = "cancelled"
                record["updated_unix"] = int(time.time())
                self._persist_operations()
            elif record.get("state") == "running" and record.get("stage") == "image_prefetch":
                record["cancel_requested"] = True
                record["updated_unix"] = int(time.time())
                self._persist_operations()
            elif record.get("state") not in {
                "cancelled",
                "succeeded",
                "failed",
                "interrupted",
            }:
                raise ValueError("PackVM provisioning cannot be cancelled after it starts")
            return _public_operation(record)

    def doctor(self) -> Mapping[str, Any]:
        """Return authenticated health without mutating the VM."""

        with self._lock:
            return asdict(self._provisioner.doctor())

    def readiness_snapshot(self) -> Mapping[str, Any]:
        """Return the Host-authenticated readiness evidence for runtime capture."""

        with self._lock:
            return self._provisioner.readiness_snapshot()

    def production_backend_registration(self) -> object | None:
        """Expose authenticated direct-VZ facts to the bootstrap composition root.

        Explicit Lima injection is intentionally not adaptable here.  Only a
        provisioner that independently verifies direct VZ state may offer a
        registration, and a missing signed helper transport stays unavailable.
        """

        candidate = getattr(self._provisioner, "prepare_direct_vz", None)
        if not callable(candidate):
            return None
        try:
            return candidate()
        except (OSError, ValueError):
            return None

    def stop(self, payload: Mapping[str, object]) -> Mapping[str, Any]:
        """Stop only the authenticated v4 instance after exact confirmation."""

        if set(payload) != {"confirmation"} or not isinstance(payload.get("confirmation"), str):
            raise ValueError("PackVM stop payload does not match the typed contract")
        with self._lock:
            self._provisioner.stop(str(payload["confirmation"]))
            return asdict(self._provisioner.doctor())

    def cleanup(
        self,
        payload: Mapping[str, object],
        *,
        session_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Queue one durable authenticated or failed-provision cleanup."""

        expected_keys = {"confirmation", "operation_id", "source_operation_id"}
        if set(payload) != expected_keys:
            raise ValueError("PackVM cleanup payload does not match the typed contract")
        confirmation = payload.get("confirmation")
        operation_id = payload.get("operation_id")
        source_operation_id = payload.get("source_operation_id")
        if (
            not isinstance(confirmation, str)
            or not isinstance(operation_id, str)
            or (source_operation_id is not None and not isinstance(source_operation_id, str))
            or not _canonical_operation_id(operation_id)
            or (
                isinstance(source_operation_id, str)
                and not _canonical_operation_id(source_operation_id)
            )
        ):
            raise ValueError("PackVM cleanup payload types are invalid")
        current_session_digest = _session_digest(session_id)
        instance = str(self._provisioner.doctor().instance)
        expected_confirmation = f"{PACKVM_CLEANUP_PREFIX} {instance}"
        if not secrets.compare_digest(confirmation, expected_confirmation):
            raise ValueError(f"PackVM cleanup requires exact confirmation: {expected_confirmation}")
        with self._journal_transaction():
            existing = self._operation(operation_id)
            if existing is not None:
                if (
                    existing.get("operation_kind") != "cleanup"
                    or existing.get("session_digest") != current_session_digest
                    or existing.get("source_operation_id") != source_operation_id
                ):
                    raise ValueError("PackVM cleanup operation_id is already bound")
                return _public_operation(existing)
            self._ensure_operation_capacity()
            proof: Mapping[str, Any] | None = None
            mode = "attested"
            plan_digest = "sha256:" + "0" * 64
            if source_operation_id is not None:
                source = self._operations.get(source_operation_id)
                if (
                    source is None
                    or source.get("operation_kind") != "provision"
                    or source.get("state") not in {"failed", "interrupted"}
                    or source.get("session_digest") != current_session_digest
                    or not isinstance(source.get("recovery_proof"), dict)
                ):
                    raise ValueError("PackVM failed-provision cleanup source is invalid")
                bound_cleanup = source.get("cleanup_operation_id")
                if bound_cleanup is not None and bound_cleanup != operation_id:
                    raise ValueError("PackVM failed-provision cleanup is already bound")
                source["cleanup_operation_id"] = operation_id
                proof = dict(source["recovery_proof"])
                plan_digest = str(source["plan_digest"])
                mode = "failed_provision"
            elif not self._provisioner.state_path.exists():
                raise ValueError("PackVM cleanup requires failed-provision recovery evidence")
            record: dict[str, Any] = {
                "operation_id": operation_id,
                "operation_kind": "cleanup",
                "state": "queued",
                "session_digest": current_session_digest,
                "source_operation_id": source_operation_id,
                "cleanup_mode": mode,
                "plan_digest": plan_digest,
                "updated_unix": int(time.time()),
            }
            self._operations[operation_id] = record
            self._persist_operations()
            worker = threading.Thread(
                target=self._run_cleanup,
                args=(operation_id, confirmation, proof),
                daemon=True,
                name=f"packvm-cleanup-{operation_id[:8]}",
            )
            worker.start()
            return _public_operation(record)

    def _run_provision(
        self,
        operation_id: str,
        request: PackVMProvisioningRequest,
    ) -> None:
        with self._journal_transaction():
            record = self._operations[operation_id]
            if record.get("state") == "cancelled":
                return
            record["state"] = "running"
            record["stage"] = "image_prefetch"
            record["updated_unix"] = int(time.time())
            self._persist_operations()
        try:
            doctor = asdict(
                self._provisioner.provision(
                    request,
                    progress=lambda update: self._record_prefetch_progress(operation_id, update),
                    cancelled=lambda: self._operation_cancel_requested(operation_id),
                )
            )
        except PackVMImageCancelled:
            with self._journal_transaction():
                record = self._operations[operation_id]
                record.update(
                    {
                        "state": "cancelled",
                        "stage": "image_prefetch",
                        "updated_unix": int(time.time()),
                    }
                )
                record.pop("cancel_requested", None)
                self._persist_operations()
            return
        except Exception as error:
            with self._journal_transaction():
                record = self._operations[operation_id]
                failure = _operation_failure(error)
                record.update({"state": "failed", **failure, "updated_unix": int(time.time())})
                self._persist_operations()
            return
        with self._journal_transaction():
            record = self._operations[operation_id]
            record.update(
                {
                    "state": "succeeded",
                    "doctor": doctor,
                    "updated_unix": int(time.time()),
                }
            )
            self._compact_operations()
            self._persist_operations()

    def _record_prefetch_progress(self, operation_id: str, update: PackVMImageProgress) -> None:
        """Persist bounded typed download progress without exposing cache paths."""

        with self._journal_transaction():
            record = self._operations[operation_id]
            if update.stage == "verified" and record.get("cancel_requested"):
                raise PackVMImageCancelled(
                    "packvm_image_cancelled",
                    "PackVM image download was cancelled before provisioning",
                )
            record.update(
                {
                    "stage": "provisioning" if update.stage == "verified" else "image_prefetch",
                    "progress": asdict(update),
                    "updated_unix": int(time.time()),
                }
            )
            self._persist_operations()

    def _operation_cancel_requested(self, operation_id: str) -> bool:
        with self._journal_transaction():
            return bool(self._operations[operation_id].get("cancel_requested"))

    def _run_cleanup(
        self,
        operation_id: str,
        confirmation: str,
        recovery_proof: Mapping[str, Any] | None,
    ) -> None:
        with self._journal_transaction():
            record = self._operations[operation_id]
            record["state"] = "running"
            record["updated_unix"] = int(time.time())
            self._persist_operations()
        try:
            instance = str(self._provisioner.doctor().instance)
            missing = False
            if recovery_proof is None:
                self._provisioner.cleanup(confirmation)
            else:
                result = self._provisioner.cleanup_failed_provision(confirmation, recovery_proof)
                missing = bool(result["missing"])
            cleanup_result = {
                "ready": False,
                "instance": instance,
                "cleanup_confirmation": f"{PACKVM_CLEANUP_PREFIX} {instance}",
                "missing": missing,
            }
        except Exception as error:
            with self._journal_transaction():
                record = self._operations[operation_id]
                record.update(
                    {
                        "state": "failed",
                        **_operation_failure(error),
                        "updated_unix": int(time.time()),
                    }
                )
                self._persist_operations()
            return
        with self._journal_transaction():
            record = self._operations[operation_id]
            record.update(
                {
                    "state": "succeeded",
                    "result": cleanup_result,
                    "updated_unix": int(time.time()),
                }
            )
            self._compact_operations()
            self._persist_operations()

    @contextmanager
    def _journal_transaction(
        self,
        *,
        recover: bool = False,
        reload: bool = True,
    ) -> Iterator[None]:
        """Serialize and refresh one authenticated journal transaction."""

        with self._lock:
            first_load = not self._journal_loaded
            self._operations_lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = _open_journal_lock(self._operations_lock_path)
            acquired = False
            try:
                _acquire_journal_lock(descriptor)
                acquired = True
                if reload or first_load:
                    self._reload_operations(recover=recover or first_load)
                    if first_load and (self._operations or self._archived_operations):
                        self._compact_operations()
                        self._persist_operations()
                    self._journal_loaded = True
                yield
            finally:
                try:
                    if acquired:
                        _release_journal_lock(descriptor)
                finally:
                    os.close(descriptor)

    def _reload_operations(self, *, recover: bool) -> None:
        """Reload both authenticated journals while holding the process lock."""

        (
            self._archived_operations,
            self._archive_checkpoint,
            self._archive_checkpoints,
        ) = self._load_operations_archive()
        self._operations = self._load_operations(recover=recover)

    def _load_operations(self, *, recover: bool) -> dict[str, dict[str, Any]]:
        path = self._operations_path
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if self._archived_operations:
                raise ValueError("PackVM operation archive has no authenticated checkpoint")
            self._loaded_state_digest = None
            self._operations_generation = 0
            return {}
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 1024 * 1024
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ValueError("PackVM operation state is unsafe")
        encoded = path.read_bytes()
        if len(encoded) != metadata.st_size:
            raise ValueError("PackVM operation state changed while loading")
        self._loaded_state_digest = _digest_bytes(encoded)
        payload = json.loads(encoded)
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2, 3}:
            raise ValueError("PackVM operation state is invalid")
        authentication = payload.pop("authentication", None)
        if not isinstance(authentication, str):
            raise ValueError("PackVM operation state is unauthenticated")
        key = _read_private_key(self._operations_key_path)
        expected = hmac.new(
            key,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(authentication, expected):
            raise ValueError("PackVM operation state authentication failed")
        version = int(payload["version"])
        generation = payload.get("generation", 0)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
            or (version == 3 and "generation" not in payload)
        ):
            raise ValueError("PackVM operation state generation is invalid")
        self._operations_generation = generation
        checkpoint = payload.get("archive_checkpoint")
        if version == 1:
            if self._archived_operations:
                raise ValueError("PackVM operation archive checkpoint is missing")
            checkpoint_count = 0
        else:
            if not self._valid_archive_checkpoint(checkpoint):
                raise ValueError("PackVM operation archive checkpoint is invalid")
            if not isinstance(checkpoint, dict):
                raise ValueError("PackVM operation archive checkpoint is invalid")
            checkpoint_count = int(checkpoint["count"])
        raw_operations = payload.get("operations")
        if not isinstance(raw_operations, dict):
            raise ValueError("PackVM operation state is invalid")
        for sequence, (operation_id, archived) in enumerate(
            self._archived_operations.items(), start=1
        ):
            if sequence <= checkpoint_count:
                continue
            active = raw_operations.get(operation_id)
            if not isinstance(active, dict) or not hmac.compare_digest(
                _record_digest(archived), _record_digest(active)
            ):
                raise ValueError(
                    "PackVM operation archive advanced without recoverable active state"
                )
        operations: dict[str, dict[str, Any]] = {}
        for operation_id, raw in raw_operations.items():
            if not isinstance(operation_id, str) or not isinstance(raw, dict):
                raise ValueError("PackVM operation state is invalid")
            record = dict(raw)
            archived_record = self._archived_operations.get(operation_id)
            if archived_record is not None:
                if not hmac.compare_digest(_record_digest(archived_record), _record_digest(record)):
                    raise ValueError("PackVM archived operation conflicts with active state")
                continue
            if recover and record.get("state") in {"queued", "running"}:
                operation_kind = str(record.get("operation_kind") or "provision")
                if operation_kind == "provision":
                    proof = record.get("recovery_proof")
                    try:
                        if not isinstance(proof, Mapping):
                            raise ValueError("PackVM provision recovery proof is missing")
                        health = self._provisioner.recover_provision_operation(proof)
                    except (OSError, ValueError):
                        record["state"] = "interrupted"
                        record["error"] = (
                            "Host restart could not bind this PackVM provision to the "
                            "live attestation; reconciliation is required"
                        )
                        record["error_type"] = "PackVMReconciliationRequired"
                    else:
                        record["state"] = "succeeded"
                        record["doctor"] = asdict(health)
                else:
                    record["state"] = "interrupted"
                    record["error"] = (
                        f"Host restart interrupted PackVM {operation_kind}; inspect status and retry"
                    )
                    record["error_type"] = "PackVMOperationInterrupted"
                record["updated_unix"] = int(time.time())
            operations[operation_id] = record
        return operations

    def _persist_operations(self) -> None:
        path = self._operations_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not hmac.compare_digest(
            self._loaded_state_digest or "",
            _current_file_digest(path) or "",
        ):
            raise ValueError("PackVM operation state changed during transaction")
        generation = self._operations_generation + 1
        unsigned = {
            "version": 3,
            "generation": generation,
            "archive_checkpoint": self._archive_checkpoint,
            "operations": self._operations,
        }
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        key = generate_or_load_signing_key(self._operations_key_path)
        payload = (
            json.dumps(
                {
                    **unsigned,
                    "authentication": hmac.new(key, canonical, hashlib.sha256).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        descriptor, temporary = tempfile.mkstemp(prefix=".packvm-operations-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            self._loaded_state_digest = _digest_bytes(payload)
            self._operations_generation = generation
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _operation(self, operation_id: str) -> dict[str, Any] | None:
        """Return an active or authenticated archived operation record."""

        return self._operations.get(operation_id) or self._archived_operations.get(operation_id)

    def _ensure_operation_capacity(self) -> None:
        """Compact resolved terminal records before admitting a new operation."""

        self._compact_operations()
        if len(self._operations) >= _MAX_ACTIVE_OPERATIONS:
            raise ValueError("PackVM operation journal has 128 unresolved or referenced records")

    def _compact_operations(self) -> None:
        """Archive safe terminal records while retaining unresolved dependencies."""

        if len(self._operations) < _MAX_ACTIVE_OPERATIONS:
            return
        protected: set[str] = set()
        for operation_id, record in self._operations.items():
            state = str(record.get("state") or "")
            if state not in _TERMINAL_OPERATION_STATES:
                protected.add(operation_id)
                source = record.get("source_operation_id")
                if isinstance(source, str):
                    protected.add(source)
            if record.get("operation_kind") == "provision" and state in {"failed", "interrupted"}:
                cleanup_id = record.get("cleanup_operation_id")
                cleanup = self._operation(cleanup_id) if isinstance(cleanup_id, str) else None
                if cleanup is None or cleanup.get("state") != "succeeded":
                    protected.add(operation_id)
        eligible = [
            (operation_id, record)
            for operation_id, record in self._operations.items()
            if operation_id not in protected and record.get("state") in _TERMINAL_OPERATION_STATES
        ]
        eligible.sort(key=lambda item: (int(item[1].get("updated_unix") or 0), item[0]))
        count = max(0, len(self._operations) - _COMPACT_ACTIVE_OPERATIONS_TO)
        selected = eligible[:count]
        if not selected:
            return
        self._append_operations_archive(selected)
        for operation_id, _record in selected:
            self._operations.pop(operation_id, None)

    def _load_operations_archive(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[int, str]]:
        """Verify and load the append-only HMAC-chained operation archive."""

        path = self._operations_archive_path
        empty = {"count": 0, "last_digest": _EMPTY_ARCHIVE_DIGEST}
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            self._loaded_archive_size = 0
            return {}, empty, {0: _EMPTY_ARCHIVE_DIGEST}
        if metadata.st_size > self._archive_max_bytes:
            raise ValueError("PackVM operation archive byte limit exceeded")
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ValueError("PackVM operation archive is unsafe")
        self._loaded_archive_size = metadata.st_size
        key = _read_private_key(self._operations_key_path)
        archived: dict[str, dict[str, Any]] = {}
        checkpoints = {0: _EMPTY_ARCHIVE_DIGEST}
        previous = _EMPTY_ARCHIVE_DIGEST
        with path.open("rb") as handle:
            for sequence, encoded in enumerate(handle, start=1):
                if sequence > self._archive_max_records:
                    raise ValueError("PackVM operation archive record limit exceeded")
                if len(encoded) > 128 * 1024 or not encoded.endswith(b"\n"):
                    raise ValueError("PackVM operation archive record is invalid")
                try:
                    entry = json.loads(encoded)
                except json.JSONDecodeError as exc:
                    raise ValueError("PackVM operation archive record is invalid") from exc
                if not isinstance(entry, dict):
                    raise ValueError("PackVM operation archive record is invalid")
                authentication = entry.pop("authentication", None)
                entry_digest = entry.pop("entry_digest", None)
                if (
                    entry.get("version") != 1
                    or entry.get("sequence") != sequence
                    or entry.get("previous_digest") != previous
                    or not isinstance(authentication, str)
                    or not isinstance(entry_digest, str)
                ):
                    raise ValueError("PackVM operation archive chain is invalid")
                expected_authentication = hmac.new(
                    key, _canonical_json(entry), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(authentication, expected_authentication):
                    raise ValueError("PackVM operation archive authentication failed")
                expected_digest = _digest_bytes(
                    _canonical_json({**entry, "authentication": authentication})
                )
                if not hmac.compare_digest(entry_digest, expected_digest):
                    raise ValueError("PackVM operation archive digest failed")
                operation_id = entry.get("operation_id")
                record = entry.get("record")
                if (
                    not isinstance(operation_id, str)
                    or not isinstance(record, dict)
                    or record.get("operation_id") != operation_id
                    or record.get("state") not in _TERMINAL_OPERATION_STATES
                    or operation_id in archived
                ):
                    raise ValueError("PackVM operation archive record is invalid")
                archived[operation_id] = dict(record)
                previous = entry_digest
                checkpoints[sequence] = previous
        checkpoint = {"count": len(archived), "last_digest": previous}
        return archived, checkpoint, checkpoints

    def _valid_archive_checkpoint(self, value: object) -> bool:
        """Accept the authenticated archive prefix, including crash-ahead appends."""

        if not isinstance(value, dict) or set(value) != {"count", "last_digest"}:
            return False
        count = value.get("count")
        digest = value.get("last_digest")
        if isinstance(count, bool) or not isinstance(count, int) or not isinstance(digest, str):
            return False
        expected = self._archive_checkpoints.get(count)
        return expected is not None and hmac.compare_digest(expected, digest)

    def _append_operations_archive(
        self,
        records: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Durably append records before removing them from active state."""

        key = generate_or_load_signing_key(self._operations_key_path)
        path = self._operations_archive_path
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise ValueError("PackVM operation archive is unsafe")
            if metadata.st_size != self._loaded_archive_size:
                raise ValueError("PackVM operation archive changed during transaction")
            previous = str(self._archive_checkpoint["last_digest"])
            sequence = int(self._archive_checkpoint["count"])
            payloads: list[tuple[str, dict[str, Any], bytes, str, int]] = []
            for operation_id, record in records:
                sequence += 1
                unsigned = {
                    "version": 1,
                    "sequence": sequence,
                    "previous_digest": previous,
                    "operation_id": operation_id,
                    "record": record,
                }
                authentication = hmac.new(
                    key, _canonical_json(unsigned), hashlib.sha256
                ).hexdigest()
                entry_digest = _digest_bytes(
                    _canonical_json({**unsigned, "authentication": authentication})
                )
                payload = (
                    _canonical_json(
                        {
                            **unsigned,
                            "authentication": authentication,
                            "entry_digest": entry_digest,
                        }
                    )
                    + b"\n"
                )
                payloads.append((operation_id, dict(record), payload, entry_digest, sequence))
                previous = entry_digest
            appended_bytes = sum(len(payload) for _, _, payload, _, _ in payloads)
            if sequence > self._archive_max_records:
                raise ValueError("PackVM operation archive record limit exceeded")
            if metadata.st_size + appended_bytes > self._archive_max_bytes:
                raise ValueError("PackVM operation archive byte limit exceeded")
            for operation_id, record, payload, entry_digest, record_sequence in payloads:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("PackVM operation archive append failed")
                    view = view[written:]
                self._archived_operations[operation_id] = record
                self._archive_checkpoints[record_sequence] = entry_digest
            os.fsync(descriptor)
            self._loaded_archive_size = metadata.st_size + appended_bytes
            self._archive_checkpoint = {
                "count": sequence,
                "last_digest": previous,
            }
        finally:
            os.close(descriptor)


def _journal_thread_lock(path: Path) -> threading.RLock:
    """Return the process-wide reentrant lock for one stable journal path."""

    identity = str(path.resolve(strict=False))
    with _JOURNAL_LOCKS_GUARD:
        lock = _JOURNAL_LOCKS.get(identity)
        if lock is None:
            lock = threading.RLock()
            _JOURNAL_LOCKS[identity] = lock
        return lock


def _open_journal_lock(path: Path) -> int:
    """Open and validate the stable cross-process journal lock file."""

    if os.name == "nt":
        return _open_windows_journal_lock(path)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(path, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ValueError("PackVM operation lock is unsafe")
        if metadata.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_windows_journal_lock(path: Path) -> int:
    """Open one ACL-protected, no-reparse Windows lock by stable File ID."""

    descriptor: int | None = None
    try:
        store = SecureDirectory(path.parent, create=True)
        descriptor = store.open_lock(path.name)
        metadata = os.fstat(descriptor)
        identity = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_ctime_ns),
        )
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("PackVM operation lock is unsafe")
        with _WINDOWS_VERIFIED_LOCKS_GUARD:
            verified = identity in _WINDOWS_VERIFIED_LOCKS
        if not verified:
            _secure_windows_journal_lock_acl(path)
            store.validate_open_file(path.name, descriptor)
        if metadata.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        if not verified:
            secured = os.fstat(descriptor)
            secured_identity = (
                int(secured.st_dev),
                int(secured.st_ino),
                int(secured.st_ctime_ns),
            )
            with _WINDOWS_VERIFIED_LOCKS_GUARD:
                _WINDOWS_VERIFIED_LOCKS.add(secured_identity)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except (OSError, SecurePersistenceError, SigningKeyError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ValueError("PackVM operation lock is unsafe") from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _secure_windows_journal_lock_acl(path: Path) -> None:
    """Restrict a pinned Windows journal lock to the current user SID."""

    _secure_windows_signing_key(path)


def _acquire_journal_lock(descriptor: int) -> None:
    """Acquire an exclusive advisory lock on Windows, macOS, or Linux."""

    while True:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                getattr(msvcrt, "locking")(descriptor, getattr(msvcrt, "LK_LOCK"), 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        except InterruptedError:
            continue


def _release_journal_lock(descriptor: int) -> None:
    """Release a journal lock acquired by :func:`_acquire_journal_lock`."""

    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        getattr(msvcrt, "locking")(descriptor, getattr(msvcrt, "LK_UNLCK"), 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _current_file_digest(path: Path) -> str | None:
    """Return a safe bounded file digest for compare-and-swap publication."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > 1024 * 1024
        or metadata.st_mode & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ValueError("PackVM operation state is unsafe")
    encoded = path.read_bytes()
    if len(encoded) != metadata.st_size:
        raise ValueError("PackVM operation state changed during transaction")
    return _digest_bytes(encoded)


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _record_digest(record: Mapping[str, Any]) -> str:
    return _digest_bytes(_canonical_json(record))


def _session_digest(session_id: str | None) -> str:
    """Digest the server-observed panel session without persisting its secret."""

    value = session_id if isinstance(session_id, str) and session_id else "direct-local-lifecycle"
    return _digest_text(value)


def _canonical_operation_id(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is not None


def _operation_failure(error: Exception) -> dict[str, Any]:
    """Normalize failures without exposing host paths or unbounded stderr."""

    message = str(error)
    message = re.sub(r"(?<![A-Za-z0-9_.-])/(?:[^\s:'\"]+/?)+", "<host-path>", message)
    result: dict[str, Any] = {
        "error": message[:1000] or "PackVM operation failed",
        "error_type": type(error).__name__,
    }
    if isinstance(error, PackVMProcessError) or hasattr(error, "diagnostic"):
        result["diagnostic"] = error.diagnostic()
    return result


def _public_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project the exact pollable schema without server authorization proof."""

    allowed = {
        "operation_id",
        "operation_kind",
        "state",
        "plan_digest",
        "updated_unix",
        "doctor",
        "error",
        "error_type",
        "diagnostic",
        "result",
        "source_operation_id",
        "cleanup_mode",
        "stage",
        "progress",
    }
    return {key: value for key, value in record.items() if key in allowed}


def _read_private_key(path: Path) -> bytes:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > 64
        or metadata.st_mode & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ValueError("PackVM operation key is unsafe")
    return path.read_bytes()


__all__ = ["PackVMLifecycleV4"]
