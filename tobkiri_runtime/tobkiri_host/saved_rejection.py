"""Classify definitive saved-turn refusals before guest execution."""

from __future__ import annotations

from core_runtime.authority.v4 import AuthorityDenied

from .errors import BackendUnavailableError, ResolutionError, SavedTurnRejectedError


def saved_turn_rejection(
    exc: Exception,
    *,
    reason: str,
    default: str,
) -> BackendUnavailableError:
    """Return a terminal marker only if the pre-guest refusal is definitive."""
    code = _saved_terminal_code(exc, default=default)
    if code is None:
        return BackendUnavailableError(reason)
    return SavedTurnRejectedError(
        reason,
        error_code=code,
        terminal_details=_saved_terminal_details(exc),
    )


def is_packvm_gate_busy(exc: BaseException) -> bool:
    """Whether the allocator reported transient lifecycle-gate contention.

    The allocator is injected into the supervisor, so the concrete error type
    stays provisioner-side. It is matched by name across the exception's MRO
    without importing the provisioner module. Wrappers
    that chain the busy error through ``__cause__``/``__context__`` count as
    busy too — a wrapped retryable error must never latch compromise.
    """

    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if "PackVMGateBusyError" in {cls.__name__ for cls in type(current).__mro__}:
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _saved_terminal_code(exc: BaseException, *, default: str) -> str | None:
    """Return a bounded diagnostic code only for a definitive saved rejection.

    Callers use this before starting guest execution, so a deterministic
    denial there can never produce an outcome receipt and the
    durable turn settles ``failed`` with this code.  Transient lifecycle-gate
    contention and opaque failures stay unmarked: an unknown outcome keeps
    the fail-closed ``waiting``/reconciliation state.
    """

    if is_packvm_gate_busy(exc):
        return None
    if isinstance(exc, (AuthorityDenied, ResolutionError, ValueError)):
        code = getattr(exc, "code", None)
        return code if type(code) is str and code else default
    code = getattr(exc, "code", None)
    if type(code) is str and callable(getattr(exc, "to_dict", None)):
        return code
    return None


def _saved_terminal_details(exc: BaseException) -> dict[str, int]:
    """Return bounded capacity counters from a typed deterministic refusal."""

    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        required = getattr(current, "required_bytes", None)
        available = getattr(current, "available_bytes", None)
        if (
            getattr(current, "code", None) == "PACKVM_HOST_CAPACITY_INSUFFICIENT"
            and type(required) is int
            and type(available) is int
            and required > available >= 0
        ):
            return {
                "required_bytes": required,
                "available_bytes": available,
            }
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return {}
