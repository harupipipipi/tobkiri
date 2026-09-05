"""Typed Launcher runtime-surface projections for canonical Pack v4 state.

The projection boundary deliberately starts from ``ActiveDefaultProfile``.  It
does not consult the legacy ecosystem Registry, mutable frontend stores, or
compatibility Pack metadata. Launcher routes come only from the verified,
digest-pinned Frontend Contract Map used by the canonical Broker transport.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import queue
import secrets
import threading
import time
from concurrent.futures import (
    CancelledError,
    Future,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Generic, Mapping, TypeVar, cast

from ecosystem.defaultspack.domain.runtime_v4 import (
    ActiveDefaultProfile,
    BundledCatalog,
    ProfileResolutionDenied,
    ResolvedDefaultProfile,
)
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document

RUNTIME_SURFACE_API_VERSION = "io.tobkiri.launcher.runtime-surface.v4"
NO_ACTIVE_PROFILE_REVISION = canonical_digest(
    {
        "schema": "io.tobkiri.profile-predecessor.v1",
        "state": "none",
        "field": "revision",
    }
)
NO_ACTIVE_PLAN_DIGEST = canonical_digest(
    {"schema": "io.tobkiri.profile-predecessor.v1", "state": "none", "field": "plan"}
)
NO_ACTIVE_ACTIVATION_ID = "activation:none"


class RuntimeSurfaceErrorCode(str, Enum):
    """Stable fail-closed error codes consumed by Tobkiri Launcher."""

    PROFILE_NOT_ACTIVE = "PROFILE_NOT_ACTIVE"
    STALE_REVISION = "STALE_REVISION"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    UNAPPROVED = "UNAPPROVED"
    TIMEOUT = "TIMEOUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    API_FAILURE = "API_FAILURE"


_ERROR_STATUS = {
    RuntimeSurfaceErrorCode.PROFILE_NOT_ACTIVE: 409,
    RuntimeSurfaceErrorCode.STALE_REVISION: 409,
    RuntimeSurfaceErrorCode.DIGEST_MISMATCH: 409,
    RuntimeSurfaceErrorCode.UNAPPROVED: 403,
    RuntimeSurfaceErrorCode.TIMEOUT: 504,
    RuntimeSurfaceErrorCode.INVALID_REQUEST: 400,
    RuntimeSurfaceErrorCode.API_FAILURE: 503,
}


class RuntimeSurfaceError(RuntimeError):
    """One typed and safely reportable runtime-surface boundary failure."""

    def __init__(self, code: RuntimeSurfaceErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = _ERROR_STATUS[code]

    def as_dict(self) -> dict[str, object]:
        """Return the stable HTTP-neutral error representation."""

        return {
            "runtime_surface_api_version": RUNTIME_SURFACE_API_VERSION,
            "state": "error",
            "code": self.code.value,
            "message": str(self),
            "retryable": self.code
            in {RuntimeSurfaceErrorCode.TIMEOUT, RuntimeSurfaceErrorCode.API_FAILURE},
            "write_set": [],
        }


@dataclass(frozen=True)
class RuntimeSurfaceSnapshot:
    """Verified activation and its finite bundled catalog."""

    active: ActiveDefaultProfile
    catalog: BundledCatalog


SnapshotLoader = Callable[[], ActiveDefaultProfile]
CatalogLoader = Callable[[], BundledCatalog]
UserSettingsReader = Callable[[], Mapping[str, object]]
PackVMReadinessReader = Callable[[], Mapping[str, object]]
CapabilityBindingReader = Callable[[], Mapping[str, object]]


_READ_WORKER_COUNT = 4
_READ_QUEUE_CAPACITY = 16
_READ_WORKER_NAME_PREFIX = "tobkiri-runtime-read"
_ReadResult = TypeVar("_ReadResult")


class _ReadDeadline:
    """Cooperative cancellation boundary shared by one complete read job."""

    def __init__(
        self,
        *,
        deadline: float,
        clock: Callable[[], float],
        cancelled: threading.Event,
        timeout_message: str,
    ) -> None:
        self.deadline = deadline
        self._clock = clock
        self._cancelled = cancelled
        self._timeout_message = timeout_message

    def checkpoint(self) -> None:
        """Reject cancellation or expiry before publishing further work."""

        if self._cancelled.is_set() or self._clock() >= self.deadline:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.TIMEOUT,
                self._timeout_message,
            )

    def cancel(self) -> None:
        """Prevent this read from publishing a result after its caller left."""

        self._cancelled.set()


@dataclass(eq=False)
class _ReadJob(Generic[_ReadResult]):
    owner: object
    future: Future[_ReadResult]
    deadline: _ReadDeadline
    operation: Callable[[_ReadDeadline], _ReadResult]


class _BoundedReadExecutor:
    """Process-wide fixed workers with bounded admission and owner cancellation."""

    def __init__(self, *, workers: int, queue_capacity: int) -> None:
        self._workers = workers
        self._capacity = workers + queue_capacity
        self._admission = threading.BoundedSemaphore(self._capacity)
        self._queue: queue.Queue[_ReadJob[Any]] = queue.Queue(maxsize=self._capacity)
        self._lock = threading.Lock()
        self._jobs_by_owner: dict[object, set[_ReadJob[Any]]] = {}
        self._threads = tuple(
            threading.Thread(
                target=self._worker,
                name=f"{_READ_WORKER_NAME_PREFIX}-{index}",
                daemon=True,
            )
            for index in range(workers)
        )
        for thread in self._threads:
            thread.start()

    def submit(
        self,
        *,
        owner: object,
        deadline: _ReadDeadline,
        operation: Callable[[_ReadDeadline], _ReadResult],
    ) -> Future[_ReadResult] | None:
        """Admit one job without allowing an unbounded pending queue."""

        if not self._admission.acquire(blocking=False):
            return None
        future: Future[_ReadResult] = Future()
        job = _ReadJob(
            owner=owner,
            future=future,
            deadline=deadline,
            operation=operation,
        )
        with self._lock:
            self._jobs_by_owner.setdefault(owner, set()).add(job)
        try:
            self._queue.put_nowait(job)
        except queue.Full:  # pragma: no cover - semaphore and queue move atomically enough
            self._finish(job)
            return None
        return future

    def cancel_owner(self, owner: object) -> None:
        """Cancel all queued and active jobs owned by a closing service."""

        with self._lock:
            jobs = tuple(self._jobs_by_owner.get(owner, ()))
        for job in jobs:
            job.deadline.cancel()
            job.future.cancel()

    def stats(self) -> Mapping[str, int]:
        """Return bounded executor metrics for lifecycle and regression tests."""

        with self._lock:
            admitted = sum(len(jobs) for jobs in self._jobs_by_owner.values())
        return {
            "workers": self._workers,
            "live_workers": sum(thread.is_alive() for thread in self._threads),
            "capacity": self._capacity,
            "admitted": admitted,
        }

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job.future.cancelled():
                    continue
                job.deadline.checkpoint()
                result = job.operation(job.deadline)
                job.deadline.checkpoint()
                if not job.future.cancelled():
                    job.future.set_result(result)
            except BaseException as error:
                if not job.future.cancelled():
                    job.future.set_exception(error)
            finally:
                self._finish(job)
                self._queue.task_done()

    def _finish(self, job: _ReadJob[Any]) -> None:
        with self._lock:
            jobs = self._jobs_by_owner.get(job.owner)
            if jobs is not None:
                jobs.discard(job)
                if not jobs:
                    self._jobs_by_owner.pop(job.owner, None)
        self._admission.release()


_READ_EXECUTOR = _BoundedReadExecutor(
    workers=_READ_WORKER_COUNT,
    queue_capacity=_READ_QUEUE_CAPACITY,
)


def _read_executor_stats() -> Mapping[str, int]:
    """Expose non-secret bounded-worker counts to focused lifecycle tests."""

    return _READ_EXECUTOR.stats()


@dataclass(frozen=True)
class _ProfileCandidate:
    candidate_id: str
    session_id: str
    expected_profile_revision: str
    expected_plan_digest: str
    expected_activation_id: str
    profile_definition_digest: str
    profile_catalog_digest: str
    bundle_lock_digest: str
    resolved: Any
    candidate_digest: str
    expires_at: float


@dataclass(frozen=True)
class _ProfileApproval:
    approval_id: str
    session_id: str
    candidate: _ProfileCandidate
    approval_digest: str
    authority_record_id: str
    expires_at: float


class RuntimeProfileChangeService:
    """Server-bound resolve/review/approval/activation Profile ceremony."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 120.0,
        clock: Callable[[], float] = time.time,
        surface_service: "RuntimeSurfaceService | None" = None,
        store_path: Path | None = None,
        bundle_root: Path | None = None,
        user_data_root: Path | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._surface_service = surface_service
        self._bundle_root = bundle_root
        self._lock = threading.RLock()
        from core_runtime.bootstrap.profile_capture import runtime_user_data_root

        self._user_data_root = (
            Path(user_data_root).resolve()
            if user_data_root is not None
            else runtime_user_data_root()
        )
        if store_path is None:
            store_path = self._user_data_root / "control" / "reconciliation-v4.sqlite3"
        from core_runtime.control_reconciliation_v4 import ControlReconciliationStore

        self._store = ControlReconciliationStore(store_path)

    def resolve(self, body: Mapping[str, object], *, session_id: str) -> dict[str, object]:
        """Resolve a candidate closure without writing runtime state."""

        legacy_expected = {
            "profile_id",
            "expected_profile_revision",
            "expected_plan_digest",
            "desired_pack_ids",
        }
        catalog_expected = legacy_expected | {
            "profile_definition_digest",
            "profile_catalog_digest",
            "bundle_lock_digest",
        }
        if frozenset(body) not in {
            frozenset(legacy_expected),
            frozenset(catalog_expected),
        }:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "Profile resolve request shape is invalid",
            )
        profile_id = _required_string(body.get("profile_id"))
        authoritative_selection = set(body) == catalog_expected
        revision = _required_string(body.get("expected_profile_revision"))
        plan_digest = _required_string(body.get("expected_plan_digest"))
        requested = body.get("desired_pack_ids")
        if (
            not isinstance(requested, list)
            or not requested
            or any(not isinstance(item, str) or not item.strip() for item in requested)
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "desired_pack_ids must be a non-empty string array",
            )
        pack_ids = [item.strip() for item in requested]
        if len(pack_ids) != len(set(pack_ids)):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "desired_pack_ids contains a duplicate",
            )
        no_active_predecessor = hmac.compare_digest(
            revision, NO_ACTIVE_PROFILE_REVISION
        ) and hmac.compare_digest(plan_digest, NO_ACTIVE_PLAN_DIGEST)
        if no_active_predecessor:
            from core_runtime.active_profile_store_v4 import ActiveProfileStore

            if ActiveProfileStore(self._user_data_root).load(verify_snapshot=True) is not None:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.STALE_REVISION,
                    "Profile activation predecessor is no longer empty",
                )
            current: Mapping[str, Any] | None = None
            current_data: Mapping[str, Any] | None = None
        else:
            if hmac.compare_digest(revision, NO_ACTIVE_PROFILE_REVISION) or hmac.compare_digest(
                plan_digest, NO_ACTIVE_PLAN_DIGEST
            ):
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.INVALID_REQUEST,
                    "Profile predecessor binding is incomplete",
                )
            current = self._surface().read_profile(
                expected_profile_revision=revision,
                expected_plan_digest=plan_digest,
            )
            current_data = cast(Mapping[str, Any], current["data"])
        if authoritative_selection:
            definition_digest = _required_string(body.get("profile_definition_digest"))
            catalog_digest = _required_string(body.get("profile_catalog_digest"))
            bundle_digest = _required_string(body.get("bundle_lock_digest"))
            try:
                self._surface().require_catalog_binding(
                    profile_id=profile_id,
                    expected_definition_digest=definition_digest,
                    expected_catalog_digest=catalog_digest,
                    expected_bundle_lock_digest=bundle_digest,
                )
            except RuntimeSurfaceError:
                raise
        else:
            if current is None or profile_id != str(current["profile_id"]):
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.INVALID_REQUEST,
                    "non-active Profile selection requires exact catalog bindings",
                )
            catalog = self._surface().read_profile_catalog()
            catalog_data = cast(Mapping[str, Any], catalog["data"])
            entry = next(
                item for item in catalog_data["profiles"] if item["profile_id"] == profile_id
            )
            definition_digest = str(entry["definition"]["digest"])
            catalog_digest = str(catalog_data["catalog_digest"])
            bundle_digest = str(catalog_data["bundle_lock_digest"])
        try:
            from core_runtime.pack_control_v4 import resolve_profile_pack_set

            if authoritative_selection:
                if self._bundle_root is None:
                    resolved = resolve_profile_pack_set(
                        pack_ids,
                        profile_id=profile_id,
                        expected_profile_definition_digest=definition_digest,
                        expected_profile_catalog_digest=catalog_digest,
                        expected_bundle_lock_digest=bundle_digest,
                        user_data_root=self._user_data_root,
                    )
                else:
                    resolved = resolve_profile_pack_set(
                        pack_ids,
                        profile_id=profile_id,
                        expected_profile_definition_digest=definition_digest,
                        expected_profile_catalog_digest=catalog_digest,
                        expected_bundle_lock_digest=bundle_digest,
                        bundle_root=self._bundle_root,
                        user_data_root=self._user_data_root,
                    )
            else:
                if self._bundle_root is None:
                    resolved = resolve_profile_pack_set(
                        pack_ids,
                        user_data_root=self._user_data_root,
                    )
                else:
                    resolved = resolve_profile_pack_set(
                        pack_ids,
                        bundle_root=self._bundle_root,
                        user_data_root=self._user_data_root,
                    )
        except Exception as error:
            raise _map_change_error(error) from error
        execution_profile_id = None if current is None else str(current["profile_id"])
        execution_activation_id = (
            NO_ACTIVE_ACTIVATION_ID
            if current_data is None
            else str(current_data["activation_record"]["activation_id"])
        )
        review = {
            "candidate_generation": "profile-change-generation:" + secrets.token_hex(16),
            "profile": dict(resolved.profile),
            "profile_lock": dict(resolved.lock),
            "resolved_plan": dict(resolved.plan),
            "selection": {
                "selected_profile_id": profile_id,
                "execution_profile_id": execution_profile_id,
                "execution_profile_revision": revision,
                "execution_plan_digest": plan_digest,
                "execution_activation_id": (
                    None if current_data is None else execution_activation_id
                ),
            },
            "predecessor": {
                "state": "none" if current is None else "active",
                "profile_revision": revision,
                "plan_digest": plan_digest,
                "activation_id": execution_activation_id,
            },
            "catalog_binding": {
                "profile_definition_digest": definition_digest,
                "profile_catalog_digest": catalog_digest,
                "bundle_lock_digest": bundle_digest,
            },
        }
        candidate_id = (
            "candidate.profile-change."
            + canonical_digest(
                {
                    "candidate_digest": canonical_digest(review),
                    "session_id": session_id,
                    "nonce": str(time.time_ns()),
                }
            ).removeprefix("sha256:")[:48]
        )
        candidate_digest = canonical_digest(review)
        candidate = _ProfileCandidate(
            candidate_id=candidate_id,
            session_id=session_id,
            expected_profile_revision=revision,
            expected_plan_digest=plan_digest,
            expected_activation_id=execution_activation_id,
            profile_definition_digest=definition_digest,
            profile_catalog_digest=catalog_digest,
            bundle_lock_digest=bundle_digest,
            resolved=resolved,
            candidate_digest=candidate_digest,
            expires_at=self._clock() + self._ttl_seconds,
        )
        try:
            persisted = self._store.save_candidate(
                candidate_id=candidate_id,
                candidate_digest=candidate_digest,
                session_id=session_id,
                review=review,
                expires_at=candidate.expires_at,
            )
        except Exception as error:
            raise _map_reconciliation_error(error) from error
        candidate = self._candidate_from_record(persisted)
        return {
            "runtime_surface_api_version": RUNTIME_SURFACE_API_VERSION,
            "state": "resolved",
            "candidate_id": candidate.candidate_id,
            "candidate_digest": candidate.candidate_digest,
            "expires_in": int(self._ttl_seconds),
            "review": review,
            "next_action": "review",
            "write_set": [],
        }

    def review(self, body: Mapping[str, object], *, session_id: str) -> dict[str, object]:
        """Acknowledge the exact resolved candidate for later approval."""

        with self._lock:
            candidate_id, digest = self._candidate_request(body)
            try:
                current = self._store.require_candidate(
                    candidate_id,
                    digest,
                    session_id=session_id,
                    allowed_states=("resolved", "reviewed"),
                )
                current_candidate = self._candidate_from_record(current)
                self._require_predecessor_current(current_candidate)
                self._require_candidate_catalog_current(current_candidate)
                record = self._store.transition_reviewed(
                    candidate_id,
                    digest,
                    session_id=session_id,
                )
            except Exception as error:
                raise _map_reconciliation_error(error) from error
            candidate = self._candidate_from_record(record)
            self._require_unexpired(candidate)
        return {
            "runtime_surface_api_version": RUNTIME_SURFACE_API_VERSION,
            "state": "reviewed",
            "candidate_id": candidate.candidate_id,
            "candidate_digest": candidate.candidate_digest,
            "next_action": "approval",
            "write_set": [],
        }

    def approve(self, body: Mapping[str, object], *, session_id: str) -> dict[str, object]:
        """Create a one-shot server approval from a reviewed candidate."""

        with self._lock:
            candidate_id, digest = self._candidate_request(body)
            try:
                current = self._store.require_candidate(
                    candidate_id,
                    digest,
                    session_id=session_id,
                    allowed_states=("reviewed", "approval_prepared", "approved"),
                )
                current_candidate = self._candidate_from_record(current)
                self._require_predecessor_current(current_candidate)
                self._require_candidate_catalog_current(current_candidate)
                prepared = self._store.prepare_approval(
                    candidate_id,
                    digest,
                    session_id=session_id,
                )
            except Exception as error:
                raise _map_reconciliation_error(error) from error
            candidate = self._candidate_from_record(prepared)
            self._require_unexpired(candidate)
            authority_record = _commit_authority_profile_approval(
                candidate,
                session_id=session_id,
                approval_id=str(prepared["approval_id"]),
                decided_at=float(prepared["approval_decided_at"]),
                user_data_root=self._user_data_root,
            )
            approval_id = str(authority_record.approval_id)
            approval_digest = str(authority_record.digest)
            try:
                approved_record = self._store.mark_approved(
                    candidate.candidate_id,
                    candidate.candidate_digest,
                    session_id=session_id,
                    approval_digest=approval_digest,
                    authority_record=authority_record.to_dict(),
                )
            except Exception as error:
                raise _map_reconciliation_error(error) from error
            approval = _ProfileApproval(
                approval_id=approval_id,
                session_id=session_id,
                candidate=candidate,
                approval_digest=approval_digest,
                authority_record_id=str(authority_record.approval_id),
                expires_at=float(approved_record["expires_at"]),
            )
        return self._approval_projection(approval, authority_record)

    def _approval_projection(
        self, approval: _ProfileApproval, authority_record: Any
    ) -> dict[str, object]:
        return {
            "runtime_surface_api_version": RUNTIME_SURFACE_API_VERSION,
            "state": "approved",
            "approval_id": approval.approval_id,
            "approval_digest": approval.approval_digest,
            "authority_approval": {
                "approval_id": authority_record.approval_id,
                "approval_digest": authority_record.digest,
                "decision": authority_record.decision,
                "security_epoch": authority_record.security_epoch,
            },
            "expires_in": max(0, int(approval.expires_at - self._clock())),
            "next_action": "activation",
            "write_set": [],
        }

    def activate(self, body: Mapping[str, object], *, session_id: str) -> dict[str, object]:
        """Consume one approval and atomically activate its exact candidate."""

        if set(body) != {"approval_id", "approval_digest"}:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "Profile activation request shape is invalid",
            )
        approval_id = _required_string(body.get("approval_id"))
        approval_digest = _required_string(body.get("approval_digest"))
        with self._lock:
            try:
                record = self._store.require_approval(
                    approval_id,
                    approval_digest,
                    session_id=session_id,
                )
            except Exception as error:
                raise _map_reconciliation_error(error, approval=True) from error
            if record["state"] == "activated":
                return self._activation_projection(cast(Mapping[str, Any], record["activation"]))
            candidate = self._candidate_from_record(record)
            approval = _ProfileApproval(
                approval_id=approval_id,
                session_id=session_id,
                candidate=candidate,
                approval_digest=approval_digest,
                authority_record_id=approval_id,
                expires_at=float(record["expires_at"]),
            )
            self._require_unexpired(candidate)
            self._surface().require_catalog_binding(
                profile_id=str(candidate.resolved.profile["profile_id"]),
                expected_definition_digest=candidate.profile_definition_digest,
                expected_catalog_digest=candidate.profile_catalog_digest,
                expected_bundle_lock_digest=candidate.bundle_lock_digest,
            )
            activation_id = (
                f"activation:{candidate.resolved.profile['profile_id']}-profile-change-"
                + canonical_digest(
                    {
                        "approval_id": approval_id,
                        "candidate_digest": candidate.candidate_digest,
                    }
                ).removeprefix("sha256:")[:24]
            )
            self._require_predecessor_current(
                candidate,
                accepted_activation_id=activation_id,
            )
            _verify_authority_profile_approval(
                approval,
                user_data_root=self._user_data_root,
            )
            try:
                from core_runtime.pack_control_v4 import activate_resolved_profile_pack_set

                bundle_binding = (
                    {} if self._bundle_root is None else {"bundle_root": self._bundle_root}
                )
                activation = activate_resolved_profile_pack_set(
                    candidate.resolved,
                    activation_id=activation_id,
                    expected_profile_revision=candidate.expected_profile_revision,
                    expected_plan_digest=candidate.expected_plan_digest,
                    expected_activation_id=candidate.expected_activation_id,
                    **bundle_binding,
                    user_data_root=self._user_data_root,
                )
            except Exception as error:
                raise _map_change_error(error) from error
            try:
                self._store.mark_activated(
                    approval_id,
                    approval_digest,
                    session_id=session_id,
                    activation=activation,
                )
            except Exception as error:
                raise _map_reconciliation_error(error, approval=True) from error
        return self._activation_projection(activation)

    @staticmethod
    def _activation_projection(activation: Mapping[str, Any]) -> dict[str, object]:
        return {
            "runtime_surface_api_version": RUNTIME_SURFACE_API_VERSION,
            "state": "active",
            "profile_id": str(activation["profile_id"]),
            "activation_id": str(activation["activation_id"]),
            "plan_digest": str(activation["plan_digest"]),
            "security_epoch": int(activation["security_epoch"]),
            "fencing_token": int(activation["fencing_token"]),
        }

    def _surface(self) -> "RuntimeSurfaceService":
        if self._surface_service is None:
            self._surface_service = RuntimeSurfaceService()
        return self._surface_service

    @staticmethod
    def _candidate_request(body: Mapping[str, object]) -> tuple[str, str]:
        if set(body) != {"candidate_id", "candidate_digest"}:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "Profile ceremony request shape is invalid",
            )
        return (
            _required_string(body.get("candidate_id")),
            _required_string(body.get("candidate_digest")),
        )

    def _candidate_from_record(self, record: Mapping[str, Any]) -> _ProfileCandidate:
        review = cast(Mapping[str, Any], record["review"])
        profile = cast(Mapping[str, Any], review["profile"])
        lock = cast(Mapping[str, Any], review["profile_lock"])
        plan = cast(Mapping[str, Any], review["resolved_plan"])
        predecessor = cast(Mapping[str, Any], review["predecessor"])
        return _ProfileCandidate(
            candidate_id=str(record["candidate_id"]),
            session_id=str(record["session_digest"]),
            expected_profile_revision=str(record["expected_profile_revision"]),
            expected_plan_digest=str(record["expected_plan_digest"]),
            expected_activation_id=str(predecessor["activation_id"]),
            profile_definition_digest=str(record["profile_definition_digest"]),
            profile_catalog_digest=str(record["profile_catalog_digest"]),
            bundle_lock_digest=str(record["bundle_lock_digest"]),
            resolved=ResolvedDefaultProfile(profile=profile, lock=lock, plan=plan),
            candidate_digest=str(record["candidate_digest"]),
            expires_at=float(record["expires_at"]),
        )

    def _require_unexpired(self, candidate: _ProfileCandidate) -> None:
        if candidate.expires_at <= self._clock():
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.TIMEOUT,
                "Profile ceremony candidate expired",
            )

    def _require_predecessor_current(
        self,
        candidate: _ProfileCandidate,
        *,
        accepted_activation_id: str | None = None,
    ) -> None:
        """Fence every ceremony transition to the reviewed Host predecessor."""

        from core_runtime.active_profile_store_v4 import ActiveProfileStore

        current = ActiveProfileStore(self._user_data_root).load(verify_snapshot=True)
        expects_none = (
            hmac.compare_digest(
                candidate.expected_profile_revision,
                NO_ACTIVE_PROFILE_REVISION,
            )
            and hmac.compare_digest(
                candidate.expected_plan_digest,
                NO_ACTIVE_PLAN_DIGEST,
            )
            and hmac.compare_digest(
                candidate.expected_activation_id,
                NO_ACTIVE_ACTIVATION_ID,
            )
        )
        candidate_is_current = (
            current is not None
            and accepted_activation_id is not None
            and (
                current.profile_id == str(candidate.resolved.profile["profile_id"])
                and hmac.compare_digest(
                    current.profile_revision,
                    str(candidate.resolved.plan["profile_revision"]),
                )
                and hmac.compare_digest(
                    current.plan_digest,
                    str(candidate.resolved.plan["plan_digest"]),
                )
                and hmac.compare_digest(
                    current.lock_digest,
                    str(candidate.resolved.lock["lock_digest"]),
                )
                and hmac.compare_digest(current.activation_id, accepted_activation_id)
            )
        )
        if candidate_is_current:
            return
        if expects_none:
            if current is None:
                return
        elif current is not None and (
            hmac.compare_digest(
                current.profile_revision,
                candidate.expected_profile_revision,
            )
            and hmac.compare_digest(
                current.plan_digest,
                candidate.expected_plan_digest,
            )
            and hmac.compare_digest(
                current.activation_id,
                candidate.expected_activation_id,
            )
        ):
            return
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.STALE_REVISION,
            "Profile activation predecessor changed during ceremony",
        )

    def _require_candidate_catalog_current(self, candidate: _ProfileCandidate) -> None:
        """Fence review and approval to the exact resolved catalog bytes."""

        self._surface().require_catalog_binding(
            profile_id=str(candidate.resolved.profile["profile_id"]),
            expected_definition_digest=candidate.profile_definition_digest,
            expected_catalog_digest=candidate.profile_catalog_digest,
            expected_bundle_lock_digest=candidate.bundle_lock_digest,
        )


