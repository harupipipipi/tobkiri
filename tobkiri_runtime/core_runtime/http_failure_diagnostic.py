"""Fixed-vocabulary diagnostics for failed Shell startup HTTP reads."""

from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import unquote, urlsplit

_LOGGER = logging.getLogger(__name__)
_PREFIX = "/api/contracts/defaultspack/"
_READS = {
    "/api/ui/full-catalog": "catalog",
    "/api/tools/catalog": "catalog",
    "/api/ai/strategies": "catalog",
    "/api/ui/settings": "settings",
    "/api/command-protocol/v1/catalog": "commands",
}
_CODES = frozenset(
    {
        "PROFILE_NOT_ACTIVE",
        "STALE_REVISION",
        "DIGEST_MISMATCH",
        "OPERATION_NOT_FOUND",
        "UNAPPROVED",
        "TIMEOUT",
        "UNSUPPORTED_PLATFORM",
        "INVALID_REQUEST",
        "SAVED_TURN_NOT_STARTED",
        "API_FAILURE",
        "PACKVM_HOST_CAPACITY_INSUFFICIENT",
        "CONTRACT_OPERATION_INVALID",
        "CONTRACT_METHOD_MISMATCH",
        "CONTRACT_METHOD_UNSUPPORTED",
        "CONTRACT_PATH_INVALID",
        "CONTRACT_OPERATION_UNKNOWN",
        "CONTRACT_QUERY_INVALID",
        "CONTRACT_MAP_STALE",
        "invalid_request_identity",
        "invalid_contract_payload",
        "request_replay_capacity_exhausted",
        "operation_reconciliation_capacity_exhausted",
    }
)
_MESSAGE = "Shell UI read failed interface=%s http_status=%s code=%s"


class _DeferredWriter(Protocol):
    def _defer_response_log(
        self, log: logging.Logger, level: int, message: str, *args: object
    ) -> None: ...


class _SafeLogger(logging.Logger):
    def log(self, level: int, msg: object, *args: object, **kwargs: object) -> None:
        try:
            _LOGGER.warning(_MESSAGE, *args)
        except Exception:
            # A diagnostic sink cannot change delivery or request cleanup.
            pass


_SAFE_LOGGER = _SafeLogger(__name__ + ".deferred")


def defer_ui_read_failure(
    writer: _DeferredWriter,
    *,
    method: object,
    request_path: object,
    status: object,
    response_data: object,
) -> None:
    """Queue one fixed record after a failed allowlisted HTTP response flush.

    Parsing only classifies an interface; it grants no route authority. No raw
    request, query, response payload, error text, or header enters the log queue.
    """

    try:
        if type(method) is not str or method != "GET":
            return
        if type(status) is not int or not 400 <= status <= 599:
            return
        if type(request_path) is not str or len(request_path) > 8192:
            return
        if not request_path.startswith(_PREFIX):
            return
        operation = unquote(request_path[len(_PREFIX) :])
        if not operation.startswith("GET "):
            return
        target = urlsplit(operation[4:])
        if target.scheme or target.netloc or target.fragment:
            return
        interface = _READS.get(target.path)
        if interface is None:
            return
        candidate = response_data.get("code") if type(response_data) is dict else None
        code = (
            candidate if type(candidate) is str and candidate in _CODES else "unknown"
        )
        writer._defer_response_log(
            _SAFE_LOGGER, logging.WARNING, _MESSAGE, interface, status, code
        )
    except Exception:
        # Diagnostics must not alter an otherwise completed HTTP response.
        return
