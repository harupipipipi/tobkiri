"""Finite Host-only failure evidence, without response or exception text."""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import threading
import time
from typing import Callable, Mapping
import urllib.error

_REASONS = frozenset(
    {
        "timeout",
        "network",
        "http_status",
        "parser",
        "cancelled",
        "progress",
        "policy",
        "unknown",
    }
)
_STAGES = frozenset(
    {
        "prepare",
        "dns",
        "connect",
        "tls",
        "send",
        "headers",
        "body",
        "decode",
        "normalize",
        "progress_claim",
        "progress_publish",
        "finalize",
    }
)
_MAX_DURATION_MS = 86_400_000
_LOGGER = logging.getLogger(__name__)


def _request_id(value: object) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"request\.[0-9a-f]{32}", value)
        else "unknown"
    )


def _log(record: Mapping[str, object]) -> None:
    _LOGGER.warning("%s", json.dumps(dict(record), sort_keys=True))


class RequestFailureDiagnostics:
    """Observe one captured request; observations never authorize or cancel work."""

    def __init__(
        self,
        request_id: object,
        *,
        parent_request_id: object = None,
        clock: Callable[[], float] = time.monotonic,
        sink: Callable[[Mapping[str, object]], None] = _log,
    ) -> None:
        self._request_id = _request_id(request_id)
        self._parent_id = _request_id(parent_request_id)
        self._clock, self._sink = clock, sink
        self._lock = threading.RLock()
        self._started = self._now()
        self._stage_started = self._started
        self._stage = "prepare"
        self._interruption = "unknown"
        self._emitted = False

    def _now(self) -> float:
        try:
            value = self._clock()
            return (
                float(value)
                if type(value) in {int, float} and math.isfinite(value)
                else 0.0
            )
        except Exception:
            return 0.0

    def stage(self, stage: str) -> None:
        """Select only a fixed stage; no caller strings enter the record."""
        with self._lock:
            normalized = stage if type(stage) is str and stage in _STAGES else "prepare"
            if normalized != self._stage:
                self._stage, self._stage_started = normalized, self._now()

    def interrupted(self, reason: str) -> None:
        """Latch a trusted lifetime observation across its owned watchdog thread."""
        with self._lock:
            if self._interruption == "unknown":
                self._interruption = (
                    reason
                    if type(reason) is str
                    and reason
                    in {
                        "timeout",
                        "cancelled",
                        "policy",
                    }
                    else "unknown"
                )

    def failure(self, error: BaseException, *, reason: str | None = None) -> None:
        """Emit at most once, before a boundary removes the original error type."""
        with self._lock:
            if self._emitted:
                return
            self._emitted = True
            if self._interruption != "unknown":
                reason = self._interruption
            elif reason is None:
                if self._stage in {"progress_claim", "progress_publish"}:
                    reason = "progress"
                elif isinstance(error, TimeoutError):
                    reason = "timeout"
                elif isinstance(error, InterruptedError):
                    reason = "cancelled"
                elif isinstance(error, urllib.error.HTTPError):
                    reason = "http_status"
                elif isinstance(error, PermissionError):
                    reason = "policy"
                elif isinstance(error, (OSError, http.client.HTTPException)):
                    reason = "network"
                elif isinstance(error, (ValueError, TypeError, UnicodeError, KeyError)):
                    reason = "parser"
            bucket = "none"
            if isinstance(error, urllib.error.HTTPError) and type(error.code) is int:
                status = error.code
                bucket = (
                    "redirect"
                    if 300 <= status < 400
                    else (
                        "auth"
                        if status in {401, 403}
                        else (
                            "rate_limit"
                            if status == 429
                            else (
                                "client"
                                if 400 <= status < 500
                                else "server" if 500 <= status < 600 else "other"
                            )
                        )
                    )
                )
            now = self._now()
            record = {
                "event": "host_request_failure",
                "request_id": self._request_id,
                "parent_request_id": self._parent_id,
                "reason": (
                    reason if type(reason) is str and reason in _REASONS else "unknown"
                ),
                "stage": self._stage,
                "http_bucket": bucket,
                "elapsed_ms": self._duration(now, self._started),
                "stage_elapsed_ms": self._duration(now, self._stage_started),
            }
        try:
            self._sink(record)
        except Exception:
            pass

    @staticmethod
    def _duration(now: float, started: float) -> int:
        elapsed = now - started
        if not math.isfinite(elapsed) or elapsed <= 0:
            return 0
        return min(_MAX_DURATION_MS, int(min(elapsed, 86_400) * 1000))