class RuntimeSurfaceService:
    """Project verified Profile v4 state into typed Launcher read models."""

    def __init__(
        self,
        *,
        snapshot_loader: SnapshotLoader | None = None,
        catalog_loader: CatalogLoader | None = None,
        user_settings_reader: UserSettingsReader | None = None,
        packvm_readiness_reader: PackVMReadinessReader | None = None,
        capability_binding_reader: CapabilityBindingReader | None = None,
        frontend_contract_bindings: tuple[object, ...] | None = None,
        read_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        self._snapshot_loader = snapshot_loader or self._load_active
        self._catalog_loader = catalog_loader or self._load_catalog
        self._user_settings_reader = user_settings_reader
        self._packvm_readiness_reader = packvm_readiness_reader
        self._capability_binding_reader = capability_binding_reader
        self._frontend_contract_bindings = frontend_contract_bindings
        self._read_timeout_seconds = read_timeout_seconds
        self._clock = clock
        self._read_owner = object()
        self._read_lifecycle_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        """Cancel owned reads without creating replacement worker threads."""

        with self._read_lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            owner = self._read_owner
        _READ_EXECUTOR.cancel_owner(owner)

    def cancel_pending_reads(self) -> None:
        """Fence current reads while leaving this service reusable after restart."""

        with self._read_lifecycle_lock:
            if self._closed:
                return
            owner = self._read_owner
            self._read_owner = object()
        _READ_EXECUTOR.cancel_owner(owner)

    def _run_read(
        self,
        timeout_message: str,
        operation: Callable[[_ReadDeadline], _ReadResult],
    ) -> _ReadResult:
        """Run one complete projection under a single monotonic deadline."""

        started = self._clock()
        deadline = _ReadDeadline(
            deadline=started + self._read_timeout_seconds,
            clock=self._clock,
            cancelled=threading.Event(),
            timeout_message=timeout_message,
        )
        with self._read_lifecycle_lock:
            if self._closed:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.API_FAILURE,
                    "canonical runtime read service is closed",
                )
            operation_context = contextvars.copy_context()
            future = _READ_EXECUTOR.submit(
                owner=self._read_owner,
                deadline=deadline,
                operation=lambda read_deadline: operation_context.run(
                    operation,
                    read_deadline,
                ),
            )
        if future is None:
            deadline.cancel()
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.TIMEOUT,
                timeout_message,
            )
        try:
            remaining = deadline.deadline - self._clock()
            if remaining <= 0:
                raise FutureTimeoutError
            return future.result(timeout=remaining)
        except (CancelledError, FutureTimeoutError) as error:
            deadline.cancel()
            future.cancel()
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.TIMEOUT,
                timeout_message,
            ) from error

    def bind_capability_reader(self, reader: CapabilityBindingReader) -> None:
        """Install the one Host-captured PackAPI capability snapshot reader."""

        if self._capability_binding_reader is not None:
            raise RuntimeError("capability binding reader is already installed")
        self._capability_binding_reader = reader

    @staticmethod
    def _load_active() -> ActiveDefaultProfile:
        from core_runtime.bootstrap.profile_capture import capture_active_profile

        return capture_active_profile()

    @staticmethod
    def _load_catalog() -> BundledCatalog:
        from core_runtime.bootstrap.profile_capture import host_profile_catalog

        return host_profile_catalog()

    def read_profile(
        self,
        *,
        expected_profile_revision: str | None = None,
        expected_plan_digest: str | None = None,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> dict[str, object]:
        """Return the complete active Profile runtime projection."""

        selected_id = _coalesce_selected_profile_id(
            profile_id,
            selected_profile_id,
        )
        return self._run_read(
            "canonical runtime snapshot timed out",
            lambda deadline: self._read_profile(
                deadline,
                expected_profile_revision=expected_profile_revision,
                expected_plan_digest=expected_plan_digest,
                selected_profile_id=selected_id,
            ),
        )

    def _read_profile(
        self,
        deadline: _ReadDeadline,
        *,
        expected_profile_revision: str | None,
        expected_plan_digest: str | None,
        selected_profile_id: str | None,
    ) -> dict[str, object]:
        snapshot = self._snapshot(deadline)
        self._check_expected_bindings(
            snapshot.active,
            expected_profile_revision=expected_profile_revision,
            expected_plan_digest=expected_plan_digest,
        )
        active_profile_id = str(snapshot.active.resolved.profile["profile_id"])
        if selected_profile_id is not None and selected_profile_id != active_profile_id:
            data = self._browsing_profile_projection(
                snapshot,
                selected_profile_id,
            )
            deadline.checkpoint()
            return self._read_envelope(
                snapshot,
                surface="profile",
                data=data,
                selected_profile_id=selected_profile_id,
            )
        data = self._profile_projection(snapshot, deadline=deadline)
        deadline.checkpoint()
        return self._read_envelope(snapshot, surface="profile", data=data)

    def read_profile_catalog(
        self,
        *,
        session_id: str | None = None,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> dict[str, object]:
        """Return all verified Profile definitions without changing selection."""

        selected_id = _coalesce_selected_profile_id(
            profile_id,
            selected_profile_id,
        )
        return self._run_read(
            "canonical Profile catalog timed out",
            lambda deadline: self._read_profile_catalog(
                deadline,
                session_id=session_id,
                selected_profile_id=selected_id,
            ),
        )

    def _read_profile_catalog(
        self,
        deadline: _ReadDeadline,
        *,
        session_id: str | None,
        selected_profile_id: str | None,
    ) -> dict[str, object]:
        try:
            deadline.checkpoint()
            active = self._snapshot_loader()
            deadline.checkpoint()
            catalog = self._catalog_loader()
            deadline.checkpoint()
            candidate_records: tuple[Mapping[str, Any], ...] = ()
            if session_id is not None:
                from core_runtime.bootstrap.profile_capture import runtime_user_data_root
                from core_runtime.control_reconciliation_v4 import ControlReconciliationStore

                candidate_records = ControlReconciliationStore(
                    runtime_user_data_root() / "control" / "reconciliation-v4.sqlite3"
                ).profile_candidates(session_id=session_id)
                deadline.checkpoint()
            candidate_map = {
                str(record["review"]["profile"]["profile_id"]): record
                for record in candidate_records
            }
            effective_sets: list[object] = [active.resolved.lock["effective_set"]]
            effective_sets.extend(
                record["review"]["profile_lock"]["effective_set"] for record in candidate_records
            )
            catalog = _catalog_for_effective_sets(catalog, effective_sets)
            from core_runtime.profile_catalog_v4 import project_profile_catalog

            projection = project_profile_catalog(
                catalog,
                active,
                candidates=candidate_map,
                selected_profile_id=selected_profile_id,
            )
        except ProfileResolutionDenied as error:
            raise _map_profile_error(error) from error
        except RuntimeSurfaceError:
            raise
        except ValueError as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "requested browsing Profile is unavailable",
            ) from error
        except (OSError, RuntimeError) as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.API_FAILURE,
                "canonical Profile catalog is unavailable",
            ) from error
        deadline.checkpoint()
        snapshot = RuntimeSurfaceSnapshot(active=active, catalog=catalog)
        return self._read_envelope(
            snapshot,
            surface="profiles",
            data=projection,
            selected_profile_id=selected_profile_id,
        )

    def require_catalog_binding(
        self,
        *,
        profile_id: str,
        expected_definition_digest: str,
        expected_catalog_digest: str,
        expected_bundle_lock_digest: str,
    ) -> None:
        """Verify one selection against the currently admitted catalog bytes."""

        def verify(deadline: _ReadDeadline) -> None:
            try:
                from core_runtime.profile_catalog_v4 import require_profile_catalog_binding

                deadline.checkpoint()
                catalog = self._catalog_loader()
                deadline.checkpoint()
                require_profile_catalog_binding(
                    catalog,
                    profile_id=profile_id,
                    expected_definition_digest=expected_definition_digest,
                    expected_catalog_digest=expected_catalog_digest,
                    expected_bundle_lock_digest=expected_bundle_lock_digest,
                )
                deadline.checkpoint()
            except ValueError as error:
                message = str(error)
                code = (
                    RuntimeSurfaceErrorCode.INVALID_REQUEST
                    if "absent" in message
                    else RuntimeSurfaceErrorCode.DIGEST_MISMATCH
                )
                raise RuntimeSurfaceError(code, message) from error

        self._run_read("canonical Profile catalog timed out", verify)

    def read_advanced(
        self,
        view: str,
        *,
        expected_profile_revision: str | None = None,
        expected_plan_digest: str | None = None,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> dict[str, object]:
        """Return a Pack, Contract, Operation, or principal projection."""

        selected_id = _coalesce_selected_profile_id(
            profile_id,
            selected_profile_id,
        )
        normalized = str(view or "").strip().lower()
        allowed = {"packs", "contracts", "operations", "principals"}
        if normalized not in allowed:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "advanced view must be packs, contracts, operations, or principals",
            )
        return self._run_read(
            f"canonical {normalized} projection timed out",
            lambda deadline: self._read_advanced(
                normalized,
                deadline=deadline,
                expected_profile_revision=expected_profile_revision,
                expected_plan_digest=expected_plan_digest,
                selected_profile_id=selected_id,
            ),
        )

    def _read_advanced(
        self,
        normalized: str,
        *,
        deadline: _ReadDeadline,
        expected_profile_revision: str | None,
        expected_plan_digest: str | None,
        selected_profile_id: str | None,
    ) -> dict[str, object]:
        snapshot = self._snapshot(deadline)
        self._check_expected_bindings(
            snapshot.active,
            expected_profile_revision=expected_profile_revision,
            expected_plan_digest=expected_plan_digest,
        )
        active_profile_id = str(snapshot.active.resolved.profile["profile_id"])
        if selected_profile_id is not None and selected_profile_id != active_profile_id:
            browsing = self._browsing_advanced_projection(
                snapshot,
                selected_profile_id,
            )
            data = {normalized: browsing[normalized]}
            if normalized == "contracts":
                data["routes"] = browsing["routes"]
            deadline.checkpoint()
            return self._read_envelope(
                snapshot,
                surface=normalized,
                data=data,
                selected_profile_id=selected_profile_id,
            )
        advanced = self._advanced_projection(
            snapshot,
            packvm_readiness=self._packvm_readiness(deadline),
            capability_binding=self._capability_binding(deadline),
            frontend_bindings=self._frontend_bindings(snapshot, deadline),
        )
        deadline.checkpoint()
        data = {normalized: advanced[normalized]}
        if normalized == "contracts":
            data["routes"] = advanced["routes"]
        return self._read_envelope(snapshot, surface=normalized, data=data)

    def read_settings(
        self,
        *,
        profile_id: str | None = None,
        selected_profile_id: str | None = None,
    ) -> dict[str, object]:
        """Return user preferences and immutable runtime settings separately."""

        selected_id = _coalesce_selected_profile_id(
            profile_id,
            selected_profile_id,
        )
        return self._run_read(
            "canonical runtime settings timed out",
            lambda deadline: self._read_settings(
                deadline,
                selected_profile_id=selected_id,
            ),
        )

    def _read_settings(
        self,
        deadline: _ReadDeadline,
        *,
        selected_profile_id: str | None,
    ) -> dict[str, object]:
        snapshot = self._snapshot(deadline)
        user_settings: dict[str, object] | None = None
        if self._user_settings_reader is not None:
            try:
                deadline.checkpoint()
                user_settings = dict(self._user_settings_reader())
                deadline.checkpoint()
            except Exception as error:
                if isinstance(error, RuntimeSurfaceError):
                    raise
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.API_FAILURE,
                    "Launcher-local settings adapter is unavailable",
                ) from error
        active = snapshot.active
        active_profile_id = str(active.resolved.profile["profile_id"])
        if selected_profile_id is not None and selected_profile_id != active_profile_id:
            entry = self._browsing_profile_entry(snapshot, selected_profile_id)
            definition = snapshot.catalog.profiles[selected_profile_id]
            data = {
                "user_settings": {
                    "scope": "user",
                    "source": "launcher_local",
                    "state": (
                        "unavailable_from_runtime"
                        if user_settings is None
                        else "available_from_explicit_adapter"
                    ),
                    "mutable_via_profile_activation": False,
                    **({"values": user_settings} if user_settings is not None else {}),
                },
                "runtime_profile_settings": {
                    "scope": "runtime_profile",
                    "mutable_via_profile_activation": True,
                    "state": "browsing_only",
                    "profile_id": selected_profile_id,
                    "profile_revision": canonical_digest(definition),
                    "catalog_revision": definition.get("catalog_revision"),
                    "plan_digest": None,
                    "lock_digest": None,
                    "execution_profile_id": active_profile_id,
                    "execution_profile_revision": str(active.resolved.plan["profile_revision"]),
                    "execution_activation_id": str(active.activation["activation_id"]),
                    "execution_plan_digest": str(active.resolved.plan["plan_digest"]),
                },
                "profile_catalog_entry": entry,
            }
            return self._read_envelope(
                snapshot,
                surface="settings",
                data=data,
                selected_profile_id=selected_profile_id,
            )
        data = {
            "user_settings": {
                "scope": "user",
                "source": "launcher_local",
                "state": (
                    "unavailable_from_runtime"
                    if user_settings is None
                    else "available_from_explicit_adapter"
                ),
                "mutable_via_profile_activation": False,
                **({"values": user_settings} if user_settings is not None else {}),
            },
            "runtime_profile_settings": {
                "scope": "runtime_profile",
                "mutable_via_profile_activation": True,
                "profile_id": str(active.resolved.profile["profile_id"]),
                "profile_revision": str(active.resolved.plan["profile_revision"]),
                "catalog_revision": str(active.resolved.lock["catalog_revision"]),
                "plan_digest": str(active.resolved.plan["plan_digest"]),
                "lock_digest": str(active.resolved.lock["lock_digest"]),
                "security_epoch": int(active.resolved.plan["security_epoch"]),
            },
        }
        return self._read_envelope(snapshot, surface="settings", data=data)

    @staticmethod
    def _read_envelope(
        snapshot: RuntimeSurfaceSnapshot,
        *,
        surface: str,
        data: Mapping[str, object],
        selected_profile_id: str | None = None,
    ) -> dict[str, object]:
        """Bind one surface payload to the exact active canonical records."""

        active = snapshot.active
        profile = active.resolved.profile
        lock = active.resolved.lock
        plan = active.resolved.plan
        data_payload = dict(data)
        if selected_profile_id is not None:
            data_payload.setdefault(
                "selection",
                {
                    "state": (
                        "active_execution"
                        if selected_profile_id == str(profile["profile_id"])
                        else "browsing"
                    ),
                    "selected_profile_id": selected_profile_id,
                    "execution_profile_id": str(profile["profile_id"]),
                },
            )
        envelope: dict[str, object] = {
            "runtime_surface_api_version": RUNTIME_SURFACE_API_VERSION,
            "surface": surface,
            "state": "ready",
            "profile_id": str(profile["profile_id"]),
            "profile_revision": str(plan["profile_revision"]),
            "plan_digest": str(plan["plan_digest"]),
            "catalog_revision": str(lock["catalog_revision"]),
            "records": {
                "profile_lock": {
                    "digest": str(lock["lock_digest"]),
                    "source_ref": (
                        f"profile-lock-v4://{profile['profile_id']}/{lock['lock_digest']}"
                    ),
                },
                "resolved_plan": {
                    "digest": str(plan["plan_digest"]),
                    "source_ref": (
                        f"resolved-plan-v1://{profile['profile_id']}/{plan['plan_digest']}"
                    ),
                },
                "activation_record": {
                    "digest": canonical_digest(active.activation),
                    "source_ref": (
                        f"activation-record-v1://{profile['profile_id']}/"
                        f"{active.activation['activation_id']}"
                    ),
                },
                "authority_snapshot": {
                    "digest": str(profile["profile_authority_snapshot_digest"]),
                    "source_ref": (
                        f"authority-snapshot-v4://{profile['profile_id']}/"
                        f"{profile['profile_authority_snapshot_digest']}"
                    ),
                },
            },
            "data": data_payload,
        }
        return envelope

    def _snapshot(self, deadline: _ReadDeadline) -> RuntimeSurfaceSnapshot:
        try:
            deadline.checkpoint()
            active = self._snapshot_loader()
            deadline.checkpoint()
            catalog = self._catalog_loader()
            deadline.checkpoint()
            catalog = _catalog_for_active_closure(catalog, active)
        except ProfileResolutionDenied as error:
            raise _map_profile_error(error) from error
        except RuntimeSurfaceError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.API_FAILURE,
                "canonical runtime snapshot is unavailable",
            ) from error
        deadline.checkpoint()
        return RuntimeSurfaceSnapshot(active=active, catalog=catalog)

    @staticmethod
    def _check_expected_bindings(
        active: ActiveDefaultProfile,
        *,
        expected_profile_revision: str | None,
        expected_plan_digest: str | None,
    ) -> None:
        current_revision = str(active.resolved.plan["profile_revision"])
        current_plan_digest = str(active.resolved.plan["plan_digest"])
        if expected_profile_revision is not None and not hmac.compare_digest(
            expected_profile_revision, current_revision
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.STALE_REVISION,
                "requested Profile revision is stale",
            )
        if expected_plan_digest is not None and not hmac.compare_digest(
            expected_plan_digest, current_plan_digest
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "requested ResolvedPlan digest does not match",
            )

    def _profile_projection(
        self,
        snapshot: RuntimeSurfaceSnapshot,
        *,
        deadline: _ReadDeadline,
    ) -> dict[str, object]:
        active = snapshot.active
        profile = active.resolved.profile
        lock = active.resolved.lock
        plan = active.resolved.plan
        advanced = self._advanced_projection(
            snapshot,
            packvm_readiness=self._packvm_readiness(deadline),
            capability_binding=self._capability_binding(deadline),
            frontend_bindings=self._frontend_bindings(snapshot, deadline),
        )
        deadline.checkpoint()
        application = next(
            (dict(item) for item in profile["packs"] if item.get("role") == "application"),
            None,
        )
        return {
            "profile": {
                "profile_id": str(profile["profile_id"]),
                "display_name": str(profile.get("display_name") or profile["profile_id"]),
                "profile_revision": str(plan["profile_revision"]),
                "catalog_revision": str(lock["catalog_revision"]),
            },
            "profile_document": dict(profile),
            "base": dict(plan["base"]),
            "shell": dict(plan["shell"]),
            "application": application,
            "pack_closure": advanced["packs"],
            "resolved_wiring": {
                "requested_edges": [dict(item) for item in profile["requested_edges"]],
                "bindings": _normalized_plan_bindings(snapshot),
            },
            "profile_lock": dict(lock),
            "resolved_plan": dict(plan),
            "activation_record": dict(active.activation),
            "authority_snapshot": {
                "profile_authority_snapshot_digest": str(
                    profile["profile_authority_snapshot_digest"]
                ),
                "authority_references": list(profile["authority_references"]),
                "security_epoch": int(active.activation["security_epoch"]),
                "fencing_token": int(active.activation["fencing_token"]),
            },
        }

    def _browsing_profile_entry(
        self,
        snapshot: RuntimeSurfaceSnapshot,
        selected_profile_id: str,
    ) -> Mapping[str, Any]:
        """Return one non-active catalog row without creating an execution plan."""

        from core_runtime.profile_catalog_v4 import project_profile_catalog

        try:
            projection = project_profile_catalog(
                snapshot.catalog,
                snapshot.active,
                selected_profile_id=selected_profile_id,
            )
        except ValueError as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "requested browsing Profile is unavailable",
            ) from error
        profiles = projection.get("profiles")
        if not isinstance(profiles, list):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "requested browsing Profile is unavailable",
            )
        profile_entries = [item for item in profiles if isinstance(item, Mapping)]
        entry = next(
            (item for item in profile_entries if item["profile_id"] == selected_profile_id),
            None,
        )
        if not isinstance(entry, Mapping):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "requested browsing Profile is unavailable",
            )
        return entry

    def _browsing_profile_projection(
        self,
        snapshot: RuntimeSurfaceSnapshot,
        selected_profile_id: str,
    ) -> dict[str, object]:
        """Project a selected Profile while retaining the active execution context."""

        entry = self._browsing_profile_entry(snapshot, selected_profile_id)
        definition = snapshot.catalog.profiles[selected_profile_id]
        active = snapshot.active
        active_profile_id = str(active.resolved.profile["profile_id"])
        return {
            "selection": {
                "state": "browsing",
                "selected_profile_id": selected_profile_id,
                "execution_profile_id": active_profile_id,
                "execution_profile_revision": str(active.resolved.plan["profile_revision"]),
                "execution_activation_id": str(active.activation["activation_id"]),
                "execution_plan_digest": str(active.resolved.plan["plan_digest"]),
            },
            "profile": {
                "profile_id": selected_profile_id,
                "display_name": str(definition.get("display_name") or selected_profile_id),
                "profile_revision": canonical_digest(definition),
                "catalog_revision": definition.get("catalog_revision"),
            },
            "profile_document": dict(definition),
            "base": dict(entry["bindings"]["base"]),
            "shell": dict(entry["bindings"]["shell"]),
            "application": dict(entry["bindings"]["application"]),
            "pack_closure": list(entry["pack_closure"]),
            "profile_catalog_entry": dict(entry),
            "profile_lock": None,
            "resolved_plan": None,
            "activation_record": None,
            "authority_snapshot": dict(entry["authority_snapshot"]),
        }

    def _browsing_advanced_projection(
        self,
        snapshot: RuntimeSurfaceSnapshot,
        selected_profile_id: str,
    ) -> dict[str, object]:
        """Project inspectable catalog metadata without active invocation authority."""

        entry = self._browsing_profile_entry(snapshot, selected_profile_id)
        definition = snapshot.catalog.profiles[selected_profile_id]
        closure = cast(list[Mapping[str, Any]], entry["pack_closure"])
        packs: list[dict[str, object]] = []
        contracts: list[dict[str, object]] = []
        operations: list[dict[str, object]] = []
        principals: list[dict[str, object]] = []
        for locked in closure:
            pack_id = str(locked["pack_id"])
            manifest = snapshot.catalog.packs.get(pack_id)
            if manifest is None:
                continue
            pack = manifest["pack"]
            packs.append(
                {
                    "pack_id": pack_id,
                    "role": str(locked.get("role") or "provider"),
                    "kind": str(pack["kind"]),
                    "version": str(pack["version"]),
                    "display_name": str(pack["display_name"]),
                    "artifact_digest": str(pack["artifact_digest"]),
                    "artifact_ref": f"pack-v4://{pack_id}@{pack['artifact_digest']}",
                    "artifacts": [
                        _artifact_projection(pack_id, artifact)
                        for artifact in manifest["artifacts"]
                    ],
                    "pack_dependencies": dict(manifest["requirements"]["pack_dependencies"]),
                    "contract_dependencies": [
                        dict(item) for item in manifest["requirements"]["contract_dependencies"]
                    ],
                    "installed": False,
                    "enabled": False,
                    "approved": False,
                    "required": True,
                    "invokable_operations": [],
                    "reason": "browsing_only",
                }
            )
            for contract in manifest["contracts"]:
                contract_id = str(contract["contract_id"])
                revision = str(contract["revision_digest"])
                functions = [
                    str(function["id"])
                    for function in manifest["functions"]
                    if function["contract_revision_digest"] == revision
                ]
                contracts.append(
                    {
                        "pack_id": pack_id,
                        "contract_id": contract_id,
                        "revision_digest": revision,
                        "operations": list(contract["operations"]),
                        "operation_catalog": [],
                        "schema_state": "browsing",
                        "provider_semantics": None,
                        "provenance": None,
                        "provider_function_ids": sorted(functions),
                    }
                )
                for operation_id in contract["operations"]:
                    operation_name = str(operation_id)
                    function_id = functions[0] if len(functions) == 1 else ""
                    edge: Mapping[str, Any] = next(
                        (
                            item
                            for item in definition.get("requested_edges", [])
                            if isinstance(item, Mapping)
                            and item.get("contract_id") == contract_id
                            and item.get("operation_id") == operation_name
                        ),
                        {},
                    )
                    row: dict[str, object] = {
                        "pack_id": pack_id,
                        "owner_pack_id": pack_id,
                        "contract_id": contract_id,
                        "operation_id": operation_name,
                        "contribution_id": (
                            f"browsing::{selected_profile_id}::{pack_id}::"
                            f"{contract_id}::{operation_name}"
                        ),
                        "activation_id": str(snapshot.active.activation["activation_id"]),
                        "catalog_digest": str(entry["definition"]["digest"]),
                        "domain_kind": "browsing",
                        "artifact_digest": str(pack["artifact_digest"]),
                        "function_id": function_id,
                        "function_principal_id": (
                            f"browsing::{selected_profile_id}::{function_id}"
                        ),
                        "contract_revision_digest": revision,
                        "function_implementation_digest": None,
                        "caller_function_id": str(edge.get("caller_function_id") or ""),
                        "target_provider_id": str(edge.get("target_provider_id") or function_id),
                        "authority_reference": None,
                        "invokable": False,
                        "invocation_reason": "browsing_only",
                        "route": None,
                    }
                    operations.append(row)
                    principals.append(
                        {
                            **row,
                            "principal_id": row["function_principal_id"],
                            "parent_artifact_digest": str(pack["artifact_digest"]),
                            "status": "browsing",
                            "authority": None,
                        }
                    )
        return {
            "packs": sorted(packs, key=lambda item: str(item["pack_id"])),
            "contracts": sorted(
                contracts,
                key=lambda item: (
                    str(item["pack_id"]),
                    str(item["contract_id"]),
                ),
            ),
            "operations": sorted(
                operations,
                key=lambda item: (
                    str(item["contract_id"]),
                    str(item["operation_id"]),
                ),
            ),
            "principals": sorted(
                principals,
                key=lambda item: str(item["function_id"]),
            ),
            "routes": [],
        }

    @staticmethod
    def _advanced_projection(
        snapshot: RuntimeSurfaceSnapshot,
        *,
        packvm_readiness: Mapping[str, object] | None,
        capability_binding: Mapping[str, object] | None,
        frontend_bindings: tuple[object, ...],
    ) -> dict[str, Any]:
        active = snapshot.active
        contract_catalogs = _validated_contract_catalogs(snapshot)
        lifecycle = _captured_lifecycle_projection(snapshot)
        lifecycle_packs = {str(item["pack_id"]): item for item in lifecycle["packs"]}
        selected = {
            str(item["identity"]): dict(item) for item in active.resolved.lock["effective_set"]
        }
        required_pack_ids = {
            str(active.resolved.plan["base"]["pack_id"]),
            str(active.resolved.plan["shell"]["pack_id"]),
            *(
                str(item["pack_id"])
                for item in active.resolved.profile["packs"]
                if item.get("role") != "application"
            ),
        }
        invokable_by_pack: dict[str, list[str]] = {}
        for binding in active.resolved.plan["bindings"]:
            invokable_by_pack.setdefault(str(binding["pack_id"]), []).append(
                f"{binding['contract_id']}::{binding['operation_id']}"
            )
        packs: list[dict[str, object]] = []
        contracts: dict[tuple[str, str], dict[str, object]] = {}
        functions_by_contract: dict[tuple[str, str], list[str]] = {}
        for pack_id, locked in sorted(selected.items()):
            manifest = snapshot.catalog.packs.get(pack_id)
            if manifest is None:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "ProfileLock contains a Pack absent from the verified catalog",
                )
            pack = manifest["pack"]
            if not hmac.compare_digest(
                str(pack["artifact_digest"]), str(locked["artifact_digest"])
            ):
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "ProfileLock Pack artifact does not match the verified catalog",
                )
            packs.append(
                {
                    "pack_id": pack_id,
                    "role": str(locked["role"]),
                    "kind": str(pack["kind"]),
                    "version": str(pack["version"]),
                    "display_name": str(pack["display_name"]),
                    "artifact_digest": str(pack["artifact_digest"]),
                    "artifact_ref": (f"pack-v4://{pack_id}@{pack['artifact_digest']}"),
                    "artifacts": [
                        _artifact_projection(pack_id, artifact)
                        for artifact in manifest["artifacts"]
                    ],
                    "provenance": {
                        "source_identity": str(manifest["integrity"]["source_identity"]),
                        "artifact_set_digest": str(manifest["integrity"]["artifact_set_digest"]),
                        "contract_catalog_digest": str(
                            manifest["integrity"]["contract_catalog_digest"]
                        ),
                    },
                    "pack_dependencies": dict(manifest["requirements"]["pack_dependencies"]),
                    "contract_dependencies": [
                        dict(item) for item in manifest["requirements"]["contract_dependencies"]
                    ],
                    "installed": bool(lifecycle_packs.get(pack_id, {}).get("installed", False)),
                    "enabled": bool(lifecycle_packs.get(pack_id, {}).get("enabled", False)),
                    "approved": bool(lifecycle_packs.get(pack_id, {}).get("approved", False)),
                    "approval_source": "pack_control_catalog",
                    "required": pack_id in required_pack_ids,
                    "invokable_operations": sorted(invokable_by_pack.get(pack_id, [])),
                    "reason": lifecycle_packs.get(pack_id, {}).get("approval_reason"),
                }
            )
            for function in manifest["functions"]:
                for contract in manifest["contracts"]:
                    if function["contract_revision_digest"] == contract["revision_digest"]:
                        functions_by_contract.setdefault(
                            (pack_id, str(contract["contract_id"])), []
                        ).append(str(function["id"]))
            for contract in manifest["contracts"]:
                key = (pack_id, str(contract["contract_id"]))
                catalog = contract_catalogs.get(pack_id)
                contract_document = (
                    _one_contract_document(
                        catalog,
                        contract_id=str(contract["contract_id"]),
                        revision_digest=str(contract["revision_digest"]),
                    )
                    if catalog is not None
                    else None
                )
                operation_catalog = (
                    [
                        _operation_schema_projection(contract_document, operation)
                        for operation in contract_document["operations"]
                    ]
                    if contract_document is not None
                    else []
                )
                contracts[key] = {
                    "pack_id": pack_id,
                    "contract_id": str(contract["contract_id"]),
                    "revision_digest": str(contract["revision_digest"]),
                    "operations": list(contract["operations"]),
                    "operation_catalog": operation_catalog,
                    "schema_state": (
                        "verified" if contract_document is not None else "unavailable"
                    ),
                    "provider_semantics": (
                        dict(contract_document["provider_semantics"])
                        if contract_document is not None
                        else None
                    ),
                    "provenance": (
                        dict(contract_document["provenance"])
                        if contract_document is not None
                        else None
                    ),
                    "provider_function_ids": sorted(functions_by_contract.get(key, [])),
                }
        edge_lookup = {
            (
                str(edge["caller_function_id"]),
                str(edge["target_provider_id"]),
                str(edge["contract_id"]),
                str(edge["operation_id"]),
            ): edge
            for edge in active.resolved.profile["requested_edges"]
        }
        operations: list[dict[str, object]] = []
        principals: list[dict[str, object]] = []
        for binding in active.resolved.plan["bindings"]:
            principal = dict(binding["function_principal"])
            edge = edge_lookup.get(
                (
                    str(binding["caller_function_id"]),
                    str(principal["function_id"]),
                    str(binding["contract_id"]),
                    str(binding["operation_id"]),
                ),
                {},
            )
            row: dict[str, object] = {
                "pack_id": str(binding["pack_id"]),
                "owner_pack_id": str(binding["pack_id"]),
                "contract_id": str(binding["contract_id"]),
                "operation_id": str(binding["operation_id"]),
                "contribution_id": _operation_contribution_id(binding),
                "activation_id": str(active.activation["activation_id"]),
                "catalog_digest": str(active.resolved.lock["catalog_revision"]),
                "domain_kind": str(binding["domain_kind"]),
                "artifact_digest": str(binding["artifact_digest"]),
                "function_id": str(principal["function_id"]),
                "function_principal_id": _principal_id(principal),
                "contract_revision_digest": str(principal["contract_revision_digest"]),
                "function_implementation_digest": str(principal["function_implementation_digest"]),
                "caller_function_id": str(binding["caller_function_id"]),
                "target_provider_id": str(edge.get("target_provider_id") or ""),
                "authority_reference": str(binding["authority_reference"]),
                "route": {
                    "contract_id": str(binding["contract_id"]),
                    "operation_id": str(binding["operation_id"]),
                    "function_id": str(principal["function_id"]),
                    "provider_pack_id": str(binding["pack_id"]),
                },
            }
            contract_row = contracts.get((str(binding["pack_id"]), str(binding["contract_id"])))
            if contract_row is None:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "ResolvedPlan binding has no verified Contract document",
                )
            verified_operation_catalog = cast(
                list[Mapping[str, Any]], contract_row["operation_catalog"]
            )
            operation_rows = [
                item
                for item in verified_operation_catalog
                if item["operation_id"] == str(binding["operation_id"])
            ]
            if contract_row["schema_state"] == "verified" and len(operation_rows) != 1:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "ResolvedPlan binding has no unique verified Operation schema",
                )
            row["schema"] = (
                dict(operation_rows[0]) if len(operation_rows) == 1 else {"state": "unavailable"}
            )
            semantics = contract_row["provider_semantics"]
            row["provider_semantics"] = (
                dict(cast(Mapping[str, Any], semantics)) if semantics is not None else None
            )
            lifecycle_pack = lifecycle_packs.get(str(binding["pack_id"]), {})
            pack_kind = next(
                (item["kind"] for item in packs if item["pack_id"] == str(binding["pack_id"])),
                "",
            )
            capability_target = _capability_invocation_target(
                capability_binding,
                active=active,
                operation=row,
            )
            row["invokable"] = capability_target is not None
            row["invocation_reason"] = (
                None
                if capability_target is not None
                else str(lifecycle_pack.get("approval_reason") or "capability_binding_unavailable")
            )
            if pack_kind == "normal_sandbox" and not _packvm_attested(packvm_readiness):
                row["invokable"] = False
                row["invocation_reason"] = "packvm_attestation_not_current"
            row["invocation_contribution_id"] = (
                str(capability_target["contribution_id"]) if capability_target is not None else None
            )
            row["invocation_owner_pack_id"] = (
                str(capability_target["owner_pack_id"]) if capability_target is not None else None
            )
            row["invocation_catalog_hash"] = (
                str(capability_binding["catalog_hash"])
                if capability_target is not None and capability_binding is not None
                else None
            )
            operations.append(row)
            principals.append(
                {
                    **row,
                    "parent_artifact_digest": str(principal["parent_artifact_digest"]),
                    "principal_id": _principal_id(principal),
                    "status": "active",
                    "authority": {
                        "reference": str(edge.get("authority_reference") or ""),
                        "security_epoch": int(active.activation["security_epoch"]),
                        "activation_id": str(active.activation["activation_id"]),
                    },
                }
            )
        routes = _verified_route_projection(
            frontend_bindings,
            operations=operations,
            frontend_map_digest=_frontend_map_digest(snapshot),
        )
        return {
            "packs": packs,
            "contracts": [contracts[key] for key in sorted(contracts)],
            "operations": sorted(
                operations,
                key=lambda item: (item["contract_id"], item["operation_id"]),
            ),
            "principals": sorted(
                principals,
                key=lambda item: (item["function_id"], item["operation_id"]),
            ),
            "routes": routes,
        }

    def _frontend_bindings(
        self,
        snapshot: RuntimeSurfaceSnapshot,
        deadline: _ReadDeadline,
    ) -> tuple[object, ...]:
        deadline.checkpoint()
        if self._frontend_contract_bindings is not None:
            return self._frontend_contract_bindings
        from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
            load_frontend_contract_bindings,
        )

        runtime_root = Path(__file__).resolve().parents[3]
        application = _active_application_manifest(snapshot)
        map_artifact = _frontend_map_artifact(application)
        try:
            bindings = tuple(
                load_frontend_contract_bindings(
                    runtime_root
                    / "ecosystem"
                    / "defaultspack"
                    / Path(*PurePosixPath(str(map_artifact["path"])).parts),
                    application,
                )
            )
            deadline.checkpoint()
            return bindings
        except RuntimeSurfaceError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "verified Frontend Contract Map is unavailable",
            ) from error

    def _packvm_readiness(
        self,
        deadline: _ReadDeadline,
    ) -> Mapping[str, object] | None:
        deadline.checkpoint()
        if self._packvm_readiness_reader is None:
            return None
        try:
            result = dict(self._packvm_readiness_reader())
            deadline.checkpoint()
            return result
        except RuntimeSurfaceError:
            raise
        except Exception:
            return None

    def _capability_binding(
        self,
        deadline: _ReadDeadline,
    ) -> Mapping[str, object] | None:
        deadline.checkpoint()
        if self._capability_binding_reader is None:
            return None
        try:
            result = dict(self._capability_binding_reader())
            deadline.checkpoint()
            return result
        except RuntimeSurfaceError:
            raise
        except Exception:
            return None


