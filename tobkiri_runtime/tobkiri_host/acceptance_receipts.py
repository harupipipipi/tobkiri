"""Non-publishable Host ledger for PackVM QA acceptance receipts."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading
import time

from .models import RuntimeEvidence

_ENABLE_ENV = "TOBKIRI_PACKVM_ACCEPTANCE_ENABLE"
_DIGEST_ENV = "TOBKIRI_PACKVM_ACCEPTANCE_PACK_DIGEST"
_PACK_ID = "tobkiri_packvm_sandbox_qa_pack"
_CI_APP_IDENTIFIER = "dev.tobkiri.launcher.ci-e2e"
_CI_DATA_ROOT_ENV = "TOBKIRI_CI_E2E_APP_DATA_ROOT"
_OPERATION_PREFIX = f"{_PACK_ID}."
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SCENARIOS = frozenset(
    {
        "probe_isolation",
        "stdin_overflow",
        "stdout_overflow",
        "stderr_overflow",
        "deadline_hold",
        "cancel_hold",
        "abnormal_exit",
    }
)


@dataclass(frozen=True)
class AcceptanceReceipt:
    """Host-authenticated terminal facts, never Pack-authored output."""

    request_id: str
    scenario: str
    nonce: str
    guest_artifact_identity: str
    attestation_digest: str
    request_digest: str
    original_deadline_ns: int
    finished_ns: int
    termination: str
    exit_code: int | None
    authenticated_cancel_ack: bool
    invocation_reaped: bool
    resource_reservation_released: bool
    materialization_released: bool


@dataclass
class _Record:
    request_id: str
    scenario: str
    nonce: str
    guest_artifact_identity: str
    attestation_digest: str
    request_digest: str
    original_deadline_ns: int
    termination: str | None = None
    exit_code: int | None = None
    authenticated_cancel_ack: bool = False
    invocation_reaped: bool = False
    resource_reservation_released: bool = False
    materialization_released: bool = False
    finished_ns: int = 0


class AcceptanceReceiptPort:
    """Record only Broker-owned facts for the exact signed QA fixture."""

    def __init__(self, expected_pack_digest: str, *, _gate: object) -> None:
        if _gate is not _HOST_GATE or _DIGEST.fullmatch(expected_pack_digest) is None:
            raise PermissionError("PackVM acceptance receipt port is unavailable")
        self._expected_pack_digest = expected_pack_digest
        self._records: dict[str, _Record] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_host_environment(
        cls,
        *,
        app_identifier: str,
        user_data_root: Path,
    ) -> AcceptanceReceiptPort:
        """Create the port only under the native CI/E2E acceptance gates."""

        enabled = os.environ.get(_ENABLE_ENV, "").strip().lower()
        digest = os.environ.get(_DIGEST_ENV, "").strip()
        configured_root = os.environ.get(_CI_DATA_ROOT_ENV, "").strip()
        try:
            exact_root = user_data_root.resolve(strict=True)
            isolated_root = Path(configured_root).resolve(strict=True)
        except (OSError, RuntimeError):
            exact_root = None
            isolated_root = None
        if (
            app_identifier != _CI_APP_IDENTIFIER
            or not configured_root
            or exact_root is None
            or isolated_root is None
            or exact_root != isolated_root
            or enabled not in {"1", "true", "yes", "on"}
            or _DIGEST.fullmatch(digest) is None
        ):
            raise PermissionError("PackVM acceptance receipt port is unavailable")
        return cls(digest, _gate=_HOST_GATE)

    def begin(
        self,
        *,
        request_id: str,
        pack_id: str,
        pack_digest: str,
        operation_id: str,
        nonce: str,
        evidence: RuntimeEvidence,
        request_digest: str,
        original_deadline_ns: int,
    ) -> None:
        """Capture materialization facts verified by the Broker."""

        scenario = operation_id.removeprefix(_OPERATION_PREFIX)
        if (
            pack_id != _PACK_ID
            or pack_digest != self._expected_pack_digest
            or not operation_id.startswith(_OPERATION_PREFIX)
            or scenario not in _SCENARIOS
            or not request_id
            or len(nonce) != 64
            or not all(character in "0123456789abcdef" for character in nonce)
            or _DIGEST.fullmatch(request_digest) is None
            or type(original_deadline_ns) is not int
            or original_deadline_ns <= 0
            or evidence.guest_execution_boundary != "linux-packvm-guest"
            or evidence.authenticated_channel is not True
            or evidence.nonce_fresh is not True
            or evidence.guest_artifact_identity is None
            or evidence.attestation_digest is None
        ):
            raise PermissionError("PackVM acceptance invocation binding is invalid")
        with self._lock:
            if request_id in self._records:
                raise PermissionError("PackVM acceptance request identity was reused")
            self._records[request_id] = _Record(
                request_id=request_id,
                scenario=scenario,
                nonce=nonce,
                guest_artifact_identity=evidence.guest_artifact_identity,
                attestation_digest=evidence.attestation_digest,
                request_digest=request_digest,
                original_deadline_ns=original_deadline_ns,
            )

    def is_candidate(self, pack_id: str, pack_digest: str, operation_id: str) -> bool:
        """Return true only for the exact configured QA Pack operation domain."""

        return (
            pack_id == _PACK_ID
            and pack_digest == self._expected_pack_digest
            and operation_id.startswith(_OPERATION_PREFIX)
        )

    def record_completed(self, request_id: str) -> None:
        """Record a normal Host-observed guest completion."""

        self._terminate(request_id, "completed", None, False)

    def record_authenticated_cancel(self, request_id: str, *, deadline: bool) -> None:
        """Record successful backend.cancel return for this exact request."""

        self._terminate(
            request_id,
            "deadline_expired" if deadline else "cancelled",
            None,
            True,
        )

    def record_abnormal_exit(self, request_id: str, exit_code: int) -> None:
        """Record a typed guest termination status supplied by the backend."""

        if type(exit_code) is not int or exit_code == 0:
            raise ValueError("PackVM acceptance abnormal exit status is invalid")
        self._terminate(request_id, "abnormal_exit", exit_code, False)

    def record_invocation_reaped(self, request_id: str) -> None:
        """Record Future completion; cancellation ACK alone is insufficient."""

        with self._lock:
            self._require(request_id).invocation_reaped = True

    def record_resources_released(
        self,
        request_id: str,
        *,
        reservation: bool,
        materialization: bool,
    ) -> None:
        """Record cleanup only after the corresponding Host calls succeeded."""

        if reservation is not True or materialization is not True:
            raise ValueError("PackVM acceptance cleanup proof is invalid")
        with self._lock:
            record = self._require(request_id)
            record.resource_reservation_released = True
            record.materialization_released = True

    def take(self, request_id: str, nonce: str) -> AcceptanceReceipt:
        """Consume a complete exact receipt; incomplete state remains unavailable."""

        with self._lock:
            record = self._require(request_id)
            if (
                record.nonce != nonce
                or record.termination is None
                or record.finished_ns <= 0
                or not record.invocation_reaped
                or not record.resource_reservation_released
                or not record.materialization_released
            ):
                raise PermissionError("PackVM acceptance receipt is incomplete")
            if record.termination in {"deadline_expired", "cancelled"}:
                if not record.authenticated_cancel_ack:
                    raise PermissionError("PackVM acceptance cancellation is unverified")
            receipt = AcceptanceReceipt(**record.__dict__)
            del self._records[request_id]
            return receipt

    def discard(self, request_id: str) -> None:
        """Drop an incomplete record after a non-acceptance failure."""

        with self._lock:
            self._records.pop(request_id, None)

    def _terminate(
        self,
        request_id: str,
        termination: str,
        exit_code: int | None,
        authenticated_cancel_ack: bool,
    ) -> None:
        with self._lock:
            record = self._require(request_id)
            if record.termination is not None:
                raise PermissionError("PackVM acceptance termination was already recorded")
            record.termination = termination
            record.exit_code = exit_code
            record.authenticated_cancel_ack = authenticated_cancel_ack
            record.finished_ns = time.monotonic_ns()

    def _require(self, request_id: str) -> _Record:
        record = self._records.get(request_id)
        if record is None:
            raise PermissionError("PackVM acceptance request is unavailable")
        return record


_HOST_GATE = object()


__all__ = ["AcceptanceReceipt", "AcceptanceReceiptPort"]
