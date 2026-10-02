"""Stable errors returned by the Pack v4 host execution core."""

from __future__ import annotations


class HostCoreError(Exception):
    """Base class with a stable, non-provider-controlled error code."""

    code = "host_core_error"


class InvalidArtifactError(HostCoreError):
    """Artifact metadata is inconsistent or incomplete."""

    code = "invalid_artifact"


class ResolutionError(HostCoreError):
    """A request cannot be resolved to one exact operation binding."""

    code = "resolution_failed"


class AdapterError(HostCoreError):
    """A structural adapter violates a safety or topology constraint."""

    code = "adapter_invalid"


class AdmissionError(HostCoreError):
    """Static admission or resource reservation failed."""

    code = "admission_denied"


class BackendUnavailableError(HostCoreError):
    """No fully gated backend can safely execute the workload."""

    code = "backend_unavailable"


class SavedTurnRejectedError(BackendUnavailableError):
    """A saved turn was definitively rejected before any guest-visible effect.

    Raised only where the failure provably precedes guest execution — the
    Host-side saved preflight denial or a deterministic domain-allocation
    refusal — so no outcome receipt can ever exist.  The durable turn
    coordinator reads ``saved_terminal_error_code`` to settle the record
    ``failed`` instead of parking it in the fail-closed ``waiting`` state.
    Unmarked backend failures remain ambiguous and keep waiting.
    """

    code = "saved_turn_rejected"

    def __init__(self, reason: str, *, error_code: str = "BACKEND_UNAVAILABLE") -> None:
        if not isinstance(error_code, str) or not 0 < len(error_code) <= 64:
            raise ValueError("saved terminal error code is invalid")
        super().__init__(reason)
        self.saved_terminal_error_code = error_code


class AuthorizationError(HostCoreError):
    """The security core denied or could not verify the request."""

    code = "denied"


class AuditUnavailableError(HostCoreError):
    """An authoritative effect audit reservation could not be created."""

    code = "audit_unavailable"


class ResourceHandleError(HostCoreError):
    """An opaque resource handle is invalid, stale, or out of scope."""

    code = "resource_handle_invalid"


class QueueFullError(AdmissionError):
    """One or more bounded admission queue scopes are full."""

    code = "busy"


class ResourceExhaustedError(AdmissionError):
    """The reservation would violate a hard limit or host guard."""

    code = "resource_exhausted"


class RequestTimedOutError(HostCoreError):
    """A local request timed out with no uncertain external effect."""

    code = "timed_out"


class RequestCancellationRequestedError(HostCoreError):
    """Host cancellation was requested; provider termination is not certified."""

    code = "cancellation_requested"


class ProviderExecutionError(HostCoreError):
    """Provider failed; internal exception text is intentionally not exposed."""

    code = "provider_failed"


class PackVMAcceptanceError(HostCoreError):
    """Typed signed guest fact available only to the gated QA receipt path."""

    code = "packvm_acceptance_failed"

    def __init__(self, termination: str, exit_code: int | None = None) -> None:
        allowed = {
            "input_limit_rejected",
            "output_limit_rejected",
            "error_limit_rejected",
            "abnormal_exit",
        }
        if termination not in allowed:
            raise ValueError("PackVM acceptance termination is invalid")
        if termination == "abnormal_exit":
            if type(exit_code) is not int or exit_code == 0:
                raise ValueError("PackVM acceptance exit code is invalid")
        elif exit_code is not None:
            raise ValueError("PackVM limit rejection cannot carry an exit code")
        super().__init__("PackVM acceptance boundary rejected the operation")
        self.termination = termination
        self.exit_code = exit_code


class AmbiguousEffectError(HostCoreError):
    """An external effect may have been accepted and needs reconciliation."""

    code = "ambiguous_effect"

    def __init__(self, reconciliation_id: str) -> None:
        super().__init__("external effect status is unknown")
        self.reconciliation_id = reconciliation_id


class TriggerError(HostCoreError):
    """A trigger registration or occurrence is invalid."""

    code = "trigger_invalid"