def create_runtime_surface_services(
    **kwargs: object,
) -> tuple[RuntimeSurfaceService, RuntimeProfileChangeService]:
    """Compose Defaultspack's surface behind the generic Host control port."""

    snapshot_loader = _optional_runtime_reader(kwargs, "snapshot_loader")
    catalog_loader = _optional_runtime_reader(kwargs, "catalog_loader")
    user_settings_reader = _optional_runtime_reader(kwargs, "user_settings_reader")
    packvm_readiness_reader = _optional_runtime_reader(
        kwargs,
        "packvm_readiness_reader",
    )
    capability_binding_reader = _optional_runtime_reader(
        kwargs,
        "capability_binding_reader",
    )
    frontend_contract_bindings = _optional_frontend_contract_bindings(kwargs)
    bundle_root = _optional_path_argument(kwargs, "bundle_root")
    user_data_root = _optional_path_argument(kwargs, "user_data_root")
    surface = RuntimeSurfaceService(
        snapshot_loader=snapshot_loader,
        catalog_loader=catalog_loader,
        user_settings_reader=user_settings_reader,
        packvm_readiness_reader=packvm_readiness_reader,
        capability_binding_reader=capability_binding_reader,
        frontend_contract_bindings=frontend_contract_bindings,
    )
    changes = RuntimeProfileChangeService(
        surface_service=surface,
        bundle_root=bundle_root,
        user_data_root=user_data_root,
    )
    return surface, changes


