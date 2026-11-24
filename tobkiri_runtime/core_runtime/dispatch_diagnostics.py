"""Payload-free Host diagnostics for failures hidden by durable reconciliation."""

from __future__ import annotations

import logging
import re

_LOGGER = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,191}\Z")


def _identifier(value: object) -> str:
    return value if type(value) is str and _IDENTIFIER.fullmatch(value) else "unknown"


def log_nested_dispatch_failure(
    contract_id: str, operation_id: str, error: Exception,
) -> None:
    """Record bounded type names only; never format exceptions or their payloads.

    Durable callers deliberately hide exception messages from users because an
    operation can have committed effects before failing. Host operators still
    need to distinguish a deadline from admission, authority, or guest failure.
    This diagnostic does not change settlement, replay, or exception identity.
    """
    try:
        _log_failure(contract_id, operation_id, error)
    except Exception:  # noqa: BLE001 - diagnostic failures cannot replace dispatch
        # A broken diagnostic handler must never replace the dispatch failure
        # that the caller will re-raise and reconcile.
        return


def _log_failure(contract_id: str, operation_id: str, error: Exception) -> None:
    types: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(types) < 4 and id(current) not in seen:
        seen.add(id(current))
        types.append(_identifier(type(current).__name__))
        cause = current.__cause__
        current = cause if cause is not None else (
            None if current.__suppress_context__ else current.__context__
        )
    _LOGGER.warning(
        "nested_dispatch_failed contract=%s operation=%s error_types=%s",
        _identifier(contract_id), _identifier(operation_id), ">".join(types),
    )