def _optional_runtime_reader(
    kwargs: Mapping[str, object],
    name: str,
) -> Callable[[], Any] | None:
    """Read an optional callback from the Host's neutral composition bag."""

    value = kwargs.get(name)
    if value is None:
        return None
    if not callable(value):
        raise TypeError(f"{name} must be callable")
    return value


def _optional_frontend_contract_bindings(
    kwargs: Mapping[str, object],
) -> tuple[object, ...] | None:
    """Read immutable frontend bindings without accepting mutable sequences."""

    value = kwargs.get("frontend_contract_bindings")
    if value is None:
        return None
    if not isinstance(value, tuple):
        raise TypeError("frontend_contract_bindings must be a tuple")
    return value


def _optional_path_argument(
    kwargs: Mapping[str, object],
    name: str,
) -> Path | None:
    """Read an optional filesystem root from the neutral composition bag."""

    value = kwargs.get(name)
    if value is None:
        return None
    if not isinstance(value, Path):
        raise TypeError(f"{name} must be a pathlib.Path")
    return value


def _map_profile_error(error: ProfileResolutionDenied) -> RuntimeSurfaceError:
    message = str(error).lower()
    if "digest" in message or "tamper" in message or "changed" in message:
        code = RuntimeSurfaceErrorCode.DIGEST_MISMATCH
        safe = "canonical runtime integrity verification failed"
    elif "stale" in message or "epoch" in message or "fence" in message:
        code = RuntimeSurfaceErrorCode.STALE_REVISION
        safe = "canonical runtime activation is stale"
    elif "approv" in message or "authority" in message:
        code = RuntimeSurfaceErrorCode.UNAPPROVED
        safe = "canonical runtime authority approval is unavailable"
    elif "active" in message or "activation" in message:
        code = RuntimeSurfaceErrorCode.PROFILE_NOT_ACTIVE
        safe = "canonical runtime Profile is not active"
    else:
        code = RuntimeSurfaceErrorCode.API_FAILURE
        safe = "canonical runtime snapshot is unavailable"
    return RuntimeSurfaceError(code, safe)


def _frontend_map_digest(snapshot: RuntimeSurfaceSnapshot) -> str:
    return str(_frontend_map_artifact(_active_application_manifest(snapshot))["digest"])


def _active_application_manifest(
    snapshot: RuntimeSurfaceSnapshot,
) -> Mapping[str, Any]:
    """Return the Application Pack selected by the active ResolvedPlan."""

    application = snapshot.active.resolved.plan.get("application")
    application_id = (
        str(application.get("pack_id") or "") if isinstance(application, Mapping) else ""
    )
    if not application_id:
        application_id = next(
            (
                str(item["pack_id"])
                for item in snapshot.active.resolved.profile.get("packs", [])
                if item.get("role") == "application"
            ),
            "",
        )
    manifest = snapshot.catalog.packs.get(application_id)
    if manifest is None:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "verified active Application Pack is unavailable",
        )
    return manifest


def _frontend_map_artifact(application: Mapping[str, Any]) -> Mapping[str, Any]:
    """Find the unique frontend contract map admitted by an Application Pack."""

    matches = [
        artifact
        for artifact in application.get("artifacts", [])
        if isinstance(artifact, Mapping)
        and artifact.get("kind") == "asset"
        and PurePosixPath(str(artifact.get("path") or "")).name == "frontend_contract_map.v4.json"
    ]
    if len(matches) != 1:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "Frontend Contract Map artifact is not unique",
        )
    path = PurePosixPath(str(matches[0].get("path") or ""))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "Frontend Contract Map artifact path is unsafe",
        )
    return matches[0]


def _verified_route_projection(
    bindings: tuple[object, ...],
    *,
    operations: list[dict[str, object]],
    frontend_map_digest: str,
) -> list[dict[str, object]]:
    """Bind every digest-pinned frontend route to one captured principal."""

    routes: list[dict[str, object]] = []
    for binding in bindings:
        method = str(getattr(binding, "method")).upper()
        logical_target = str(getattr(binding, "path"))
        presentation = str(getattr(binding, "presentation"))
        for target in getattr(binding, "targets"):
            contract_id = str(getattr(target, "contract_id"))
            operation_id = str(getattr(target, "operation_id"))
            provider_id = str(getattr(target, "provider_id"))
            function_id = str(getattr(target, "function_id"))
            exact = [
                operation
                for operation in operations
                if operation["contract_id"] == contract_id
                and operation["operation_id"] == operation_id
                and operation["function_id"] == function_id
                and operation["target_provider_id"] == provider_id
            ]
            if len(exact) != 1:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "Frontend Contract route has no unique captured principal",
                )
            operation = exact[0]
            route_identity = {
                "method": method,
                "logical_target": logical_target,
                "contract_id": contract_id,
                "operation_id": operation_id,
                "provider_id": provider_id,
                "function_id": function_id,
                "frontend_map_digest": frontend_map_digest,
            }
            mutating = method != "GET"
            routes.append(
                {
                    "route_id": canonical_digest(route_identity),
                    **route_identity,
                    "contribution_id": str(getattr(target, "contribution_id")),
                    "presentation": presentation,
                    "owner_pack_id": str(operation["owner_pack_id"]),
                    "manifest_digest": str(operation["artifact_digest"]),
                    "function_principal_id": str(operation["function_principal_id"]),
                    "allowed_payload_keys": sorted(
                        str(key) for key in getattr(target, "allowed_payload_keys")
                    ),
                    "security": {
                        "transport": "canonical_contract",
                        "panel_authentication_required": True,
                        "broker_authority_required": True,
                        "csrf_required": mutating,
                        "request_id_required": mutating,
                        "replay_protection_required": mutating,
                    },
                }
            )
    return sorted(routes, key=lambda item: (item["logical_target"], item["method"]))


def _principal_id(value: Mapping[str, Any]) -> str:
    from core_runtime.authority.v4 import FunctionPrincipal

    return FunctionPrincipal.from_dict(value).principal_id


def _operation_contribution_id(binding: Mapping[str, Any]) -> str:
    """Return the stable normalized ID for one exact resolved Operation."""

    return "operation::" + canonical_digest(
        {
            "pack_id": binding["pack_id"],
            "contract_id": binding["contract_id"],
            "operation_id": binding["operation_id"],
            "function_principal": binding["function_principal"],
        }
    ).removeprefix("sha256:")


def _artifact_projection(
    owner_pack_id: str,
    artifact: Mapping[str, Any],
) -> dict[str, str]:
    """Normalize one manifest-relative artifact without exposing a Host path."""

    raw_path = str(artifact["path"])
    relative = PurePosixPath(raw_path)
    normalized = relative.as_posix()
    if (
        relative.is_absolute()
        or normalized != raw_path
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "selected Pack artifact path is not canonical and relative",
        )
    digest = str(artifact["digest"])
    return {
        "entry_id": canonical_digest(
            {
                "owner_pack_id": owner_pack_id,
                "path": normalized,
                "artifact_digest": digest,
            }
        ),
        "owner_pack_id": owner_pack_id,
        "path": normalized,
        "kind": str(artifact["kind"]),
        "artifact_digest": digest,
    }


def _normalized_plan_bindings(
    snapshot: RuntimeSurfaceSnapshot,
) -> list[dict[str, object]]:
    """Normalize only exact Profile edges and ResolvedPlan principal bindings."""

    active = snapshot.active
    bindings = active.resolved.plan["bindings"]
    principals = {
        str(item["function_principal"]["function_id"]): item["function_principal"]
        for item in bindings
    }
    shell_id = str(active.resolved.profile["shell"]["pack_id"])
    shell_manifest = snapshot.catalog.packs.get(shell_id)
    if shell_manifest is None:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "active Shell principal manifest is unavailable",
        )
    for function in shell_manifest["functions"]:
        contracts = [
            item
            for item in shell_manifest["contracts"]
            if item["revision_digest"] == function["contract_revision_digest"]
        ]
        if len(contracts) != 1 or len(function["operations"]) != 1:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "active Shell principal declaration is ambiguous",
            )
        principals[str(function["id"])] = {
            "parent_artifact_digest": str(shell_manifest["pack"]["artifact_digest"]),
            "function_implementation_digest": str(function["implementation_digest"]),
            "function_id": str(function["id"]),
            "contract_revision_digest": str(function["contract_revision_digest"]),
            "operation_id": str(function["operations"][0]),
        }
    edge_lookup = {
        (
            str(edge["caller_function_id"]),
            str(edge["target_provider_id"]),
            str(edge["contract_id"]),
            str(edge["operation_id"]),
        ): edge
        for edge in active.resolved.profile["requested_edges"]
    }
    result: list[dict[str, object]] = []
    for binding in bindings:
        target = binding["function_principal"]
        edge = edge_lookup.get(
            (
                str(binding["caller_function_id"]),
                str(target["function_id"]),
                str(binding["contract_id"]),
                str(binding["operation_id"]),
            )
        )
        if edge is None or binding["caller_function_id"] not in principals:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "ResolvedPlan binding has no exact Profile edge principals",
            )
        result.append(
            {
                "binding_id": canonical_digest(binding),
                "source_principal_id": _principal_id(
                    principals[str(binding["caller_function_id"])]
                ),
                "target_principal_id": _principal_id(target),
                "target_contract_id": str(binding["contract_id"]),
                "operation_id": str(binding["operation_id"]),
                "owner_pack_id": str(binding["pack_id"]),
                "edge_digest": canonical_digest(edge),
                "authority_reference": str(binding["authority_reference"]),
            }
        )
    return result


def _captured_lifecycle_projection(
    snapshot: RuntimeSurfaceSnapshot | None = None,
) -> Mapping[str, Any]:
    """Read the exact active Pack-control catalog or fail the surface closed."""

    try:
        from core_runtime.pack_control_v4 import capture_pack_control_catalog

        return capture_pack_control_catalog(active=None if snapshot is None else snapshot.active)
    except Exception as error:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.API_FAILURE,
            "authoritative Pack lifecycle projection is unavailable",
        ) from error


def _packvm_attested(value: Mapping[str, object] | None) -> bool:
    """Accept only a fresh, internally digest-bound Host PackVM snapshot."""

    if not isinstance(value, Mapping) or value.get("ready") is not True:
        return False
    required = (
        "version",
        "backend_id",
        "instance",
        "instance_machine_id",
        "instance_config_hash",
        "instance_directory_device",
        "instance_directory_inode",
        "config_digest",
        "image_digest",
        "image_source",
        "image_local_device",
        "image_local_inode",
        "limactl_digest",
        "lima_home_digest",
        "lima_home_device",
        "lima_home_inode",
        "guest_runner_digest",
        "host_build_digest",
        "ceremony_nonce_digest",
        "session_digest",
        "plan_digest",
        "created_unix",
    )
    if any(key not in value for key in required):
        return False
    if (
        value.get("backend_id") != "tobkiri.python-pack-v4"
        or value.get("instance") != "tobkiri-packvm-v4"
    ):
        return False
    observed = value.get("observed_unix")
    if not isinstance(observed, int) or abs(time.time() - observed) > 30:
        return False
    attestation_digest = value.get("attestation_digest")
    if not isinstance(attestation_digest, str):
        return False
    attested = {key: value[key] for key in required}
    return hmac.compare_digest(attestation_digest, canonical_digest(attested))


def _capability_invocation_target(
    value: Mapping[str, object] | None,
    *,
    active: ActiveDefaultProfile,
    operation: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Return one exact PackAPI invoke target after catalog hash verification."""

    if value is None:
        return None
    profile_id = str(active.resolved.profile["profile_id"])
    profile_revision = str(active.resolved.plan["profile_revision"])
    activation_id = str(active.activation["activation_id"])
    plan_digest = str(active.resolved.plan["plan_digest"])
    if (
        value.get("profile_id") != profile_id
        or value.get("profile_revision") != profile_revision
        or value.get("activation_id") != activation_id
        or value.get("plan_digest") != plan_digest
    ):
        return None
    targets = value.get("targets")
    if not isinstance(targets, list) or any(not isinstance(item, Mapping) for item in targets):
        return None
    required = (
        "contribution_id",
        "contract_id",
        "operation_id",
        "provider_id",
        "function_id",
        "artifact_digest",
    )
    digest_targets: list[dict[str, str]] = []
    for target in targets:
        if any(not isinstance(target.get(key), str) for key in required) or not isinstance(
            target.get("owner_pack_id"), str
        ):
            return None
        digest_targets.append({key: str(target[key]) for key in required})
    expected_hash = canonical_digest(
        {
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "activation_id": activation_id,
            "plan_digest": plan_digest,
            "contributions": digest_targets,
        }
    )
    if not hmac.compare_digest(str(value.get("catalog_hash") or ""), expected_hash):
        return None
    exact = [
        target
        for target in targets
        if target.get("contract_id") == operation.get("contract_id")
        and target.get("operation_id") == operation.get("operation_id")
        and target.get("provider_id") == operation.get("target_provider_id")
        and target.get("function_id") == operation.get("function_id")
        and target.get("artifact_digest") == operation.get("artifact_digest")
    ]
    return exact[0] if len(exact) == 1 else None


def _validated_contract_catalogs(
    snapshot: RuntimeSurfaceSnapshot,
) -> dict[str, Mapping[str, Any]]:
    """Load only digest-pinned Contract catalogs for the selected Pack closure."""

    from core_runtime.external_pack_catalog_v4 import resolve_admitted_pack_root
    from core_runtime.pack_boundary import PackBoundaryError

    catalogs: dict[str, Mapping[str, Any]] = {}
    selected_pack_ids = sorted(
        str(item["identity"])
        for item in snapshot.active.resolved.lock["effective_set"]
        if snapshot.catalog.packs.get(str(item["identity"]), {}).get("contracts")
    )
    for pack_id in selected_pack_ids:
        manifest = snapshot.catalog.packs.get(pack_id)
        if manifest is None:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack is absent from the verified manifest catalog",
            )
        try:
            root = resolve_admitted_pack_root(pack_id)
            root_before = root.lstat()
        except PackBoundaryError:
            # Defaults bundle-only Base/Shell/Application artifacts do not have
            # a materialized Pack root. Their manifest declaration remains
            # visible, but no schema document is synthesized for it.
            continue
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack artifact root is unavailable",
            ) from error
        if root.is_symlink() or not root.is_dir():
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack artifact root is invalid",
            )
        manifest_path = root / "pack.v4.json"
        index_path = root / "artifact-index.v4.json"
        contracts_path = root / "contracts.v4.json"
        if any(
            path.is_symlink() or not path.is_file()
            for path in (manifest_path, index_path, contracts_path)
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack Contract catalog artifact is unavailable",
            )
        try:
            materialized_manifest_bytes = manifest_path.read_bytes()
            materialized_manifest = validate_document(
                materialized_manifest_bytes,
                "pack",
            )
            index_bytes = index_path.read_bytes()
            contract_bytes = contracts_path.read_bytes()
            index = validate_document(index_bytes, "pack_artifact_index")
            catalog = validate_document(contract_bytes, "pack_contract_catalog")
            root_after = root.lstat()
        except Exception as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack Contract catalog artifact is invalid",
            ) from error
        if root.is_symlink() or (root_before.st_dev, root_before.st_ino) != (
            root_after.st_dev,
            root_after.st_ino,
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack artifact root changed while reading",
            )
        index_entries = [
            item
            for item in index["artifacts"]
            if item["role"] == "contract_catalog" and item["path"] == "contracts.v4.json"
        ]
        digest = "sha256:" + hashlib.sha256(contract_bytes).hexdigest()
        materialized_manifest_digest = (
            "sha256:" + hashlib.sha256(materialized_manifest_bytes).hexdigest()
        )
        projected_source_identity = str(manifest["integrity"]["source_identity"])
        source_identity = str(materialized_manifest["integrity"]["source_identity"])
        if (
            materialized_manifest["pack"]["id"] != pack_id
            or materialized_manifest["pack"]["artifact_digest"]
            != manifest["pack"]["artifact_digest"]
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack materialization identity does not match",
            )
        if source_identity != projected_source_identity:
            provenance = manifest.get("provenance")
            expected_source_path = f"ecosystem/{pack_id}/pack.v4.json"
            if (
                not isinstance(provenance, Mapping)
                or provenance.get("schema") != "io.tobkiri.provenance.v2"
                or provenance.get("source_kind") != "generated"
                or provenance.get("source_path") != expected_source_path
                or provenance.get("source_digest") != materialized_manifest_digest
                or provenance.get("source_digest") != projected_source_identity
            ):
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "selected Pack projection is not bound to its materialization",
                )
        if (
            len(index_entries) != 1
            or str(index_entries[0]["digest"]) != digest
            or str(materialized_manifest["integrity"]["contract_catalog_digest"]) != digest
            or str(index["pack_id"]) != pack_id
            or str(catalog["pack_id"]) != pack_id
            or str(index["source_identity"]) != source_identity
            or str(catalog["source_identity"]) != source_identity
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected Pack Contract catalog digest does not match its manifest",
            )
        catalogs[pack_id] = catalog
    return catalogs


def _one_contract_document(
    catalog: Mapping[str, Any],
    *,
    contract_id: str,
    revision_digest: str,
) -> Mapping[str, Any]:
    documents = [
        item
        for item in catalog["contracts"]
        if item["contract_id"] == contract_id and item["revision_digest"] == revision_digest
    ]
    if len(documents) != 1:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "Pack manifest has no unique verified Contract document",
        )
    return documents[0]


def _operation_schema_projection(
    contract: Mapping[str, Any], operation: Mapping[str, Any]
) -> dict[str, object]:
    schemas = contract["schema_catalog"]
    schema_keys = (
        "input_schema_digest",
        "output_schema_digest",
        "error_schema_digest",
    )
    if any(operation[key] not in schemas for key in schema_keys):
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "verified Operation schema document is absent",
        )
    return {
        "operation_id": str(operation["operation_id"]),
        "input_schema_digest": str(operation["input_schema_digest"]),
        "output_schema_digest": str(operation["output_schema_digest"]),
        "error_schema_digest": str(operation["error_schema_digest"]),
        "input_schema": dict(schemas[operation["input_schema_digest"]]),
        "output_schema": dict(schemas[operation["output_schema_digest"]]),
        "error_schema": dict(schemas[operation["error_schema_digest"]]),
        "effect_ceiling": list(operation["effect_ceiling"]),
        "scope_semantics": operation.get("scope_semantics"),
        "scope_semantics_digest": operation.get("scope_semantics_digest"),
        "idempotency": dict(operation.get("idempotency") or {"mode": "none"}),
        "timeout_default_ms": operation.get("timeout_default_ms"),
        "timeout_hard_max_ms": operation.get("timeout_hard_max_ms"),
    }


def _catalog_for_active_closure(
    catalog: BundledCatalog, active: ActiveDefaultProfile
) -> BundledCatalog:
    """Add only admitted external manifests selected by the verified lock."""

    return _catalog_for_effective_sets(
        catalog,
        (active.resolved.lock["effective_set"],),
    )


def _catalog_for_effective_sets(
    catalog: BundledCatalog,
    effective_sets: object,
) -> BundledCatalog:
    """Add admitted external manifests required by exact resolved closures."""

    expected: dict[str, str] = {}
    if not isinstance(effective_sets, (list, tuple)):
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "resolved Profile closure is invalid",
        )
    for effective_set in effective_sets:
        if not isinstance(effective_set, list):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "resolved Profile closure is invalid",
            )
        for item in effective_set:
            if not isinstance(item, Mapping):
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "resolved Profile closure entry is invalid",
                )
            pack_id = str(item.get("identity") or "")
            digest = str(item.get("artifact_digest") or "")
            prior = expected.setdefault(pack_id, digest)
            if not pack_id or prior != digest:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                    "resolved Profile closure identity is inconsistent",
                )
    missing = {pack_id for pack_id in expected if pack_id not in catalog.packs}
    if not missing:
        return catalog
    from core_runtime.external_pack_catalog_v4 import resolve_admitted_pack_root

    packs = dict(catalog.packs)
    for pack_id in sorted(missing):
        root = resolve_admitted_pack_root(pack_id)
        manifest_path = root / "pack.v4.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected external Pack manifest is unavailable",
            )
        try:
            manifest = validate_document(manifest_path.read_bytes(), "pack")
        except Exception as error:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected external Pack manifest is invalid",
            ) from error
        if (
            manifest["pack"]["id"] != pack_id
            or manifest["pack"]["artifact_digest"] != expected[pack_id]
        ):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
                "selected external Pack identity does not match",
            )
        packs[pack_id] = manifest
    return BundledCatalog(
        root=catalog.root,
        packs=packs,
        bases=catalog.bases,
        shells=catalog.shells,
        profiles=catalog.profiles,
        artifact_root=catalog.artifact_root,
        executable_catalogs=catalog.executable_catalogs,
    )


def _commit_authority_profile_approval(
    candidate: _ProfileCandidate,
    *,
    session_id: str,
    approval_id: str,
    decided_at: float,
    user_data_root: Path,
) -> Any:
    """Commit immutable approval provenance and its Authority audit event."""

    from core_runtime.authority.v4 import (
        ApprovalRecord,
        AuthorityStore,
        FunctionPrincipal,
    )
    from core_runtime.authority.v4_models import authority_digest
    from core_runtime.pack_control_v4 import CONTROL_PRESENTATION_CONTRACT

    bindings = candidate.resolved.plan.get("bindings")
    approval_bindings = [
        binding
        for binding in bindings or []
        if isinstance(binding, Mapping)
        and binding.get("contract_id") == CONTROL_PRESENTATION_CONTRACT
        and binding.get("operation_id") == "profile.change.approve"
        and binding.get("function_principal", {}).get("function_id")
        == "tobkiri.host.control-presentation"
    ]
    if len(approval_bindings) != 1:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "canonical Profile activation approval principal is absent or ambiguous",
        )
    approval_binding = approval_bindings[0]
    if not isinstance(approval_binding.get("function_principal"), Mapping):
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "Profile candidate principal is invalid",
        )
    principal = FunctionPrincipal.from_dict(approval_binding["function_principal"])
    actor_suffix = authority_digest({"session_id": session_id}).removeprefix("sha256:")[:24]
    record = ApprovalRecord(
        approval_id=approval_id,
        snapshot_digest=candidate.candidate_digest,
        actor_id=f"launcher.panel.{actor_suffix}",
        decision="approved",
        decided_at=decided_at,
        caller=principal,
        target=principal,
        profile_id=str(candidate.resolved.profile["profile_id"]),
        effect_bundle_digest=canonical_digest(candidate.resolved.plan["bindings"]),
        security_epoch=int(candidate.resolved.plan["security_epoch"]),
    )
    try:
        with AuthorityStore(user_data_root / "authority" / "v4.sqlite3") as authority:
            if authority.security_epoch != record.security_epoch:
                raise RuntimeSurfaceError(
                    RuntimeSurfaceErrorCode.STALE_REVISION,
                    "Profile approval SecurityEpoch is stale",
                )
            committed = authority.get_approval(record.approval_id)
            if committed is None:
                try:
                    authority.put_records_atomically([record])
                except Exception:
                    committed = authority.get_approval(record.approval_id)
                    if committed is None:
                        raise
                else:
                    committed = authority.get_approval(record.approval_id)
    except RuntimeSurfaceError:
        raise
    except Exception as error:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "Authority Kernel did not commit Profile approval",
        ) from error
    if committed != record:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "Authority Kernel Profile approval receipt is unavailable",
        )
    return record


def _verify_authority_profile_approval(
    approval: _ProfileApproval,
    *,
    user_data_root: Path,
) -> None:
    """Re-authenticate the durable approval immediately before activation."""

    from core_runtime.authority.v4 import AuthorityStore

    try:
        with AuthorityStore(user_data_root / "authority" / "v4.sqlite3") as authority:
            record = authority.get_approval(approval.authority_record_id)
            current_epoch = authority.security_epoch
    except Exception as error:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "Authority Kernel Profile approval cannot be verified",
        ) from error
    candidate = approval.candidate
    if (
        record is None
        or record.decision != "approved"
        or record.digest != approval.approval_digest
        or record.snapshot_digest != candidate.candidate_digest
        or record.profile_id != candidate.resolved.profile["profile_id"]
        or record.security_epoch != candidate.resolved.plan["security_epoch"]
        or current_epoch != record.security_epoch
    ):
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "Authority Kernel Profile approval is stale or does not match",
        )


def _map_change_error(error: Exception) -> RuntimeSurfaceError:
    """Map Profile control failures without exposing host paths or internals."""

    if isinstance(error, RuntimeSurfaceError):
        return error
    from core_runtime.pack_control_v4 import PackControlDenied

    if isinstance(error, PackControlDenied):
        typed_codes = {
            "pack_control_stale_revision": RuntimeSurfaceErrorCode.STALE_REVISION,
            "pack_control_conflict": RuntimeSurfaceErrorCode.STALE_REVISION,
            "pack_control_digest_mismatch": RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "pack_control_unapproved": RuntimeSurfaceErrorCode.UNAPPROVED,
            "pack_control_denied": RuntimeSurfaceErrorCode.UNAPPROVED,
            "pack_control_invalid_request": RuntimeSurfaceErrorCode.INVALID_REQUEST,
            "pack_control_timeout": RuntimeSurfaceErrorCode.TIMEOUT,
            "pack_control_unavailable": RuntimeSurfaceErrorCode.API_FAILURE,
        }
        code = typed_codes.get(error.code)
        if code is not None:
            return RuntimeSurfaceError(code, "Profile change could not be completed")
    message = str(error).lower()
    if "stale" in message or "predecessor" in message:
        return RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.STALE_REVISION,
            "Profile change predecessor is stale",
        )
    if "digest" in message or "changed" in message or "inconsistent" in message:
        return RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "Profile change integrity verification failed",
        )
    if "approv" in message or "authority" in message or "installed" in message:
        return RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "Profile change is not approved for activation",
        )
    return RuntimeSurfaceError(
        RuntimeSurfaceErrorCode.API_FAILURE,
        "Profile change could not be completed",
    )


def _map_reconciliation_error(error: Exception, *, approval: bool = False) -> RuntimeSurfaceError:
    """Map durable reconciliation failures without leaking persisted state."""

    if isinstance(error, RuntimeSurfaceError):
        return error
    message = str(error).lower()
    if "expired" in message:
        return RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.TIMEOUT,
            "Profile ceremony candidate expired",
        )
    if approval or "approval" in message:
        return RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.UNAPPROVED,
            "Profile activation approval is unavailable",
        )
    if "binding" in message or "another session" in message or "changed" in message:
        return RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.DIGEST_MISMATCH,
            "Profile ceremony durable binding does not match",
        )
    if "unknown" in message:
        return RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.TIMEOUT,
            "Profile ceremony candidate is unavailable",
        )
    return RuntimeSurfaceError(
        RuntimeSurfaceErrorCode.API_FAILURE,
        "Profile ceremony reconciliation failed",
    )


def _required_string(value: object) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.INVALID_REQUEST,
            "required Profile ceremony binding is missing",
        )
    return normalized


def _coalesce_selected_profile_id(
    profile_id: str | None,
    selected_profile_id: str | None,
) -> str | None:
    """Accept one explicit browsing selector without allowing ambiguity."""

    if profile_id is not None and selected_profile_id is not None:
        if str(profile_id) != str(selected_profile_id):
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.INVALID_REQUEST,
                "browsing Profile selectors disagree",
            )
    selected = selected_profile_id if selected_profile_id is not None else profile_id
    if selected is None:
        return None
    normalized = str(selected).strip()
    if not normalized:
        raise RuntimeSurfaceError(
            RuntimeSurfaceErrorCode.INVALID_REQUEST,
            "browsing Profile selector is empty",
        )
    return normalized


__all__ = [
    "RUNTIME_SURFACE_API_VERSION",
    "RuntimeSurfaceErrorCode",
    "RuntimeProfileChangeService",
    "RuntimeSurfaceError",
    "RuntimeSurfaceService",
]
